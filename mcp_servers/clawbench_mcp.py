#!/usr/bin/env python3
"""
ClawBench MCP Server — Expose ClawBench tools via Model Context Protocol.

Provides all ClawBench tool domains as MCP tools so Claude Code (or any
MCP client) can connect directly to the agent's environment.

Usage:
    # Start the MCP server (stdio transport)
    FIXTURES_PATH=./fixtures SCENARIO=client_escalation python mcp_servers/clawbench_mcp.py

    # Connect from Claude Code
    claude --mcp-server "python mcp_servers/clawbench_mcp.py"

Requires: pip install mcp
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HAS_MCP = False
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    HAS_MCP = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------
FIXTURES_PATH = Path(os.environ.get("FIXTURES_PATH", "./fixtures"))
SCENARIO = os.environ.get("SCENARIO", "inbox_triage")


def load_fixture(name: str) -> Any | None:
    path = FIXTURES_PATH / SCENARIO / name
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def email_list() -> str:
    inbox = load_fixture("inbox.json") or []
    summaries = [
        {
            "id": msg.get("id"),
            "sender": msg.get("sender"),
            "subject": msg.get("subject"),
            "date": msg.get("received_ts", ""),
            "flags": msg.get("labels", []),
        }
        for msg in inbox
    ]
    return json.dumps(summaries, indent=2)


def email_read(message_id: str) -> str:
    inbox = load_fixture("inbox.json") or []
    email = next((e for e in inbox if str(e.get("id")) == message_id), None)
    if not email:
        return f"Message not found: {message_id}"
    return (
        f"From: {email.get('sender', '')}\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Date: {email.get('received_ts', '')}\n\n"
        f"{email.get('body', '')}"
    )


def email_send(to: str, subject: str, body: str) -> str:
    return "Message sent successfully"


def calendar_agenda() -> str:
    events = load_fixture("calendar.json") or []
    return json.dumps({"items": events}, indent=2)


def calendar_search(query: str) -> str:
    events = load_fixture("calendar.json") or []
    q = query.lower()
    filtered = [
        e for e in events
        if q in e.get("title", "").lower()
        or q in e.get("location", "").lower()
        or q in e.get("notes", "").lower()
        or q in json.dumps(e.get("attendees", [])).lower()
    ]
    return json.dumps({"items": filtered}, indent=2)


def calendar_create(title: str, when: str, duration: str = "30m") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return json.dumps({"id": f"evt_{ts}", "status": "confirmed"}, indent=2)


def tasks_query(status: str = "", priority: str = "", assignee: str = "") -> str:
    tasks = load_fixture("tasks.json") or []
    if status:
        tasks = [t for t in tasks if t.get("status", "").lower() == status.lower()]
    if priority:
        tasks = [t for t in tasks if t.get("priority", "").lower() == priority.lower()]
    if assignee:
        tasks = [t for t in tasks if t.get("assignee", "").lower() == assignee.lower()]
    return json.dumps({"results": tasks}, indent=2)


def task_get(task_id: str) -> str:
    tasks = load_fixture("tasks.json") or []
    item = next((t for t in tasks if str(t.get("id")) == task_id), None)
    if not item:
        docs = load_fixture("documents.json") or []
        item = next((d for d in docs if str(d.get("id")) == task_id), None)
    if not item:
        return f"Page not found: {task_id}"
    return json.dumps(item, indent=2)


def task_create(title: str, priority: str = "", assignee: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return json.dumps({"id": f"page_{ts}", "status": "created"}, indent=2)


def slack_channels() -> str:
    channels = load_fixture("slack_channels.json") or []
    return json.dumps({"ok": True, "channels": channels}, indent=2)


def slack_read(channel: str, limit: int = 50) -> str:
    messages = load_fixture("slack_messages.json") or []
    channels = load_fixture("slack_channels.json") or []
    ch = channel.lstrip("#")
    resolved = {ch}
    for c in channels:
        if c.get("id", "").lstrip("#") == ch or c.get("name", "").lstrip("#") == ch:
            resolved.add(c.get("name", "").lstrip("#"))
            resolved.add(c.get("id", "").lstrip("#"))
    filtered = [
        m for m in messages
        if m.get("channel", "").lstrip("#") in resolved
        or m.get("channelId", "").lstrip("#") in resolved
    ][:limit]
    return json.dumps({"ok": True, "messages": filtered}, indent=2)


def slack_send(to: str, message: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return json.dumps({
        "ok": True,
        "messageId": f"slack_msg_{ts}",
        "to": to,
        "content": message,
        "warning": "IRREVERSIBLE: message sent",
    }, indent=2)


def memory_search(query: str, max_results: int = 5) -> str:
    results = []
    base = FIXTURES_PATH / SCENARIO

    memory_dir = base / "memory"
    if memory_dir.exists():
        for fpath in sorted(memory_dir.iterdir()):
            if fpath.is_file():
                content = fpath.read_text()
                lines = content.split("\n")
                for i, line in enumerate(lines):
                    if any(word in line.lower() for word in query.lower().split()):
                        start = max(0, i - 1)
                        end = min(len(lines), i + 3)
                        snippet = "\n".join(lines[start:end])
                        rel_path = f"memory/{fpath.name}"
                        results.append({
                            "snippet": snippet,
                            "path": rel_path,
                            "startLine": start + 1,
                            "endLine": end,
                            "score": 0.85,
                            "citation": f"{rel_path}#L{start + 1}-L{end}",
                        })
                        if len(results) >= max_results:
                            break
            if len(results) >= max_results:
                break

    return json.dumps({"results": results, "provider": "mock", "citations": "on"}, indent=2)


def memory_get(path: str) -> str:
    base = FIXTURES_PATH / SCENARIO
    for fpath in [base / path, base / "memory" / path]:
        if fpath.exists() and fpath.is_file():
            return json.dumps({"path": path, "text": fpath.read_text()}, indent=2)
    return json.dumps({"path": path, "text": "", "error": f"File not found: {path}"}, indent=2)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

if not HAS_MCP:
    print("MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

server = Server("clawbench")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="email_list",
            description="List all emails in the inbox",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="email_read",
            description="Read a specific email by ID",
            inputSchema={
                "type": "object",
                "properties": {"message_id": {"type": "string", "description": "Email message ID"}},
                "required": ["message_id"],
            },
        ),
        Tool(
            name="email_send",
            description="Send an email (IRREVERSIBLE)",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        ),
        Tool(
            name="calendar_agenda",
            description="List upcoming calendar events",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="calendar_search",
            description="Search calendar events by keyword",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        Tool(
            name="calendar_create",
            description="Create a calendar event (IRREVERSIBLE)",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"}, "when": {"type": "string"}, "duration": {"type": "string"},
                },
                "required": ["title", "when"],
            },
        ),
        Tool(
            name="tasks_query",
            description="List tasks with optional filters",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"}, "priority": {"type": "string"}, "assignee": {"type": "string"},
                },
            },
        ),
        Tool(
            name="task_get",
            description="Get full detail for a task or document by ID",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="task_create",
            description="Create a new task (IRREVERSIBLE)",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"}, "priority": {"type": "string"}, "assignee": {"type": "string"},
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="slack_channels",
            description="List available Slack channels",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="slack_read",
            description="Read messages from a Slack channel",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "limit": {"type": "integer", "description": "Max messages"},
                },
                "required": ["channel"],
            },
        ),
        Tool(
            name="slack_send",
            description="Send a Slack message (IRREVERSIBLE)",
            inputSchema={
                "type": "object",
                "properties": {"to": {"type": "string"}, "message": {"type": "string"}},
                "required": ["to", "message"],
            },
        ),
        Tool(
            name="memory_search",
            description="Search memory files for matching content",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"}, "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_get",
            description="Read a specific memory file",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handlers = {
        "email_list": lambda: email_list(),
        "email_read": lambda: email_read(arguments["message_id"]),
        "email_send": lambda: email_send(arguments["to"], arguments["subject"], arguments["body"]),
        "calendar_agenda": lambda: calendar_agenda(),
        "calendar_search": lambda: calendar_search(arguments["query"]),
        "calendar_create": lambda: calendar_create(
            arguments["title"], arguments["when"], arguments.get("duration", "30m")
        ),
        "tasks_query": lambda: tasks_query(
            arguments.get("status", ""), arguments.get("priority", ""), arguments.get("assignee", "")
        ),
        "task_get": lambda: task_get(arguments["task_id"]),
        "task_create": lambda: task_create(
            arguments["title"], arguments.get("priority", ""), arguments.get("assignee", "")
        ),
        "slack_channels": lambda: slack_channels(),
        "slack_read": lambda: slack_read(arguments["channel"], arguments.get("limit", 50)),
        "slack_send": lambda: slack_send(arguments["to"], arguments["message"]),
        "memory_search": lambda: memory_search(arguments["query"], arguments.get("max_results", 5)),
        "memory_get": lambda: memory_get(arguments["path"]),
    }

    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    result = handler()
    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
