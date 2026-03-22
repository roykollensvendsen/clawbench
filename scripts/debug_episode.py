#!/usr/bin/env python3
"""
debug_episode.py — Interactive agent stepping debugger for ClawBench.

Runs an episode with tool-call-level stepping. Each tool call pauses
execution so you can inspect arguments and results before continuing.

Requires DEBUG_STEP=1 on the mock-tools server.

Modes:
  --step      Pause at every tool call (default)
  --watch     Live-stream without pausing
  --replay    Step through a saved result file

Usage:
    # Step through an episode (start services with DEBUG_STEP=1 first)
    python scripts/debug_episode.py -s client_escalation

    # Watch without pausing
    python scripts/debug_episode.py -s client_escalation --watch

    # With custom AGENTS.md
    python scripts/debug_episode.py -s inbox_triage --agents-md /path/to/AGENTS.md

    # Replay a saved result
    python scripts/debug_episode.py --replay results.json
"""

import argparse
import json
import os
import shutil
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
# ANSI
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
    "exec": CYAN, "slack": MAGENTA,
    "memory_search": YELLOW, "memory_get": YELLOW,
    "read": BLUE, "web_search": DIM, "web_fetch": DIM,
    "himalaya": CYAN, "gcalcli": GREEN, "notion_cli": BLUE,
    "slack_cli": MAGENTA, "memo": YELLOW,
}


def fmt_tool_call(call: dict, show_result: bool = True) -> str:
    """Format a tool call for terminal display."""
    idx = call.get("index", "?")
    tool = call.get("tool", "?")
    args = call.get("args", {})
    color = TOOL_COLORS.get(tool, "")

    lines = [f"{BOLD}{color}[{idx}] {tool}{RESET}"]

    # Format args based on tool type
    if tool == "exec":
        lines.append(f"  {DIM}$ {RESET}{args.get('command', '')}")
    elif tool == "slack":
        action = args.get("action", "")
        ch = args.get("channelId", args.get("to", ""))
        lines.append(f"  {DIM}action={RESET}{action} {DIM}channel={RESET}{ch}")
    elif tool in ("memory_search",):
        lines.append(f"  {DIM}query={RESET}{args.get('query', '')}")
    elif tool == "read":
        lines.append(f"  {DIM}path={RESET}{args.get('path', '')}")
    else:
        s = json.dumps(args, default=str)
        if len(s) > 120:
            s = s[:117] + "..."
        lines.append(f"  {DIM}{s}{RESET}")

    if show_result:
        result = call.get("result", call.get("response", {}))
        result_str = call.get("result_summary", json.dumps(result, default=str))
        if len(result_str) > 300:
            result_str = result_str[:297] + "..."
        lines.append(f"  {DIM}→ {result_str}{RESET}")

        # Flag irreversible
        if isinstance(result, dict) and result.get("_irreversible"):
            lines.append(f"  {RED}{BOLD}⚠ IRREVERSIBLE{RESET}")

    return "\n".join(lines)


def fmt_response(text: str, max_lines: int = 50) -> str:
    lines = text.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"{DIM}... ({len(lines) - max_lines} more lines){RESET}"]
    return "\n".join(f"  {line}" for line in lines)


def debug_api(method: str, path: str, body: dict | None = None) -> dict:
    """Call the mock server debug API."""
    url = f"{MOCK_TOOLS_URL}{path}"
    try:
        if method == "GET":
            r = httpx.get(url, timeout=5)
        else:
            r = httpx.post(url, json=body or {}, timeout=5)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Step mode — true interactive stepping
# ---------------------------------------------------------------------------
def run_stepping(scenario: str, message: str, user_context: dict | None = None):
    """Run an episode with step-through debugging."""

    # Check debug stepping is enabled
    status = debug_api("GET", "/debug/status")
    if not status.get("enabled"):
        print(f"{YELLOW}Debug stepping not enabled on mock server.{RESET}")
        print(f"Enabling via /debug/enable...")
        debug_api("POST", "/debug/enable")
        status = debug_api("GET", "/debug/status")
        if not status.get("enabled"):
            print(f"{RED}Failed to enable. Start mock server with DEBUG_STEP=1{RESET}")
            return None

    # Reset
    debug_api("POST", "/debug/reset")
    reset_scenario(MOCK_TOOLS_URL, scenario)
    if user_context:
        httpx.post(f"{MOCK_TOOLS_URL}/set_user_context", json=user_context, timeout=5)

    session_key = f"debug-{scenario}-{int(time.time() * 1000)}"

    print(f"\n{BOLD}{'═' * 70}{RESET}")
    print(f"{BOLD}  STEP-THROUGH DEBUG: {scenario}{RESET}")
    print(f"  Model: {CLAWBENCH_MODEL}")
    print(f"  Controls: [Enter]=step  [c]=continue  [b N]=break at N  [d]=detail  [q]=quit")
    print(f"{BOLD}{'═' * 70}{RESET}\n")

    # Run episode in background
    result = {"response": None, "error": None, "done": False}

    def run():
        try:
            resp = send_message(
                OPENCLAW_URL, OPENCLAW_TOKEN, message,
                model=CLAWBENCH_MODEL, session_key=session_key,
            )
            result["response"] = resp
        except Exception as e:
            result["error"] = str(e)
        result["done"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Interactive stepping loop
    continuing = False
    try:
        while not result["done"]:
            # Poll for pending call
            pending = debug_api("GET", "/debug/pending")

            if not pending.get("pending"):
                if result["done"]:
                    break
                time.sleep(0.3)
                continue

            # Show the pending call
            print(fmt_tool_call(pending))

            if continuing:
                # In continue mode, auto-release
                debug_api("POST", "/debug/release")
                time.sleep(0.05)
                continue

            # Interactive prompt
            while True:
                try:
                    cmd = input(f"\n  {BOLD}[s]tep [c]ontinue [b N]reak [d]etail [q]uit >{RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    cmd = "q"

                if cmd == "" or cmd == "s":
                    debug_api("POST", "/debug/release")
                    break
                elif cmd == "c":
                    debug_api("POST", "/debug/mode", {"mode": "continue"})
                    debug_api("POST", "/debug/release")
                    continuing = True
                    break
                elif cmd.startswith("b"):
                    parts = cmd.split()
                    if len(parts) >= 2:
                        try:
                            n = int(parts[1])
                            debug_api("POST", "/debug/mode", {"mode": "continue", "break_at": n})
                            debug_api("POST", "/debug/release")
                            continuing = True
                            print(f"  {DIM}Continuing until call #{n}...{RESET}")
                            break
                        except ValueError:
                            print(f"  {RED}Usage: b <number>{RESET}")
                    else:
                        print(f"  {RED}Usage: b <number>{RESET}")
                elif cmd == "d":
                    # Show full detail
                    full = debug_api("GET", "/debug/pending")
                    if full.get("result"):
                        print(f"\n{BOLD}  Full result:{RESET}")
                        print(json.dumps(full["result"], indent=2, default=str))
                elif cmd == "q":
                    debug_api("POST", "/debug/disable")
                    print(f"\n{YELLOW}Quitting — releasing all remaining calls...{RESET}")
                    thread.join(timeout=30)
                    return None
                else:
                    print(f"  {DIM}Commands: [Enter/s]=step [c]=continue [b N]=break at call N [d]=detail [q]=quit{RESET}")

            print()

    except KeyboardInterrupt:
        debug_api("POST", "/debug/disable")
        print(f"\n{YELLOW}Interrupted — releasing remaining calls...{RESET}")

    # Wait for episode to finish
    thread.join(timeout=60)

    # Disable stepping for clean state
    debug_api("POST", "/debug/disable")

    # Show results
    tool_calls = get_tool_calls(MOCK_TOOLS_URL)
    all_reqs = get_all_requests(MOCK_TOOLS_URL)

    # Agent response
    response = result.get("response", {})
    assistant_message = ""
    if response and "choices" in response:
        assistant_message = response["choices"][0].get("message", {}).get("content", "")

    print(f"\n{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  AGENT RESPONSE{RESET}")
    print(f"{'─' * 70}")
    print(fmt_response(assistant_message))

    # Usage
    usage = extract_usage(response) if response else None
    if not usage or (usage and usage.get("total_cost_usd") is None):
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

    scenario_config = yaml.safe_load((SCENARIOS_DIR / f"{scenario}.yaml").read_text())
    checks = scenario_config.get("scoring", {}).get("checks", [])

    ep_result = {
        "scenario": scenario,
        "response": assistant_message,
        "tool_calls": tool_calls,
        "all_requests": all_reqs.get("requests", []),
        "request_summary": all_reqs.get("summary", {}),
        "failed_requests": [r for r in all_reqs.get("requests", []) if not r.get("success")],
        "raw_response": response,
        "usage": usage,
    }

    score_result = score_episode(ep_result, checks)
    total = score_result.get("total", 0)
    passed = score_result.get("passed", 0)
    gate = "PASS" if passed == total else "FAIL"
    gate_color = GREEN if gate == "PASS" else RED

    print(f"  Checks: {passed}/{total} {gate_color}{BOLD}{gate}{RESET}")
    for c in score_result.get("details", []):
        mark = f"{GREEN}✓{RESET}" if c.get("passed") else f"{RED}✗{RESET}"
        print(f"  {mark} {c.get('name', '?')}")

    print(f"\n{BOLD}{'═' * 70}{RESET}")
    return ep_result


# ---------------------------------------------------------------------------
# Watch mode — live stream without pausing
# ---------------------------------------------------------------------------
def run_watch(scenario: str, message: str, user_context: dict | None = None):
    """Run an episode and live-stream tool calls without pausing."""
    # Make sure debug stepping is disabled
    debug_api("POST", "/debug/disable")

    reset_scenario(MOCK_TOOLS_URL, scenario)
    if user_context:
        httpx.post(f"{MOCK_TOOLS_URL}/set_user_context", json=user_context, timeout=5)

    session_key = f"debug-{scenario}-{int(time.time() * 1000)}"

    print(f"\n{BOLD}{'═' * 70}{RESET}")
    print(f"{BOLD}  WATCH MODE: {scenario}{RESET}")
    print(f"  Model: {CLAWBENCH_MODEL}")
    print(f"{BOLD}{'═' * 70}{RESET}\n")

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

    seen = 0
    while thread.is_alive():
        try:
            calls = get_tool_calls(MOCK_TOOLS_URL)
            call_list = calls.get("calls", [])
            if len(call_list) > seen:
                for i in range(seen, len(call_list)):
                    call = call_list[i]
                    call["index"] = i + 1
                    print(fmt_tool_call(call))
                    print()
                seen = len(call_list)
        except Exception:
            pass
        time.sleep(0.5)

    # Final check
    calls = get_tool_calls(MOCK_TOOLS_URL)
    call_list = calls.get("calls", [])
    for i in range(seen, len(call_list)):
        call = call_list[i]
        call["index"] = i + 1
        print(fmt_tool_call(call))
        print()

    response = result.get("response", {})
    assistant_message = ""
    if response and "choices" in response:
        assistant_message = response["choices"][0].get("message", {}).get("content", "")

    print(f"\n{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  AGENT RESPONSE{RESET}")
    print(f"{'─' * 70}")
    print(fmt_response(assistant_message))
    print(f"\n{BOLD}{'═' * 70}{RESET}")


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------
def run_replay(result_path: str):
    """Step through a saved episode result interactively."""
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
            call["index"] = i + 1
            print(fmt_tool_call(call))

            try:
                cmd = input(f"\n  {DIM}[Enter]=next [d]=detail [q]=quit >{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if cmd == "q":
                return
            elif cmd == "d":
                print(json.dumps(call, indent=2, default=str))
                input(f"  {DIM}[Enter]=continue >{RESET} ")
            print()

        response = scenario_data.get("response", "")
        if response:
            print(f"\n{BOLD}AGENT RESPONSE:{RESET}")
            print(fmt_response(response))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Interactive agent debugger for ClawBench episodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Controls during step mode:
              Enter / s   Step — release current call, pause at next
              c           Continue — run remaining calls without pausing
              b N         Break — continue until call #N, then pause
              d           Detail — show full result JSON
              q           Quit — release all remaining calls and exit
        """),
    )
    parser.add_argument("--scenario", "-s", default="inbox_triage",
                        help="Scenario to run")
    parser.add_argument("--watch", action="store_true",
                        help="Watch mode — stream without pausing")
    parser.add_argument("--replay", "-r", type=str,
                        help="Replay a saved result file")
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

    if args.replay:
        run_replay(args.replay)
        return

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
        shutil.copy2(args.agents_md, WORKSPACE_DIR / "AGENTS.md")
        print(f"Injected custom AGENTS.md: {args.agents_md}")
    else:
        fixture_dir = FIXTURES_DIR / args.scenario
        variants = scenario_config.get("variants", {})
        if args.variant in variants:
            src = fixture_dir / variants[args.variant]
            if src.exists():
                shutil.copy2(src, WORKSPACE_DIR / "AGENTS.md")

    # Copy workspace files
    fixture_dir = FIXTURES_DIR / args.scenario
    for dest_name, src_name in scenario_config.get("workspace", {}).items():
        src = fixture_dir / src_name
        if src.exists():
            shutil.copy2(src, WORKSPACE_DIR / dest_name)

    # Resolve message
    message = args.message or scenario_config.get("prompt", "Help me with my tasks.")

    # Resolve user context
    ctx = dict(scenario_config.get("user_context_defaults", {}))
    if user_context:
        ctx.update(user_context)

    if args.watch:
        run_watch(args.scenario, message, ctx or None)
    else:
        run_stepping(args.scenario, message, ctx or None)


if __name__ == "__main__":
    main()
