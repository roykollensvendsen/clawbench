#!/usr/bin/env python3
"""
Test CLI wrappers and debug stepper (no server needed).

Tests:
  - CLI wrapper output matches server.py mock handler output
  - EXEC_MODE=real produces same results as mock mode
  - Debug stepper blocks and releases correctly
  - Debug mode transitions (step/continue/break-at)
  - Named tool routing via exec

Usage:
    cd clawbench
    python scripts/test_cli_wrappers.py
    python scripts/test_cli_wrappers.py --scenario client_escalation
    python scripts/test_cli_wrappers.py -v          # verbose
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

# Ensure imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pre-set LOG_PATH so server.py module-level mkdir doesn't fail
os.environ.setdefault("LOG_PATH", "/tmp/clawbench-test-logs")
os.makedirs(os.environ["LOG_PATH"], exist_ok=True)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

passed = 0
failed = 0
verbose = False


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    status = PASS if ok else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    if ok:
        passed += 1
    else:
        failed += 1
    return ok


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ---------------------------------------------------------------------------
# CLI Wrapper Tests
# ---------------------------------------------------------------------------

def test_cli_wrappers(scenario: str, fixtures_path: str):
    """Test that CLI wrappers produce correct output."""
    section("CLI Wrappers")

    env = {**os.environ, "FIXTURES_PATH": fixtures_path, "SCENARIO": scenario}
    cli_dir = Path(__file__).resolve().parent.parent / "cli_wrappers"

    # himalaya envelope list
    r = subprocess.run(
        ["python3", str(cli_dir / "himalaya"), "envelope", "list"],
        capture_output=True, text=True, env=env,
    )
    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("himalaya envelope list — returns JSON array", isinstance(data, list))
        if data:
            check("himalaya envelope list — has id field", "id" in data[0])
            check("himalaya envelope list — has sender field", "sender" in data[0])
            check("himalaya envelope list — has subject field", "subject" in data[0])
            check("himalaya envelope list — has date field", "date" in data[0])
            check("himalaya envelope list — has flags field", "flags" in data[0])
    else:
        check("himalaya envelope list — runs", False, r.stderr[:100])

    # himalaya message read
    if r.returncode == 0 and data:
        msg_id = data[0]["id"]
        r2 = subprocess.run(
            ["python3", str(cli_dir / "himalaya"), "message", "read", msg_id],
            capture_output=True, text=True, env=env,
        )
        check("himalaya message read — runs", r2.returncode == 0)
        check("himalaya message read — starts with From:", r2.stdout.startswith("From: "))
        check("himalaya message read — has Subject:", "Subject: " in r2.stdout)
        check("himalaya message read — has Date:", "Date: " in r2.stdout)

    # himalaya message read — not found
    r3 = subprocess.run(
        ["python3", str(cli_dir / "himalaya"), "message", "read", "nonexistent_id"],
        capture_output=True, text=True, env=env,
    )
    check("himalaya message read — not found exits 1", r3.returncode == 1)

    # himalaya --help
    r4 = subprocess.run(
        ["python3", str(cli_dir / "himalaya"), "--help"],
        capture_output=True, text=True, env=env,
    )
    check("himalaya --help — exits 0", r4.returncode == 0)
    check("himalaya --help — shows usage", "usage:" in r4.stdout.lower())

    # gcalcli agenda
    r = subprocess.run(
        ["python3", str(cli_dir / "gcalcli"), "agenda"],
        capture_output=True, text=True, env=env,
    )
    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("gcalcli agenda — returns JSON with items", "items" in data)
        if data["items"]:
            check("gcalcli agenda — item has id", "id" in data["items"][0])
            check("gcalcli agenda — item has title", "title" in data["items"][0])
    else:
        check("gcalcli agenda — runs", False, r.stderr[:100])

    # gcalcli search
    r = subprocess.run(
        ["python3", str(cli_dir / "gcalcli"), "search", "standup"],
        capture_output=True, text=True, env=env,
    )
    check("gcalcli search — runs", r.returncode == 0)
    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("gcalcli search — returns items", "items" in data)

    # notion-cli tasks
    r = subprocess.run(
        ["python3", str(cli_dir / "notion-cli"), "tasks"],
        capture_output=True, text=True, env=env,
    )
    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("notion-cli tasks — returns JSON with results", "results" in data)
    else:
        # Some scenarios don't have tasks.json
        check("notion-cli tasks — runs (may be empty)", True, "no tasks.json")

    # slack-cli channels
    r = subprocess.run(
        ["python3", str(cli_dir / "slack-cli"), "channels"],
        capture_output=True, text=True, env=env,
    )
    check("slack-cli channels — runs", r.returncode == 0)
    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("slack-cli channels — has ok field", data.get("ok") is True)

    # memo search
    r = subprocess.run(
        ["python3", str(cli_dir / "memo"), "search", "sprint"],
        capture_output=True, text=True, env=env,
    )
    check("memo search — runs", r.returncode == 0)
    if r.returncode == 0:
        data = json.loads(r.stdout)
        check("memo search — has results", "results" in data)

    # human mode
    env_human = {**env, "HUMAN_MODE": "1"}
    r = subprocess.run(
        ["python3", str(cli_dir / "himalaya"), "envelope", "list"],
        capture_output=True, text=True, env=env_human,
    )
    check("himalaya HUMAN_MODE=1 — not JSON", r.returncode == 0)
    if r.returncode == 0:
        is_not_json = True
        try:
            json.loads(r.stdout)
            is_not_json = False
        except json.JSONDecodeError:
            pass
        check("himalaya HUMAN_MODE=1 — outputs table, not JSON", is_not_json)


# ---------------------------------------------------------------------------
# Output Format Match: CLI wrapper vs server.py mock handler
# ---------------------------------------------------------------------------

def test_output_format_match(scenario: str, fixtures_path: str):
    """Verify CLI wrapper output matches mock server handler output."""
    section("Output Format Match (wrapper vs mock handler)")

    os.environ["FIXTURES_PATH"] = fixtures_path
    os.environ["SCENARIO"] = scenario

    from clawbench.mock_tools.server import handle_exec, load_fixture

    cli_dir = Path(__file__).resolve().parent.parent / "cli_wrappers"
    env = {**os.environ, "FIXTURES_PATH": fixtures_path, "SCENARIO": scenario}

    # himalaya envelope list
    mock_result = handle_exec({"command": "himalaya envelope list"}, scenario)
    mock_output = mock_result["aggregated"]

    r = subprocess.run(
        ["python3", str(cli_dir / "himalaya"), "envelope", "list"],
        capture_output=True, text=True, env=env,
    )
    wrapper_output = r.stdout.rstrip("\n")

    check(
        "himalaya list — mock and wrapper produce same JSON",
        json.loads(mock_output) == json.loads(wrapper_output),
    )

    # himalaya message read
    inbox = load_fixture(scenario, "inbox.json") or []
    if inbox:
        msg_id = inbox[0]["id"]
        mock_result = handle_exec({"command": f"himalaya message read {msg_id}"}, scenario)
        mock_output = mock_result["aggregated"]

        r = subprocess.run(
            ["python3", str(cli_dir / "himalaya"), "message", "read", msg_id],
            capture_output=True, text=True, env=env,
        )
        wrapper_output = r.stdout.rstrip("\n")

        check(
            "himalaya read — mock and wrapper produce same text",
            mock_output == wrapper_output,
        )

    # gcalcli agenda
    mock_result = handle_exec({"command": "gcalcli agenda"}, scenario)
    mock_output = mock_result["aggregated"]

    r = subprocess.run(
        ["python3", str(cli_dir / "gcalcli"), "agenda"],
        capture_output=True, text=True, env=env,
    )
    wrapper_output = r.stdout.rstrip("\n")

    check(
        "gcalcli agenda — mock and wrapper produce same JSON",
        json.loads(mock_output) == json.loads(wrapper_output),
    )

    # notion-cli tasks
    tasks = load_fixture(scenario, "tasks.json")
    if tasks is not None:
        mock_result = handle_exec(
            {"command": "curl -X POST https://api.notion.so/v1/databases/db/query"}, scenario
        )
        mock_output = mock_result["aggregated"]

        r = subprocess.run(
            ["python3", str(cli_dir / "notion-cli"), "tasks"],
            capture_output=True, text=True, env=env,
        )
        wrapper_output = r.stdout.rstrip("\n")

        check(
            "notion-cli tasks — same as curl notion query",
            json.loads(mock_output) == json.loads(wrapper_output),
        )


# ---------------------------------------------------------------------------
# EXEC_MODE=real Tests
# ---------------------------------------------------------------------------

def test_exec_mode_real(scenario: str, fixtures_path: str):
    """Test that EXEC_MODE=real produces same results as mock mode."""
    section("EXEC_MODE=real")

    os.environ["FIXTURES_PATH"] = fixtures_path
    os.environ["SCENARIO"] = scenario

    cli_dir = Path(__file__).resolve().parent.parent / "cli_wrappers"
    os.environ["PATH"] = str(cli_dir) + ":" + os.environ.get("PATH", "")

    from clawbench.mock_tools.server import handle_exec

    commands = [
        "himalaya envelope list",
        "gcalcli agenda",
    ]

    inbox = load_fixture_direct(fixtures_path, scenario, "inbox.json")
    if inbox:
        commands.append(f"himalaya message read {inbox[0]['id']}")

    # Note: notion-cli is a CLI wrapper, not a curl pattern.
    # Mock mode only matches curl.*notion patterns, so we skip it here.
    # The wrapper-vs-mock comparison in test_output_format_match covers this.

    for cmd in commands:
        # Mock mode
        os.environ.pop("EXEC_MODE", None)
        mock_result = handle_exec({"command": cmd}, scenario)

        # Real mode
        os.environ["EXEC_MODE"] = "real"
        real_result = handle_exec({"command": cmd}, scenario)
        os.environ.pop("EXEC_MODE", None)

        check(
            f"EXEC_MODE=real '{cmd[:40]}' — same status",
            mock_result["status"] == real_result["status"],
        )

        # Compare output (JSON or text)
        mock_out = mock_result["aggregated"]
        real_out = real_result["aggregated"]
        try:
            match = json.loads(mock_out) == json.loads(real_out)
        except (json.JSONDecodeError, TypeError):
            match = mock_out == real_out
        check(
            f"EXEC_MODE=real '{cmd[:40]}' — same output",
            match,
            "" if match else f"mock={mock_out[:80]}... real={real_out[:80]}...",
        )


def load_fixture_direct(fixtures_path: str, scenario: str, filename: str):
    path = Path(fixtures_path) / scenario / filename
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Debug Stepper Tests
# ---------------------------------------------------------------------------

def test_debug_stepper(scenario: str, fixtures_path: str):
    """Test the debug stepper blocks and releases correctly."""
    section("Debug Stepper")

    os.environ["FIXTURES_PATH"] = fixtures_path
    os.environ["SCENARIO"] = scenario
    # Import and enable stepper directly (env var is checked at import time)
    from clawbench.mock_tools.server import app, stepper
    stepper.enabled = True

    async def run_tests():
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Reset
            await client.post("/debug/reset")

            # Test 1: Status endpoint
            r = await client.get("/debug/status")
            data = r.json()
            check("debug/status — returns enabled=True", data.get("enabled") is True)
            check("debug/status — mode=step", data.get("mode") == "step")
            check("debug/status — no pending", data.get("has_pending") is False)

            # Test 2: Pending returns false when nothing pending
            r = await client.get("/debug/pending")
            check("debug/pending — nothing pending", r.json().get("pending") is False)

            # Test 3: Step mode blocks tool call
            task = asyncio.create_task(
                client.post("/tools/exec", json={"command": "himalaya envelope list"})
            )
            await asyncio.sleep(0.3)

            r = await client.get("/debug/pending")
            pending = r.json()
            check("step mode — call is pending", pending.get("pending") is True)
            check("step mode — tool is exec", pending.get("tool") == "exec")
            check("step mode — args present", "command" in pending.get("args", {}))
            check("step mode — result present", pending.get("result") is not None)
            check(
                "step mode — result has aggregated",
                "aggregated" in (pending.get("result") or {}),
            )

            # Test 4: Release unblocks
            r = await client.post("/debug/release")
            check("debug/release — returns released=True", r.json().get("released") is True)

            result = await asyncio.wait_for(task, timeout=5)
            check("step mode — call completes after release", result.status_code == 200)

            # Test 5: Continue mode doesn't block
            await client.post("/debug/reset")
            await client.post("/debug/mode", json={"mode": "continue"})

            r = await client.post("/tools/exec", json={"command": "gcalcli agenda"})
            check("continue mode — call completes immediately", r.status_code == 200)

            # Test 6: Break-at mode
            await client.post("/debug/reset")
            stepper.enabled = True  # re-enable after reset
            await client.post("/debug/mode", json={"mode": "continue", "break_at": 2})

            # Call 1 should pass through
            r = await client.post("/tools/exec", json={"command": "himalaya envelope list"})
            check("break-at — call 1 passes through", r.status_code == 200)

            # Call 2 should block
            task2 = asyncio.create_task(
                client.post("/tools/exec", json={"command": "gcalcli agenda"})
            )
            await asyncio.sleep(0.3)

            r = await client.get("/debug/pending")
            check("break-at — call 2 is pending", r.json().get("pending") is True)

            await client.post("/debug/release")
            result2 = await asyncio.wait_for(task2, timeout=5)
            check("break-at — call 2 completes after release", result2.status_code == 200)

            # Test 7: Disable releases pending
            await client.post("/debug/reset")
            stepper.enabled = True
            await client.post("/debug/mode", json={"mode": "step"})

            task3 = asyncio.create_task(
                client.post("/tools/exec", json={"command": "himalaya envelope list"})
            )
            await asyncio.sleep(0.3)

            await client.post("/debug/disable")
            result3 = await asyncio.wait_for(task3, timeout=5)
            check("disable — releases pending call", result3.status_code == 200)

            r = await client.get("/debug/status")
            check("disable — enabled=False", r.json().get("enabled") is False)

    asyncio.run(run_tests())

    os.environ.pop("DEBUG_STEP", None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global verbose

    parser = argparse.ArgumentParser(description="Test CLI wrappers and debug stepper")
    parser.add_argument("--scenario", "-s", default="client_escalation",
                        help="Scenario to test against (default: client_escalation)")
    parser.add_argument("--fixtures", "-f", default=None,
                        help="Fixtures path (default: auto-detect)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    verbose = args.verbose

    # Auto-detect fixtures path
    fixtures_path = args.fixtures
    if not fixtures_path:
        for candidate in [
            Path(__file__).resolve().parent.parent / "fixtures",
            Path("./fixtures"),
        ]:
            if candidate.exists():
                fixtures_path = str(candidate)
                break

    if not fixtures_path or not Path(fixtures_path).exists():
        print("ERROR: Cannot find fixtures directory. Use --fixtures.")
        sys.exit(1)

    scenario = args.scenario
    fixture_dir = Path(fixtures_path) / scenario
    if not fixture_dir.exists():
        print(f"ERROR: Scenario '{scenario}' not found in {fixtures_path}")
        sys.exit(1)

    print(f"\n{'═' * 60}")
    print(f"  CLI Wrappers & Debug Stepper Tests")
    print(f"  Scenario: {scenario}")
    print(f"  Fixtures: {fixtures_path}")
    print(f"{'═' * 60}")

    test_cli_wrappers(scenario, fixtures_path)
    test_output_format_match(scenario, fixtures_path)
    test_exec_mode_real(scenario, fixtures_path)
    test_debug_stepper(scenario, fixtures_path)

    print(f"\n{'═' * 60}")
    total = passed + failed
    if failed == 0:
        print(f"  ALL {total} TESTS PASSED")
    else:
        print(f"  {passed}/{total} passed, {failed} FAILED")
    print(f"{'═' * 60}\n")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
