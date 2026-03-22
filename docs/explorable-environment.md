# Explorable Agent Environment

ClawBench includes an explorable environment layer that lets humans and AI tools interact with the same simulated office the agent uses. Instead of opaque HTTP pattern-matching, you get real CLI commands with `--help`, an interactive debugger, and MCP server support.

## Quick Start

```bash
# Build the image
docker build -f Dockerfile.mock-tools -t clawbench-mock-tools .

# Explore as a human
docker run --rm -it \
  -e SCENARIO=client_escalation \
  -e FIXTURES_PATH=/app/fixtures \
  -v ./fixtures:/app/fixtures:ro \
  clawbench-mock-tools bash

# Inside the container:
himalaya --help
himalaya envelope list
himalaya message read msg_101
gcalcli agenda
notion-cli tasks
slack-cli channels
slack-cli read platform-engineering
memo search "sprint goals"
```

## CLI Wrappers

Five Python CLI tools that read fixture files and produce the same output format as the mock server's exec handler.

| Tool | Purpose | Key Commands |
|------|---------|-------------|
| `himalaya` | Email | `envelope list`, `message read <id>`, `message send --to X --subject Y --body Z`, `flag add <id> <flag>` |
| `gcalcli` | Calendar | `agenda`, `list`, `search <query>`, `add --title X --when Y`, `delete <id>` |
| `notion-cli` | Tasks | `tasks [--status X] [--priority X] [--assignee X]`, `task <id>`, `create --title X`, `update <id> --status X`, `docs` |
| `slack-cli` | Slack | `channels`, `read <channel> [--limit N]`, `send --to X --message Y`, `member <user_id>` |
| `memo` | Memory | `search <query> [--max-results N]`, `read <path>` |

### Output Modes

- **JSON (default)**: Agent-compatible, matches server.py output exactly
- **Human-readable**: Use `--human` flag or `HUMAN_MODE=1` env var

```bash
# JSON (what agents see)
himalaya envelope list

# Pretty table
himalaya --human envelope list
# or
HUMAN_MODE=1 himalaya envelope list
```

## EXEC_MODE=real

By default, the mock server pattern-matches command strings against fixtures. With `EXEC_MODE=real`, it delegates to actual subprocess execution of the CLI wrappers.

```bash
# In docker-compose or env:
EXEC_MODE=real
```

This means the CLI wrappers handle commands as real processes. The agent path is unchanged — exec tool calls still go through the mock server, which now subprocesses the wrappers instead of regex-matching.

## Named Tool Mode (toolMode=named)

The OpenClaw plugin supports replacing the general `exec` tool with scoped CLI tools. When `toolMode=named` is set in the plugin config:

- `exec` and `read` tools are **removed**
- `himalaya`, `gcalcli`, `notion_cli`, `slack_cli`, `memo` are registered as individual tools
- Each takes an `args` string parameter (the CLI arguments)
- The agent cannot run arbitrary shell commands

```json
// openclaw.json plugin config
{
  "clawbench-tools": {
    "config": {
      "mockServerUrl": "http://localhost:3001",
      "toolMode": "named"
    }
  }
}
```

The agent sees tools like:
```
himalaya(args="envelope list")
gcalcli(args="agenda")
notion_cli(args="tasks --status in_progress")
```

## Environment-Enforced Security

The Docker image includes a restricted `agent` user and `/app/tools` directory. When the agent process runs with `PATH=/app/tools`, it can only execute approved wrappers:

```bash
# As agent user (restricted PATH)
himalaya envelope list    # ✓ Works
gcalcli agenda            # ✓ Works
cat /app/fixtures/...     # ✗ command not found
curl http://evil.com      # ✗ command not found
ls /                      # ✗ command not found
python3 -c "..."          # ✗ command not found
bash -c "..."             # ✗ command not found
```

This replaces policy-based access control (hoping the agent obeys AGENTS.md) with OS-level enforcement.

## Interactive Debugger

`scripts/debug_episode.py` streams tool calls in real time during episode execution, replacing the cycle "edit policy → run 10 min eval → read log → guess".

### Watch Mode (default)

```bash
# Start services first (docker compose up)
python scripts/debug_episode.py --scenario client_escalation --wait
```

Shows each tool call with colored output as it happens, then scores the episode.

### With Custom AGENTS.md

```bash
python scripts/debug_episode.py \
  --scenario inbox_triage \
  --agents-md /path/to/my/AGENTS.md
```

### Replay Mode

Step through a saved evaluation result interactively:

```bash
python scripts/debug_episode.py --replay packs/eval-results/run-1-seed201.json
```

Use `[Enter]` to advance, `[d]` for detail, `[q]` to quit.

## MCP Server

Connect Claude Code (or any MCP client) directly to the agent's environment.

```bash
# Install MCP SDK
pip install mcp

# Start the MCP server
FIXTURES_PATH=./fixtures SCENARIO=client_escalation \
  python mcp_servers/clawbench_mcp.py
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `email_list` | List all inbox emails |
| `email_read` | Read a specific email by ID |
| `email_send` | Send an email (irreversible) |
| `calendar_agenda` | List upcoming events |
| `calendar_search` | Search events by keyword |
| `calendar_create` | Create an event (irreversible) |
| `tasks_query` | List tasks with filters |
| `task_get` | Get task/document detail |
| `task_create` | Create a task (irreversible) |
| `slack_channels` | List Slack channels |
| `slack_read` | Read channel messages |
| `slack_send` | Send a Slack message (irreversible) |
| `memory_search` | Search memory files |
| `memory_get` | Read a memory file |

### Connect from Claude Code

```bash
claude --mcp-server "FIXTURES_PATH=./fixtures SCENARIO=client_escalation python mcp_servers/clawbench_mcp.py"
```

Then use tools naturally:
```
> List emails in the inbox
> Read message msg_101
> What's on the calendar today?
> Search tasks with critical priority
```

## --help Discovery for AGENTS.md

With CLI wrappers available, AGENTS.md can be dramatically simplified. Instead of documenting every command syntax, agents discover tools at runtime:

```markdown
## Available Tools
- `himalaya` — Email client. Run `himalaya --help` for usage.
- `gcalcli` — Calendar. Run `gcalcli --help` for usage.
- `notion-cli` — Task board. Run `notion-cli --help` for usage.
- `slack-cli` — Slack. Run `slack-cli --help` for usage.
- `memo` — Memory search. Run `memo --help` for usage.
```

This reduces AGENTS.md from ~15K chars to ~1.8K chars while scoring 1.000 on all 5 scenarios with GLM-5-TEE.

## File Layout

```
cli_wrappers/           # CLI wrappers (copied to /usr/local/bin in Docker)
  himalaya
  gcalcli
  notion-cli
  slack-cli
  memo
mcp_servers/            # MCP server
  clawbench_mcp.py
scripts/
  debug_episode.py      # Interactive debugger
  run_episode.py        # Standard episode runner
clawbench/mock_tools/
  server.py             # Mock server (EXEC_MODE=real support)
Dockerfile.mock-tools   # Container with agent user + PATH control
```
