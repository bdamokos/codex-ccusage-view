#!/usr/bin/env python3
"""Correlate ccusage Codex session JSON with Codex Desktop thread titles."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback.
    ZoneInfo = None  # type: ignore[assignment]


UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


@dataclass
class ThreadInfo:
    thread_id: str
    title: str = ""
    rollout_path: str = ""
    cwd: str = ""
    git_branch: str = ""
    git_origin_url: str = ""
    source: str = ""
    first_user_message: str = ""
    preview: str = ""
    agent_nickname: str = ""
    agent_role: str = ""
    created_at: float | None = None
    updated_at: float | None = None
    source_db: str = ""


def sqlite_rows(db_path: Path, query: str) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(query))
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def load_threads(codex_home: Path, state_db: Path | None, catalog_db: Path | None) -> tuple[dict[str, ThreadInfo], dict[str, ThreadInfo]]:
    by_id: dict[str, ThreadInfo] = {}
    by_path: dict[str, ThreadInfo] = {}

    state_db = state_db or codex_home / "state_5.sqlite"
    rows = sqlite_rows(
        state_db,
        """
        SELECT id, rollout_path, title, cwd, git_branch, git_origin_url, source,
               first_user_message, preview, agent_nickname, agent_role,
               created_at, updated_at, created_at_ms, updated_at_ms
        FROM threads
        """,
    )
    for row in rows:
        thread_id = str(row["id"]).lower()
        info = ThreadInfo(
            thread_id=thread_id,
            title=row["title"] or "",
            rollout_path=row["rollout_path"] or "",
            cwd=row["cwd"] or "",
            git_branch=row["git_branch"] or "",
            git_origin_url=row["git_origin_url"] or "",
            source=row["source"] or "",
            first_user_message=row["first_user_message"] or "",
            preview=row["preview"] or "",
            agent_nickname=row["agent_nickname"] or "",
            agent_role=row["agent_role"] or "",
            created_at=millis_or_seconds(row["created_at_ms"], row["created_at"]),
            updated_at=millis_or_seconds(row["updated_at_ms"], row["updated_at"]),
            source_db=str(state_db),
        )
        by_id[thread_id] = info
        if info.rollout_path:
            by_path[str(Path(info.rollout_path).expanduser())] = info

    catalog_db = catalog_db or codex_home / "sqlite" / "codex-dev.db"
    rows = sqlite_rows(
        catalog_db,
        """
        SELECT thread_id, display_title, cwd, source_kind, git_branch,
               source_created_at, source_updated_at
        FROM local_thread_catalog
        """,
    )
    for row in rows:
        thread_id = str(row["thread_id"]).lower()
        if thread_id in by_id:
            existing = by_id[thread_id]
            display_title = row["display_title"] or ""
            if display_title and (not existing.title or is_machine_title(existing.title)):
                existing.title = display_title
            if not existing.cwd and row["cwd"]:
                existing.cwd = row["cwd"]
            if not existing.git_branch and row["git_branch"]:
                existing.git_branch = row["git_branch"]
            if not existing.source and row["source_kind"]:
                existing.source = row["source_kind"]
            continue
        info = ThreadInfo(
            thread_id=thread_id,
            title=row["display_title"] or "",
            cwd=row["cwd"] or "",
            git_branch=row["git_branch"] or "",
            source=row["source_kind"] or "",
            created_at=float(row["source_created_at"]) if row["source_created_at"] else None,
            updated_at=float(row["source_updated_at"]) if row["source_updated_at"] else None,
            source_db=str(catalog_db),
        )
        by_id[thread_id] = info

    return by_id, by_path


def is_machine_title(value: str) -> bool:
    stripped = (value or "").strip()
    return stripped.startswith("<codex_delegation>") or stripped.startswith("{\"subagent\"")


def millis_or_seconds(ms_value: Any, seconds_value: Any) -> float | None:
    if ms_value:
        return float(ms_value) / 1000
    if seconds_value:
        return float(seconds_value)
    return None


def extract_thread_id(session: dict[str, Any]) -> str:
    for key in ("sessionFile", "sessionId"):
        value = str(session.get(key) or "")
        matches = UUID_RE.findall(value)
        if matches:
            return matches[-1].lower()
    return ""


def session_rollout_path(codex_home: Path, session: dict[str, Any]) -> str:
    session_id = str(session.get("sessionId") or "")
    if not session_id:
        return ""
    if session_id.endswith(".jsonl"):
        relative = session_id
    else:
        relative = f"{session_id}.jsonl"
    return str((codex_home / "sessions" / relative).expanduser())


def enrich_usage(usage: dict[str, Any], codex_home: Path, state_db: Path | None, catalog_db: Path | None) -> dict[str, Any]:
    by_id, by_path = load_threads(codex_home, state_db, catalog_db)
    sessions = usage.get("sessions", [])
    if not isinstance(sessions, list):
        raise SystemExit("Expected ccusage JSON with a top-level 'sessions' array.")

    enriched_sessions: list[dict[str, Any]] = []
    matched = 0
    for session in sessions:
        if not isinstance(session, dict):
            continue
        thread_id = extract_thread_id(session)
        rollout_path = session_rollout_path(codex_home, session)
        info = by_path.get(rollout_path) if rollout_path else None
        correlation = "rollout_path" if info else ""
        if info is None and thread_id:
            info = by_id.get(thread_id)
            correlation = "thread_id" if info else ""

        row = dict(session)
        row["codexThreadId"] = thread_id
        row["codexRolloutPath"] = rollout_path
        if info:
            matched += 1
            row["codexTitle"] = decorated_title(info)
            row["codexCwd"] = info.cwd
            row["codexProject"] = project_name(info.cwd)
            row["codexGitBranch"] = info.git_branch
            row["codexGitOriginUrl"] = info.git_origin_url
            row["codexSource"] = info.source
            row["codexFirstUserMessage"] = info.first_user_message
            row["codexPreview"] = info.preview
            row["codexCreatedAt"] = unix_to_iso(info.created_at)
            row["codexUpdatedAt"] = unix_to_iso(info.updated_at)
            row["correlation"] = correlation
        else:
            row["codexTitle"] = ""
            row["codexCwd"] = ""
            row["codexProject"] = ""
            row["codexGitBranch"] = ""
            row["codexGitOriginUrl"] = ""
            row["codexSource"] = ""
            row["codexFirstUserMessage"] = ""
            row["codexPreview"] = ""
            row["codexCreatedAt"] = ""
            row["codexUpdatedAt"] = ""
            row["correlation"] = "unmatched"
        enriched_sessions.append(row)

    output = dict(usage)
    output["sessions"] = enriched_sessions
    output["correlation"] = {
        "matchedSessions": matched,
        "unmatchedSessions": len(enriched_sessions) - matched,
        "source": "Codex Desktop state_5.sqlite threads.rollout_path, fallback codex-dev.db local_thread_catalog.thread_id",
    }
    output["generatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return output


def decorated_title(info: ThreadInfo) -> str:
    title = info.title or first_line(info.first_user_message) or "(untitled)"
    if info.agent_nickname and info.agent_role:
        return f"{title} [{info.agent_nickname}/{info.agent_role}]"
    if info.agent_nickname:
        return f"{title} [{info.agent_nickname}]"
    return title


def project_name(cwd: str) -> str:
    if not cwd:
        return ""
    path = Path(cwd).expanduser()
    parts = path.parts
    if ".codex" in parts and "worktrees" in parts:
        return path.name
    return path.name or str(path)


def first_line(value: str) -> str:
    return " ".join((value or "").strip().splitlines()).strip()


def unix_to_iso(value: float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def parse_json_input(input_path: str | None) -> dict[str, Any]:
    if input_path and input_path != "-":
        raw = Path(input_path).expanduser().read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        raise SystemExit("No JSON input received. Pipe ccusage JSON, pass a file, or use --run-ccusage.")
    data = json.loads(raw)
    if isinstance(data, list):
        return {"sessions": data}
    if not isinstance(data, dict):
        raise SystemExit("Expected ccusage JSON object or session array.")
    return data


def run_ccusage(args: argparse.Namespace) -> dict[str, Any]:
    cmd = shlex.split(args.ccusage_command) + ["codex", "session", "--json"]
    if args.since:
        cmd += ["--since", args.since]
    if args.until:
        cmd += ["--until", args.until]
    if args.timezone:
        cmd += ["--timezone", args.timezone]
    if args.ccusage_arg:
        cmd += args.ccusage_arg

    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        raise SystemExit(proc.returncode)
    return json.loads(proc.stdout)


def sort_sessions(sessions: list[dict[str, Any]], sort_key: str) -> list[dict[str, Any]]:
    if sort_key == "session":
        return sessions
    if sort_key == "recent":
        return sorted(sessions, key=recent_thread_timestamp, reverse=True)
    if sort_key == "last":
        return sorted(sessions, key=lambda row: row.get("lastActivity") or "", reverse=True)
    if sort_key == "title":
        return sorted(sessions, key=lambda row: ((row.get("codexTitle") or "").lower(), (row.get("codexProject") or "").lower()))
    if sort_key == "tokens":
        return sorted(sessions, key=lambda row: float(row.get("totalTokens") or 0), reverse=True)
    return sorted(sessions, key=lambda row: float(row.get("costUSD") or 0), reverse=True)


def recent_thread_timestamp(row: dict[str, Any]) -> float:
    for key in ("codexUpdatedAt", "lastActivity", "codexCreatedAt"):
        timestamp = parse_iso_timestamp(row.get(key))
        if timestamp is not None:
            return timestamp
    return 0


def parse_iso_timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def format_table(data: dict[str, Any], args: argparse.Namespace) -> str:
    sessions = sort_sessions(list(data.get("sessions", [])), args.sort)
    if args.limit:
        sessions = sessions[: args.limit]

    term_width = args.width or 150
    numeric_w = 10
    cost_w = 10
    model_w = 12
    project_w = 18
    fixed_width = project_w + model_w + (numeric_w * 5) + cost_w + 25
    session_w = max(30, min(56, term_width - fixed_width))
    columns = [
        Column("Session", session_w, "left"),
        Column("Project", project_w, "left"),
        Column("Models", model_w, "left"),
        Column("Input", numeric_w, "right"),
        Column("Output", numeric_w, "right"),
        Column("Reasoni…", numeric_w, "right"),
        Column("Cache\nRead", numeric_w, "right"),
        Column("Total\nTokens", numeric_w, "right"),
        Column("Cost\n(USD)", cost_w, "right"),
    ]

    lines = [report_banner(), ""]
    totals = data.get("totals") or {}
    lines.append(table_border(columns, "top"))
    lines.extend(format_header(columns))
    lines.append(table_border(columns, "middle"))
    for row in sessions:
        lines.append(format_table_row(columns, session_cells(row)))
        lines.append(table_border(columns, "middle"))
    lines.append(format_table_row(columns, total_cells(totals)))
    lines.append(table_border(columns, "bottom"))
    return "\n".join(lines)


@dataclass(frozen=True)
class Column:
    label: str
    width: int
    align: str = "left"


def report_banner() -> str:
    title = "Codex Token Usage Report - Session"
    width = 44
    inner = width - 2
    return "\n".join(
        [
            "╭" + "─" * inner + "╮",
            "│" + " " * inner + "│",
            "│" + title.center(inner) + "│",
            "│" + " " * inner + "│",
            "╰" + "─" * inner + "╯",
        ]
    )


def table_border(columns: list[Column], kind: str) -> str:
    chars = {
        "top": ("┌", "┬", "┐"),
        "middle": ("├", "┼", "┤"),
        "bottom": ("└", "┴", "┘"),
    }[kind]
    return chars[0] + chars[1].join("─" * (col.width + 2) for col in columns) + chars[2]


def format_header(columns: list[Column]) -> list[str]:
    labels = [col.label.split("\n") for col in columns]
    height = max(len(label) for label in labels)
    rows = []
    for index in range(height):
        cells = [label[index] if index < len(label) else "" for label in labels]
        rows.append(format_table_row(columns, cells))
    return rows


def format_table_row(columns: list[Column], cells: list[str]) -> str:
    padded = []
    for col, cell in zip(columns, cells):
        value = truncate_cell(cell, col.width)
        if col.align == "right":
            padded.append(value.rjust(col.width))
        else:
            padded.append(value.ljust(col.width))
    return "│ " + " │ ".join(padded) + " │"


def session_cells(row: dict[str, Any]) -> list[str]:
    return [
        row.get("codexTitle") or row.get("sessionId") or "(unmatched)",
        row.get("codexProject") or project_name(row.get("codexCwd") or ""),
        model_summary(row.get("models")),
        int_cell(row.get("inputTokens")),
        int_cell(row.get("outputTokens")),
        int_cell(row.get("reasoningOutputTokens")),
        int_cell(row.get("cacheReadTokens")),
        int_cell(row.get("totalTokens")),
        money_cell(row.get("costUSD")),
    ]


def total_cells(totals: dict[str, Any]) -> list[str]:
    return [
        "Total",
        "",
        "",
        int_cell(totals.get("inputTokens")),
        int_cell(totals.get("outputTokens")),
        int_cell(totals.get("reasoningOutputTokens")),
        int_cell(totals.get("cacheReadTokens")),
        int_cell(totals.get("totalTokens")),
        money_cell(totals.get("costUSD")),
    ]


def model_summary(models: Any) -> str:
    if not isinstance(models, dict) or not models:
        return ""
    names = sorted(str(name) for name in models)
    if len(names) == 1:
        return f"- {names[0]}"
    return f"- {names[0]} +{len(names) - 1}"


def int_cell(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def money_cell(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def truncate_cell(value: Any, width: int) -> str:
    value = " ".join(str(value).split())
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def format_last(value: Any, timezone: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:14]
    if ZoneInfo is not None and timezone:
        try:
            dt = dt.astimezone(ZoneInfo(timezone))
        except Exception:
            dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.strftime("%m-%d %H:%M:%S")


def money(value: Any) -> str:
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def compact_int(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    for suffix in ("", "K", "M", "B"):
        if abs(number) < 1000 or suffix == "B":
            if suffix:
                return f"{number:.1f}{suffix}"
            return str(int(number))
        number /= 1000
    return str(int(number))


def short_path(value: str) -> str:
    if not value:
        return ""
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home) :]
    return value


def truncate(value: str, width: int) -> str:
    value = " ".join(str(value).split())
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "..."


def write_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def write_csv(data: dict[str, Any]) -> str:
    fields = [
        "costUSD",
        "lastActivity",
        "totalTokens",
        "inputTokens",
        "outputTokens",
        "cacheCreationTokens",
        "cacheReadTokens",
        "codexTitle",
        "codexProject",
        "codexCwd",
        "codexGitBranch",
        "codexThreadId",
        "sessionId",
        "correlation",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in sort_sessions(list(data.get("sessions", [])), "cost"):
        writer.writerow(row)
    return output.getvalue().rstrip("\n")


def render(data: dict[str, Any], args: argparse.Namespace) -> str:
    if args.format == "json":
        return write_json(data)
    if args.format == "csv":
        return write_csv(data)
    return format_table(data, args)


def default_since(timezone: str) -> str:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(timezone)).date().isoformat()
        except Exception:
            pass
    return datetime.now().astimezone().date().isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add Codex Desktop thread titles/cwd to ccusage codex session JSON."
    )
    parser.add_argument("input", nargs="?", help="ccusage JSON file, or '-' / omitted for stdin unless --run-ccusage is set.")
    parser.add_argument("--run-ccusage", action="store_true", help="Run ccusage directly instead of reading JSON.")
    parser.add_argument("--ccusage-command", default="npx --yes ccusage@latest", help="Command prefix used with --run-ccusage.")
    parser.add_argument("--ccusage-arg", action="append", default=[], help="Extra argument to pass through to ccusage; repeat as needed.")
    parser.add_argument("--since", help="Passed to ccusage when --run-ccusage is set. Defaults to today for --watch.")
    parser.add_argument("--until", help="Passed to ccusage when --run-ccusage is set.")
    parser.add_argument("--timezone", default="Europe/Brussels", help="Timezone for ccusage and display.")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Refresh repeatedly. Implies --run-ccusage.")
    parser.add_argument("--alt-screen", action="store_true", help="Use the terminal alternate screen in watch mode.")
    parser.add_argument(
        "--no-alt-screen",
        action="store_true",
        help="Deprecated compatibility flag; watch mode uses normal scrollback by default.",
    )
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")
    parser.add_argument("--sort", choices=("session", "recent", "cost", "last", "title", "tokens"), default="session")
    parser.add_argument("--limit", type=int, help="Limit displayed rows.")
    parser.add_argument("--width", type=int, help="Table width; default 150.")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--state-db", help="Override Codex state_5.sqlite path.")
    parser.add_argument("--catalog-db", help="Override Codex codex-dev.db path.")
    args = parser.parse_args()
    if args.watch:
        args.run_ccusage = True
        if not args.since:
            args.since = default_since(args.timezone)
        if args.format != "table":
            raise SystemExit("--watch currently supports --format table only.")
    return args


def once(args: argparse.Namespace) -> str:
    codex_home = Path(args.codex_home).expanduser()
    state_db = Path(args.state_db).expanduser() if args.state_db else None
    catalog_db = Path(args.catalog_db).expanduser() if args.catalog_db else None
    usage = run_ccusage(args) if args.run_ccusage else parse_json_input(args.input)
    enriched = enrich_usage(usage, codex_home, state_db, catalog_db)
    return render(enriched, args)


def write_watch_frame(text: str, args: argparse.Namespace) -> None:
    sys.stdout.write("\033[H\033[J")
    sys.stdout.write(text)
    sys.stdout.write(f"\n\nRefreshing every {args.watch}s. Press Ctrl-C to stop.")
    sys.stdout.flush()


def watch(args: argparse.Namespace) -> int:
    use_alt_screen = sys.stdout.isatty() and args.alt_screen and not args.no_alt_screen
    if use_alt_screen:
        sys.stdout.write("\033[?1049h\033[?25l")
    try:
        while True:
            write_watch_frame(once(args), args)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130
    finally:
        if use_alt_screen:
            sys.stdout.write("\033[?25h\033[?1049l")
        else:
            sys.stdout.write("\n")
        sys.stdout.flush()


def main() -> int:
    args = parse_args()
    if args.watch:
        return watch(args)
    print(once(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
