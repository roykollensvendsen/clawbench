#!/usr/bin/env python3
"""
debug_episode.py — Interactive agent stepping debugger for ClawBench.

Runs an episode and streams tool calls in real time. Replaces the cycle
"edit policy → run 10 min eval → read log → guess" with live visibility.

Modes:
  --watch     Live-stream tool calls as they happen (default)
  --step      Pause after each tool call (requires debug server mode)
  --replay    Replay a saved episode result file interactively

Usage:
    # Live watch (start services first with docker compose)
    python scripts/debug_episode.py --scenario client_escalation --watch

    # Replay a saved result
    python scripts/debug_episode.py --replay results.json

    # With custom AGENTS.md
    python scripts/debug_episode.py --scenario inbox_triage --agents-md /path/to/AGENTS.md
"""

import argparse
import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clawbench.runner import (
    DEFAULT_OPENCLAW_URL, DEFAULT_OPENCLAW_TOKEN, DEFAULT_MOCK_TOOLS_URL, DEFAULT_MODEL,
    wait_for_services, send_message, get_tool_calls, get_all_requests,
    reset_scenario, extract_usage, get_session_usage,
)
from clawbench.scoring import score_episode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SANDBOX_DIR = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = SANDBOX_DIR / "scenarios"
FIXTURES_DIR = SANDBOX_DIR / "fixtures"
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_PATH", str(SANDBOX_DIR / "workspace")))

OPENCLAW_URL = os.getenv("OPENCLAW_URL", DEFAULT_OPENCLAW_URL)
OPENCLAW_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", DEFAULT_OPENCLAW_TOKEN)
MOCK_TOOLS_URL = os.getenv("MOCK_TOOLS_URL", DEFAULT_MOCK_TOOLS_URL)
CLAWBENCH_MODEL = os.getenv("CLAWBENCH_DEFAULT_MODEL", DEFAULT_MODEL)

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
RESET = "\033[0m"

TOOL_COLORS = {
    "exec": CYAN,
    "slack": MAGENTA,
    "memory_search": YELLOW,
    "memory_get": YELLOW,
    "read": BLUE,
    "web_search": DIM,
    "web_fetch": DIM,
    "himalaya": CYAN,
    "gcalcli": GREEN,
    "notion_cli": BLUE,
    "slack_cli": MAGENTA,
    "memo": YELLOW,
}


def fmt_tool_call(idx: int, call: dict) -> str:
    """Format a single tool call for terminal display."""
    tool = call.get("tool", "?")
    args = call.get("args", {})
    response = call.get("response", {})
    color = TOOL_COLORS.get(tool, "")

    lines = [f"{BOLD}{color}[{idx}] {tool}{RESET}"]

    # Format args
    if tool == "exec":
        cmd = args.get("command", "")
        lines.append(f"  {DIM}$ {RESET}{cmd}")
    elif tool == "slack":
        action = args.get("action", "")
        ch = args.get("channelId", args.get("to", ""))
        lines.append(f"  {DIM}action={RESET}{action} {DIM}channel={RESET}{ch}")
    elif tool in ("memory_search", "memo"):
        query = args.get("query", args.get("args", ""))
        lines.append(f"  {DIM}query={RESET}{query}")
    elif tool == "read":
        path = args.get("path", "")
        lines.append(f"  {DIM}path={RESET}{path}")
    else:
        args_str = json.dumps(args, default=str)
        if len(args_str) > 120:
            args_str = args_str[:117] + "..."
        lines.append(f"  {DIM}{args_str}{RESET}")

    # Format response summary
    resp_str = json.dumps(response, default=str)
    if len(resp_str) > 200:
        resp_str = resp_str[:197] + "..."
    lines.append(f"  {DIM}→ {resp_str}{RESET}")

    # Flag irreversible actions
    if response.get("_irreversible"):
        lines.append(f"  {RED}{BOLD}⚠ IRREVERSIBLE{RESET}")

    return "\n".join(lines)


def fmt_response(text: str, max_lines: int = 40) -> str:
    """Format the agent's final response."""
    lines = text.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"{DIM}... ({len(lines) - max_lines} more lines){RESET}"]
    return "\n".join(f"  {line}" for line in lines)


# ---------------------------------------------------------------------------
# Watch mode — poll /all_requests while episode runs
# ---------------------------------------------------------------------------
def watch_episode(scenario: str, message: str, user_context: dict | None = None):
    """Run an episode and live-stream tool calls."""
    # Reset scenario
    reset_scenario(MOCK_TOOLS_URL, scenario)
    if user_context:
        httpx.post(f"{MOCK_TOOLS_URL}/set_user_context", json=user_context, timeout=5)

    session_key = f"debug-{scenario}-{int(time.time() * 1000)}"

    print(f"\n{BOLD}{'═' * 70}{RESET}")
    print(f"{BOLD}  DEBUG EPISODE: {scenario}{RESET}")
    print(f"  Model: {CLAWBENCH_MODEL}")
    print(f"  Session: {session_key}")
    print(f"{BOLD}{'═' * 70}{RESET}")
    print(f"\n{DIM}Sending message...{RESET}")
    print(f"{DIM}{message[:120]}{'...' if len(message) > 120 else ''}{RESET}\n")

    # Run episode in background thread
    result = {"response": None, "error": None}

    def run():
        try:
            resp = send_message(
                OPENCLAW_URL, OPENCLAW_TOKEN, message,
                model=CLAWBENCH_MODEL, session_key=session_key,
            )
            result["response"] = resp
        except Exception as e:
            result["error"] = str(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Poll for tool calls
    seen = 0
    poll_interval = 0.5
    idle_count = 0

    while thread.is_alive():
        try:
            all_reqs = get_all_requests(MOCK_TOOLS_URL)
            requests = all_reqs.get("requests", [])

            if len(requests) > seen:
                for i in range(seen, len(requests)):
                    req = requests[i]
                    # Build a call-like dict from the request
                    call = {
                        "tool": req.get("tool", "?"),
                        "args": req.get("request_body", {}),
                        "response": {},  # We don't have the response in requests
                    }
                    print(fmt_tool_call(i + 1, call))
                    print()
                seen = len(requests)
                idle_count = 0
            else:
                idle_count += 1
                if idle_count % 10 == 0:
                    elapsed = idle_count * poll_interval
                    print(f"{DIM}  ... waiting ({elapsed:.0f}s, {seen} calls so far){RESET}", end="\r")

        except Exception:
            pass

        time.sleep(poll_interval)

    # Episode complete — get final tool calls with responses
    tool_calls = get_tool_calls(MOCK_TOOLS_URL)
    all_reqs_final = get_all_requests(MOCK_TOOLS_URL)

    print(f"\n{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  TOOL CALL LOG ({len(tool_calls.get('calls', []))} calls){RESET}")
    print(f"{'─' * 70}")

    for i, call in enumerate(tool_calls.get("calls", [])):
        print(fmt_tool_call(i + 1, call))
        print()

    # Show response
    response = result.get("response", {})
    if result.get("error"):
        print(f"\n{RED}{BOLD}ERROR: {result['error']}{RESET}")
        return None

    assistant_message = ""
    if response and "choices" in response:
        assistant_message = response["choices"][0].get("message", {}).get("content", "")

    print(f"{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  AGENT RESPONSE{RESET}")
    print(f"{'─' * 70}")
    print(fmt_response(assistant_message))

    # Usage
    usage = extract_usage(response)
    if not usage or usage.get("total_cost_usd") is None:
        usage = get_session_usage(OPENCLAW_URL, OPENCLAW_TOKEN, session_key)

    if usage:
        cost = usage.get("total_cost_usd", 0)
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        print(f"\n{DIM}  Cost: ${cost:.4f}  Tokens: {inp} in / {out} out{RESET}")

    # Score
    print(f"\n{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  SCORING{RESET}")
    print(f"{'─' * 70}")

    ep_result = {
        "scenario": scenario,
        "response": assistant_message,
        "tool_calls": tool_calls,
        "all_requests": all_reqs_final.get("requests", []),
        "request_summary": all_reqs_final.get("summary", {}),
        "failed_requests": [r for r in all_reqs_final.get("requests", []) if not r.get("success")],
        "raw_response": response,
        "usage": usage,
    }

    scenario_config = yaml.safe_load(
        (SCENARIOS_DIR / f"{scenario}.yaml").read_text()
    )
    checks = scenario_config.get("scoring", {}).get("checks", [])
    score_result = score_episode(ep_result, checks)

    total = score_result.get("total", 0)
    passed = score_result.get("passed", 0)
    failed_checks = [c for c in score_result.get("details", []) if not c.get("passed")]

    gate = "PASS" if passed == total else "FAIL"
    gate_color = GREEN if gate == "PASS" else RED

    print(f"  Checks: {passed}/{total} {gate_color}{BOLD}{gate}{RESET}")
    if failed_checks:
        for c in failed_checks:
            print(f"  {RED}✗ {c.get('name', '?')}: {c.get('reason', '?')}{RESET}")

    print(f"\n{BOLD}{'═' * 70}{RESET}")

    return ep_result


# ---------------------------------------------------------------------------
# Replay mode — step through a saved result file
# ---------------------------------------------------------------------------
def replay_episode(result_path: str):
    """Interactively step through a saved episode result."""
    with open(result_path) as f:
        data = json.load(f)

    scenarios = data.get("scenarios", {})
    if not scenarios:
        print("No scenarios found in result file.")
        return

    for scenario_name, scenario_data in scenarios.items():
        print(f"\n{BOLD}{'═' * 70}{RESET}")
        print(f"{BOLD}  REPLAY: {scenario_name}{RESET}")
        print(f"  Score: {scenario_data.get('score', '?')}  "
              f"Gate: {'PASS' if scenario_data.get('success') else 'FAIL'}  "
              f"Cost: ${scenario_data.get('cost_usd', 0):.4f}")
        print(f"{BOLD}{'═' * 70}{RESET}")

        calls = scenario_data.get("tool_call_log", [])
        if not calls:
            print("  No tool calls recorded.")
            continue

        for i, call in enumerate(calls):
            print(fmt_tool_call(i + 1, call))
            print()

            try:
                cmd = input(f"{DIM}  [Enter]=next  [q]=quit  [d]=detail  > {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if cmd == "q":
                return
            elif cmd == "d":
                print(json.dumps(call, indent=2, default=str))
                input(f"{DIM}  [Enter]=continue > {RESET}")

        response = scenario_data.get("response", "")
        if response:
            print(f"\n{BOLD}AGENT RESPONSE:{RESET}")
            print(fmt_response(response))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Interactive agent debugger for ClawBench episodes"
    )
    parser.add_argument("--scenario", "-s", default="inbox_triage",
                        help="Scenario to run")
    parser.add_argument("--watch", "-w", action="store_true", default=True,
                        help="Live-stream tool calls (default)")
    parser.add_argument("--replay", "-r", type=str,
                        help="Replay a saved result file interactively")
    parser.add_argument("--message", "-m", type=str,
                        help="Custom message (overrides scenario default)")
    parser.add_argument("--agents-md", type=str,
                        help="Custom AGENTS.md to inject into workspace")
    parser.add_argument("--variant", default="optimized",
                        help="AGENTS.md variant (default: optimized)")
    parser.add_argument("--user-context", type=str,
                        help="JSON user context for template substitution")
    parser.add_argument("--wait", action="store_true",
                        help="Wait for services before starting")

    args = parser.parse_args()

    # Replay mode
    if args.replay:
        replay_episode(args.replay)
        return

    # Watch mode
    if args.wait:
        print("Waiting for services...")
        wait_for_services(OPENCLAW_URL, MOCK_TOOLS_URL, timeout=120)

    # Load scenario
    scenario_path = SCENARIOS_DIR / f"{args.scenario}.yaml"
    if not scenario_path.exists():
        print(f"Scenario not found: {args.scenario}")
        sys.exit(1)

    scenario_config = yaml.safe_load(scenario_path.read_text())

    # Setup workspace
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    user_context = None
    if args.user_context:
        user_context = json.loads(args.user_context)

    # Copy AGENTS.md
    if args.agents_md:
        import shutil
        shutil.copy2(args.agents_md, WORKSPACE_DIR / "AGENTS.md")
        print(f"Injected custom AGENTS.md: {args.agents_md}")
    else:
        fixture_dir = FIXTURES_DIR / args.scenario
        variants = scenario_config.get("variants", {})
        if args.variant in variants:
            import shutil
            src = fixture_dir / variants[args.variant]
            if src.exists():
                shutil.copy2(src, WORKSPACE_DIR / "AGENTS.md")

    # Copy workspace files
    fixture_dir = FIXTURES_DIR / args.scenario
    for dest_name, src_name in scenario_config.get("workspace", {}).items():
        src = fixture_dir / src_name
        if src.exists():
            import shutil
            shutil.copy2(src, WORKSPACE_DIR / dest_name)

    # Resolve message
    message = args.message or scenario_config.get("prompt", "Help me with my tasks.")

    # Resolve user context
    ctx = dict(scenario_config.get("user_context_defaults", {}))
    if user_context:
        ctx.update(user_context)

    watch_episode(args.scenario, message, ctx or None)


if __name__ == "__main__":
    main()
