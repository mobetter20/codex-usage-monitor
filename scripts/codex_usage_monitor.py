#!/usr/bin/env python3
"""Monitor local Codex usage patterns and surface practical usage signals."""

from __future__ import annotations

import argparse
import http.server
import html
import json
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_RATINGS_PATH = Path("tmp/codex_usage/ratings.jsonl")
DEFAULT_MARKDOWN_PATH = Path("tmp/codex_usage/latest-report.md")
DEFAULT_JSON_PATH = Path("tmp/codex_usage/latest-report.json")
DEFAULT_HTML_PATH = Path("tmp/codex_usage/dashboard.html")
DEFAULT_REPORT_MODE = "hybrid"

ERROR_TERMS = (
    "failed",
    "error",
    "unable",
    "could not",
    "couldn't",
    "not able",
    "blocked",
    "permission denied",
    "traceback",
)

REVIEW_TERMS = ("review", "critique", "assess", "qa", "research")
OPS_TERMS = ("run ", "check", "publish", "brief", "snapshot", "status", "compile", "log ")
BUILD_TERMS = (
    "build",
    "create",
    "tool",
    "app",
    "system",
    "dashboard",
    "monitor",
    "calendar",
    "workflow",
)

SHARED_TOP_LEVEL = {"scripts", "docs", "tmp", "Journal", ".github"}
ROOT_SHARED_FILES = {"README.md", "WORKLOG.md", ".gitignore"}
VERIFICATION_TERMS = ("pytest", "unittest", "py_compile", "npm test", "cargo test", "go test", "ruff", "mypy")
WRITE_COMMAND_TERMS = ("mkdir ", "cat >", "cp ", "mv ", "touch ", "tee ", "apply_patch", "write_text", "write_json")
PATHISH_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".jsonl",
    ".html",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".db",
    ".sqlite",
    ".ics",
    ".sh",
    ".csv",
}
ROOTISH_SEGMENTS = {"Users", "private", "tmp", "var", "opt", "usr", "etc", "dev", "Volumes"}
DEFAULT_SERVE_PORT = 8765

PROJECT_LABELS = {
    "workspace:shared": "Shared Workspace",
    "workspace:root": "Root-Level Or Conceptual",
    "multi-project": "Multiple Projects",
    "unknown": "Unclear Project",
}

TASK_BUCKET_LABELS = {
    "review": "Review / Analysis",
    "build": "Build / Implementation",
    "ops": "Ops / Run",
    "automation": "Automation",
    "general": "General",
}


def detect_default_timezone() -> str:
    local_tz = datetime.now().astimezone().tzinfo
    candidate = getattr(local_tz, "key", None) or str(local_tz or "")
    if candidate:
        try:
            ZoneInfo(candidate)
            return candidate
        except Exception:
            pass
    return "UTC"


DEFAULT_TIMEZONE = detect_default_timezone()


@dataclass
class RatingRecord:
    session_id: str
    score: int
    outcome: str
    notes: str
    recorded_at: str


@dataclass
class SessionSummary:
    session_id: str
    title: str
    task_bucket: str
    created_at: datetime
    updated_at: datetime
    duration_minutes: float
    archived: bool
    rollout_path: str | None
    model: str
    reasoning_effort: str
    substantive_user_messages: int
    user_chars: int
    initial_prompt_chars: int
    task_completions: int
    commentary_messages: int
    reasoning_notes: int
    tool_counts: dict[str, int]
    total_tools: int
    total_tokens: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    primary_used_percent_max: float | None
    secondary_used_percent_max: float | None
    plan_type: str | None
    inferred_project: str
    project_confidence: str
    project_path_refs: int
    project_dominance: float
    workspace_path_counts: dict[str, int]
    dominant_paths: list[str]
    verification_commands: int
    write_actions: int
    final_answer_chars: int
    final_answer_snippet: str
    error_signal: bool
    proxy_quality: float
    manual_rating: int | None
    manual_outcome: str
    manual_notes: str

    @property
    def planning_used(self) -> bool:
        return self.tool_counts.get("update_plan", 0) > 0

    @property
    def agent_used(self) -> bool:
        return self.tool_counts.get("spawn_agent", 0) > 0

    @property
    def apply_patch_used(self) -> bool:
        return self.tool_counts.get("apply_patch", 0) > 0

    @property
    def quality_score(self) -> float:
        if self.manual_rating is not None:
            return float(self.manual_rating)
        return self.proxy_quality

    @property
    def quality_source(self) -> str:
        return "manual" if self.manual_rating is not None else "proxy"

    @property
    def is_complete(self) -> bool:
        return self.task_completions > 0 or self.archived

    def export_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat()
        payload["quality_score"] = self.quality_score
        payload["quality_source"] = self.quality_source
        payload["planning_used"] = self.planning_used
        payload["agent_used"] = self.agent_used
        payload["apply_patch_used"] = self.apply_patch_used
        return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor local Codex usage patterns and output signals."
    )
    parser.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME))
    parser.add_argument("--ratings-path", default=str(DEFAULT_RATINGS_PATH))
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--workspace-root", default=".")

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List recent completed sessions.")
    add_common_filters(list_parser)
    list_parser.add_argument("--unrated", action="store_true")

    rate_parser = subparsers.add_parser("rate", help="Attach a manual quality score to a session.")
    add_common_filters(rate_parser)
    rate_parser.add_argument("--session")
    rate_parser.add_argument("--score", type=int)
    rate_parser.add_argument("--outcome", default="kept")
    rate_parser.add_argument("--notes", default="")
    rate_parser.add_argument("--latest", type=int, default=8)

    report_parser = subparsers.add_parser("report", help="Build a Markdown and JSON advice report.")
    add_common_filters(report_parser)
    report_parser.add_argument("--markdown-path", default=str(DEFAULT_MARKDOWN_PATH))
    report_parser.add_argument("--json-path", default=str(DEFAULT_JSON_PATH))
    report_parser.add_argument("--html-path", default=str(DEFAULT_HTML_PATH))
    report_parser.add_argument("--recent-limit", type=int, default=10)
    report_parser.add_argument("--mode", choices=["basic", "hybrid", "inferred"], default=DEFAULT_REPORT_MODE)

    serve_parser = subparsers.add_parser("serve", help="Generate browser dashboards and serve them on the local network.")
    add_common_filters(serve_parser)
    serve_parser.add_argument("--output-dir", default="tmp/codex_usage")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT)
    serve_parser.add_argument("--recent-limit", type=int, default=12)
    serve_parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=120,
        help="Rebuild the dashboard from fresh Codex data at most once per interval while serving. Use 0 to disable.",
    )

    return parser.parse_args()


def add_common_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-incomplete", action="store_true")


def load_filtered_sessions_for_args(args: argparse.Namespace, *, timezone: ZoneInfo) -> list[SessionSummary]:
    codex_home = Path(args.codex_home).expanduser()
    ratings_path = Path(args.ratings_path)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    ratings = load_ratings(ratings_path)
    sessions = load_sessions(
        codex_home=codex_home,
        timezone=timezone,
        ratings=ratings,
        workspace_root=workspace_root,
    )
    return filter_sessions(
        sessions,
        days=args.days,
        limit=args.limit,
        include_incomplete=args.include_incomplete,
    )


def main() -> int:
    args = parse_args()
    timezone = ZoneInfo(args.timezone)
    ratings_path = Path(args.ratings_path)
    sessions = load_filtered_sessions_for_args(args, timezone=timezone)

    if args.command == "list":
        handle_list(sessions, unrated_only=args.unrated)
        return 0

    if args.command == "rate":
        return handle_rate(args, sessions=sessions, ratings_path=ratings_path, timezone=timezone)

    if args.command == "report":
        return handle_report(args, sessions=sessions, timezone=timezone)

    if args.command == "serve":
        return handle_serve(args, sessions=sessions, timezone=timezone)

    raise ValueError(f"Unsupported command: {args.command}")


def load_ratings(path: Path) -> dict[str, RatingRecord]:
    ratings: dict[str, RatingRecord] = {}
    if not path.exists():
        return ratings

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(payload.get("session_id", "")).strip()
            if not session_id:
                continue
            score = safe_int(payload.get("score"))
            if score is None:
                continue
            ratings[session_id] = RatingRecord(
                session_id=session_id,
                score=score,
                outcome=str(payload.get("outcome", "kept")).strip() or "kept",
                notes=str(payload.get("notes", "")).strip(),
                recorded_at=str(payload.get("recorded_at", "")),
            )
    return ratings


def load_sessions(
    *,
    codex_home: Path,
    timezone: ZoneInfo,
    ratings: dict[str, RatingRecord],
    workspace_root: Path,
) -> list[SessionSummary]:
    database_path = codex_home / "state_5.sqlite"
    if not database_path.exists():
        raise FileNotFoundError(f"Missing Codex state database: {database_path}")
    workspace_top_levels = discover_workspace_top_levels(workspace_root)

    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            select
                id,
                rollout_path,
                created_at,
                updated_at,
                title,
                cwd,
                archived,
                first_user_message,
                model,
                reasoning_effort,
                tokens_used
            from threads
            order by updated_at desc
            """
        ).fetchall()
    finally:
        connection.close()

    sessions: list[SessionSummary] = []
    for row in rows:
        created_at = datetime.fromtimestamp(row["created_at"], tz=timezone)
        updated_at = datetime.fromtimestamp(row["updated_at"], tz=timezone)
        rollout_path = resolve_rollout_path(codex_home, row["rollout_path"], row["id"])
        rollout_metrics = (
            parse_rollout(
                rollout_path,
                workspace_root=workspace_root,
                workspace_top_levels=workspace_top_levels,
            )
            if rollout_path
            else default_rollout_metrics()
        )

        fallback_prompt = clean_text(str(row["first_user_message"] or ""))
        if not rollout_metrics["user_messages"] and fallback_prompt:
            rollout_metrics["user_messages"] = [fallback_prompt]

        substantive_messages = rollout_metrics["user_messages"]
        initial_prompt = substantive_messages[0] if substantive_messages else fallback_prompt
        manual = ratings.get(row["id"])
        total_tokens = rollout_metrics["total_tokens"] or normalize_thread_tokens(row["tokens_used"])
        final_answer = rollout_metrics["final_answer"]
        project_info = infer_project_summary(
            path_counts=rollout_metrics["workspace_path_counts"],
            workspace_root=workspace_root,
            workspace_top_levels=workspace_top_levels,
            cwd=str(row["cwd"] or ""),
        )

        session = SessionSummary(
            session_id=row["id"],
            title=clean_text(str(row["title"] or "")),
            task_bucket=infer_task_bucket(str(row["title"] or ""), initial_prompt),
            created_at=created_at,
            updated_at=updated_at,
            duration_minutes=max((updated_at - created_at).total_seconds() / 60.0, 0.0),
            archived=bool(row["archived"]),
            rollout_path=str(rollout_path) if rollout_path else None,
            model=str(row["model"] or "unknown"),
            reasoning_effort=str(row["reasoning_effort"] or "unknown"),
            substantive_user_messages=len(substantive_messages),
            user_chars=sum(len(message) for message in substantive_messages),
            initial_prompt_chars=len(initial_prompt),
            task_completions=rollout_metrics["task_completions"],
            commentary_messages=rollout_metrics["commentary_messages"],
            reasoning_notes=rollout_metrics["reasoning_notes"],
            tool_counts=dict(sorted(rollout_metrics["tool_counts"].items())),
            total_tools=sum(rollout_metrics["tool_counts"].values()),
            total_tokens=total_tokens,
            input_tokens=rollout_metrics["input_tokens"],
            cached_input_tokens=rollout_metrics["cached_input_tokens"],
            output_tokens=rollout_metrics["output_tokens"],
            reasoning_output_tokens=rollout_metrics["reasoning_output_tokens"],
            primary_used_percent_max=rollout_metrics["primary_used_percent_max"],
            secondary_used_percent_max=rollout_metrics["secondary_used_percent_max"],
            plan_type=rollout_metrics["plan_type"],
            inferred_project=project_info["inferred_project"],
            project_confidence=project_info["project_confidence"],
            project_path_refs=project_info["project_path_refs"],
            project_dominance=project_info["project_dominance"],
            workspace_path_counts=project_info["workspace_path_counts"],
            dominant_paths=project_info["dominant_paths"],
            verification_commands=rollout_metrics["verification_commands"],
            write_actions=rollout_metrics["write_actions"],
            final_answer_chars=len(final_answer),
            final_answer_snippet=trim_snippet(final_answer, 220),
            error_signal=has_error_signal(final_answer),
            proxy_quality=estimate_proxy_quality(
                task_completions=rollout_metrics["task_completions"],
                final_answer=final_answer,
                total_tokens=total_tokens,
                verification_commands=rollout_metrics["verification_commands"],
                write_actions=rollout_metrics["write_actions"],
                error_signal=has_error_signal(final_answer),
            ),
            manual_rating=manual.score if manual else None,
            manual_outcome=manual.outcome if manual else "",
            manual_notes=manual.notes if manual else "",
        )
        sessions.append(session)

    return sessions


def normalize_thread_tokens(value: Any) -> int | None:
    tokens = safe_int(value)
    if tokens is None or tokens <= 0:
        return None
    return tokens


def resolve_rollout_path(codex_home: Path, raw_path: str | None, session_id: str) -> Path | None:
    candidates: list[Path] = []
    if raw_path:
        raw = Path(str(raw_path)).expanduser()
        candidates.append(raw)
        if not raw.is_absolute():
            candidates.append(codex_home / raw)

    for folder_name in ("archived_sessions", "sessions"):
        folder = codex_home / folder_name
        if folder.exists():
            candidates.extend(sorted(folder.glob(f"*{session_id}.jsonl")))
            candidates.extend(sorted(folder.rglob(f"*{session_id}.jsonl")))

    seen: set[str] = set()
    for candidate in candidates:
        marker = str(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        if candidate.exists():
            return candidate
    return None


def parse_rollout(
    path: Path,
    *,
    workspace_root: Path,
    workspace_top_levels: set[str],
) -> dict[str, Any]:
    metrics = default_rollout_metrics()
    metrics["rollout_path"] = str(path)

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_type = record.get("type")
            payload = record.get("payload") or {}

            if record_type == "response_item":
                parse_response_item(
                    metrics,
                    payload,
                    workspace_root=workspace_root,
                    workspace_top_levels=workspace_top_levels,
                )
            elif record_type == "event_msg":
                parse_event_msg(metrics, payload)

    if not metrics["final_answer"] and metrics["task_complete_message"]:
        metrics["final_answer"] = metrics["task_complete_message"]

    return metrics


def default_rollout_metrics() -> dict[str, Any]:
    return {
        "user_messages": [],
        "tool_counts": Counter(),
        "commentary_messages": 0,
        "reasoning_notes": 0,
        "task_completions": 0,
        "task_complete_message": "",
        "final_answer": "",
        "total_tokens": None,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "primary_used_percent_max": None,
        "secondary_used_percent_max": None,
        "plan_type": None,
        "workspace_path_counts": Counter(),
        "verification_commands": 0,
        "write_actions": 0,
    }


def parse_response_item(
    metrics: dict[str, Any],
    payload: dict[str, Any],
    *,
    workspace_root: Path,
    workspace_top_levels: set[str],
) -> None:
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        message_text = flatten_content(payload.get("content") or [])
        if role == "user":
            if is_substantive_user_message(message_text):
                metrics["user_messages"].append(clean_text(message_text))
        elif role == "assistant":
            phase = payload.get("phase")
            if phase == "commentary":
                metrics["commentary_messages"] += 1
            elif phase == "final_answer":
                metrics["final_answer"] = clean_text(message_text)
        return

    if item_type in {"function_call", "custom_tool_call"}:
        name = str(payload.get("name") or "").strip()
        if name:
            metrics["tool_counts"][name] += 1
        texts = extract_tool_texts(payload)
        if name == "apply_patch":
            metrics["write_actions"] += 1
        elif any(has_command_term(text, WRITE_COMMAND_TERMS) for text in texts):
            metrics["write_actions"] += 1
        if any(has_command_term(text, VERIFICATION_TERMS) for text in texts):
            metrics["verification_commands"] += 1
        for text in texts:
            for rel_path in infer_paths_from_text(
                text,
                workspace_root=workspace_root,
                workspace_top_levels=workspace_top_levels,
            ):
                metrics["workspace_path_counts"][rel_path] += 1
        return

    if item_type == "reasoning":
        metrics["reasoning_notes"] += 1


def parse_event_msg(metrics: dict[str, Any], payload: dict[str, Any]) -> None:
    event_type = payload.get("type")
    if event_type == "task_complete":
        metrics["task_completions"] += 1
        message = clean_text(str(payload.get("last_agent_message", "")))
        if message:
            metrics["task_complete_message"] = message
        return

    if event_type == "token_count":
        info = payload.get("info") or {}
        usage = info.get("total_token_usage") or {}
        total_tokens = safe_int(usage.get("total_tokens"))
        if total_tokens is not None:
            metrics["total_tokens"] = max(metrics["total_tokens"] or 0, total_tokens)
        metrics["input_tokens"] = max_or_value(metrics["input_tokens"], safe_int(usage.get("input_tokens")))
        metrics["cached_input_tokens"] = max_or_value(
            metrics["cached_input_tokens"],
            safe_int(usage.get("cached_input_tokens")),
        )
        metrics["output_tokens"] = max_or_value(metrics["output_tokens"], safe_int(usage.get("output_tokens")))
        metrics["reasoning_output_tokens"] = max_or_value(
            metrics["reasoning_output_tokens"],
            safe_int(usage.get("reasoning_output_tokens")),
        )

        rate_limits = payload.get("rate_limits") or {}
        primary = ((rate_limits.get("primary") or {}).get("used_percent"))
        secondary = ((rate_limits.get("secondary") or {}).get("used_percent"))
        metrics["primary_used_percent_max"] = max_or_value(metrics["primary_used_percent_max"], safe_float(primary))
        metrics["secondary_used_percent_max"] = max_or_value(
            metrics["secondary_used_percent_max"],
            safe_float(secondary),
        )
        if rate_limits.get("plan_type"):
            metrics["plan_type"] = str(rate_limits["plan_type"])
        return

    if event_type == "agent_reasoning":
        metrics["reasoning_notes"] += 1


def flatten_content(content: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in content:
        text = item.get("text")
        if text:
            parts.append(str(text))
    return clean_text("\n".join(parts))


def is_substantive_user_message(message: str) -> bool:
    text = clean_text(message)
    if not text:
        return False
    if text.startswith("<environment_context>"):
        return False
    if text.startswith("<turn_aborted>"):
        return False
    return True


def clean_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def trim_snippet(text: str, limit: int) -> str:
    stripped = " ".join(clean_text(text).split())
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "..."


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def max_or_value(current: int | float | None, candidate: int | float | None) -> int | float | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def discover_workspace_top_levels(workspace_root: Path) -> set[str]:
    try:
        return {child.name for child in workspace_root.iterdir()}
    except OSError:
        return set()


def extract_tool_texts(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    arguments = payload.get("arguments")
    if arguments:
        raw_arguments = str(arguments)
        try:
            parsed_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            texts.append(raw_arguments)
        else:
            texts.extend(flatten_json_strings(parsed_arguments))

    custom_input = payload.get("input")
    if custom_input:
        texts.append(str(custom_input))

    return [text for text in dedupe_lines([clean_text(text) for text in texts]) if text]


def flatten_json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(flatten_json_strings(nested))
        return strings
    if isinstance(value, list):
        strings: list[str] = []
        for nested in value:
            strings.extend(flatten_json_strings(nested))
        return strings
    return []


def has_command_term(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def infer_paths_from_text(
    text: str,
    *,
    workspace_root: Path,
    workspace_top_levels: set[str],
) -> list[str]:
    if not text:
        return []

    candidates: list[str] = []
    for match in re.finditer(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", text, flags=re.MULTILINE):
        candidates.append(match.group(1).strip())

    try:
        candidates.extend(shlex.split(text))
    except ValueError:
        candidates.extend(text.split())

    candidates.extend(re.findall(r"(?:\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@-]+)+", text))
    candidates.extend(re.findall(r"\b[A-Za-z0-9_.-]+\.(?:py|md|json|jsonl|html|txt|toml|yaml|yml|db|sqlite|ics|sh|csv)\b", text))

    normalized: list[str] = []
    for candidate in candidates:
        rel_path = normalize_workspace_candidate(
            candidate,
            workspace_root=workspace_root,
            workspace_top_levels=workspace_top_levels,
        )
        if rel_path is not None:
            normalized.append(rel_path)
    return dedupe_lines(normalized)


def normalize_workspace_candidate(
    candidate: str,
    *,
    workspace_root: Path,
    workspace_top_levels: set[str],
) -> str | None:
    cleaned = candidate.strip().strip("\"'`()[]{}")
    cleaned = re.sub(r":\d+(?::\d+)?$", "", cleaned)
    cleaned = cleaned.rstrip(",)")
    if not cleaned or cleaned in {".", ".."}:
        return None
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return None
    if cleaned.startswith("-"):
        return None

    if cleaned.startswith("/"):
        rel_path = relative_workspace_path(Path(cleaned), workspace_root)
        if rel_path is None:
            return None
        return rel_path or "."

    cleaned = cleaned.removeprefix("./")
    if cleaned.startswith("../") or cleaned.startswith("~/"):
        return None
    if cleaned.startswith(".") and "/" not in cleaned and cleaned not in workspace_top_levels:
        return None

    normalized = Path(cleaned).as_posix()
    first_segment = normalized.split("/", 1)[0]
    if first_segment in ROOTISH_SEGMENTS:
        return None
    if "/" not in normalized:
        if normalized in ROOT_SHARED_FILES or normalized in workspace_top_levels:
            return normalized
        return None
    if first_segment not in workspace_top_levels and first_segment not in SHARED_TOP_LEVEL and not first_segment.startswith("."):
        return None
    return normalized


def relative_workspace_path(path: Path, workspace_root: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(workspace_root).as_posix()
    except ValueError:
        parts = list(path.parts)
        for index in range(len(parts) - 1, -1, -1):
            if parts[index] == workspace_root.name:
                rel_parts = parts[index + 1 :]
                return Path(*rel_parts).as_posix() if rel_parts else ""
    return None


def infer_project_summary(
    *,
    path_counts: Counter[str],
    workspace_root: Path,
    workspace_top_levels: set[str],
    cwd: str,
) -> dict[str, Any]:
    total_refs = sum(path_counts.values())
    dominant_paths = [path for path, _count in path_counts.most_common(5)]

    if total_refs == 0:
        cwd_label = normalize_workspace_candidate(
            cwd,
            workspace_root=workspace_root,
            workspace_top_levels=workspace_top_levels,
        )
        if cwd_label is None:
            return {
                "inferred_project": "unknown",
                "project_confidence": "low",
                "project_path_refs": 0,
                "project_dominance": 0.0,
                "workspace_path_counts": {},
                "dominant_paths": [],
            }
        bucket = project_bucket_for_path(cwd_label)
        return {
            "inferred_project": bucket,
            "project_confidence": "medium" if bucket not in {"workspace:root", "workspace:shared"} else "low",
            "project_path_refs": 0,
            "project_dominance": 1.0,
            "workspace_path_counts": {},
            "dominant_paths": [cwd_label],
        }

    bucket_counts: Counter[str] = Counter()
    for path, count in path_counts.items():
        bucket_counts[project_bucket_for_path(path)] += count

    chosen_bucket, chosen_count = choose_project_bucket(bucket_counts, total_refs)
    dominance = chosen_count / max(total_refs, 1)
    confidence = project_confidence_for_bucket(
        bucket=chosen_bucket,
        dominance=dominance,
        total_refs=total_refs,
        bucket_counts=bucket_counts,
    )
    return {
        "inferred_project": chosen_bucket,
        "project_confidence": confidence,
        "project_path_refs": total_refs,
        "project_dominance": round(dominance, 2),
        "workspace_path_counts": dict(path_counts),
        "dominant_paths": dominant_paths,
    }


def choose_project_bucket(bucket_counts: Counter[str], total_refs: int) -> tuple[str, int]:
    if not bucket_counts:
        return "unknown", 0

    specific = {bucket: count for bucket, count in bucket_counts.items() if not bucket.startswith("workspace:")}
    if len(specific) >= 2:
        sorted_specific = sorted(specific.items(), key=lambda item: item[1], reverse=True)
        first_count = sorted_specific[0][1]
        second_count = sorted_specific[1][1]
        if first_count / max(total_refs, 1) < 0.65 and second_count / max(total_refs, 1) >= 0.2:
            return "multi-project", first_count

    if specific:
        top_specific, top_specific_count = max(specific.items(), key=lambda item: item[1])
        if top_specific_count / max(total_refs, 1) >= 0.35:
            return top_specific, top_specific_count

    top_bucket, top_count = bucket_counts.most_common(1)[0]
    return top_bucket, top_count


def project_bucket_for_path(path: str) -> str:
    normalized = path.strip("/")
    if not normalized or normalized == ".":
        return "workspace:root"
    if normalized in ROOT_SHARED_FILES:
        return "workspace:shared"
    first_segment = normalized.split("/", 1)[0]
    if first_segment in SHARED_TOP_LEVEL:
        return "workspace:shared"
    if first_segment.startswith("."):
        return "workspace:root"
    return first_segment


def project_confidence_for_bucket(
    *,
    bucket: str,
    dominance: float,
    total_refs: int,
    bucket_counts: Counter[str],
) -> str:
    if bucket in {"unknown", "multi-project"}:
        return "low"
    if bucket in {"workspace:root", "workspace:shared"}:
        if total_refs >= 5 and dominance >= 0.75:
            return "medium"
        return "low"
    competing_specific = sorted(
        (
            count
            for label, count in bucket_counts.items()
            if label != bucket and not label.startswith("workspace:")
        ),
        reverse=True,
    )
    second_specific = competing_specific[0] if competing_specific else 0
    if total_refs >= 4 and dominance >= 0.75 and second_specific / max(total_refs, 1) < 0.2:
        return "high"
    if total_refs >= 2 and dominance >= 0.55:
        return "medium"
    return "low"


def project_label(session: SessionSummary) -> str:
    return f"{display_project_name(session.inferred_project)} [{session.project_confidence}]"


def project_short_label(session: SessionSummary) -> str:
    confidence_map = {"high": "hi", "medium": "md", "low": "lo"}
    return f"{trim_snippet(display_project_name(session.inferred_project), 18)}:{confidence_map.get(session.project_confidence, 'lo')}"


def display_project_name(project: str) -> str:
    if project in PROJECT_LABELS:
        return PROJECT_LABELS[project]
    return " ".join(part.capitalize() for part in project.replace("_", " ").replace("-", " ").split())


def project_hint(project: str) -> str:
    if project == "workspace:shared":
        return "Mostly shared folders like scripts, docs, Journal, or tmp."
    if project == "workspace:root":
        return "No single project folder stood out; often conceptual or root-level work."
    if project == "multi-project":
        return "Multiple project folders were touched in one session."
    if project == "unknown":
        return "The session did not expose enough file evidence to infer a project."
    return f"Inferred mainly from files under `{project}/`."


def display_task_bucket(bucket: str) -> str:
    return TASK_BUCKET_LABELS.get(bucket, " ".join(part.capitalize() for part in bucket.replace("_", " ").split()))


def comparison_scope(session: SessionSummary, report_mode: str) -> str | None:
    if report_mode == "basic":
        return session.task_bucket
    if session.project_confidence == "low":
        return None
    if session.inferred_project in {"unknown", "multi-project", "workspace:root"}:
        return None
    return f"{session.inferred_project}|{session.task_bucket}"


def is_project_scoped(session: SessionSummary) -> bool:
    return (
        session.project_confidence in {"medium", "high"}
        and session.inferred_project not in {"unknown", "multi-project", "workspace:root"}
    )


def recommendation_confidence(sessions: list[SessionSummary]) -> str:
    manual_count = sum(1 for session in sessions if session.manual_rating is not None)
    if manual_count >= 6:
        return "Higher-confidence"
    if manual_count >= 3:
        return "Medium-confidence"
    if len(sessions) >= 8:
        return "Low-confidence proxy"
    return "Early-signal"


def report_mode_description(report_mode: str) -> str:
    if report_mode == "basic":
        return "`basic` mode stays descriptive only: it shows usage patterns by project and setting without making ROI guesses."
    if report_mode == "inferred":
        return "`inferred` mode will make project-scoped ROI guesses from rollout evidence, even when confidence is mostly proxy-based."
    return "`hybrid` mode stays factual by default and only makes ROI suggestions where sessions look comparable within one inferred project."


def infer_task_bucket(title: str, prompt: str) -> str:
    title_lower = title.lower()
    prompt_lower = prompt.lower()
    combined = f"{title_lower}\n{prompt_lower}"

    if title_lower.startswith("automation:"):
        return "automation"
    if contains_any(combined, REVIEW_TERMS):
        return "review"
    if contains_any(combined, OPS_TERMS):
        return "ops"
    if contains_any(combined, BUILD_TERMS):
        return "build"
    return "general"


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def has_error_signal(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ERROR_TERMS)


def estimate_proxy_quality(
    *,
    task_completions: int,
    final_answer: str,
    total_tokens: int | None,
    verification_commands: int,
    write_actions: int,
    error_signal: bool,
) -> float:
    score = 2.6
    if task_completions > 0:
        score += 0.8
    if len(final_answer) >= 120:
        score += 0.6
    elif len(final_answer) >= 40:
        score += 0.3
    if not error_signal:
        score += 0.2
    else:
        score -= 0.7
    if write_actions > 0:
        score += 0.3
    if verification_commands > 0:
        score += 0.4
    if total_tokens and total_tokens > 800_000:
        score -= 0.2
    if total_tokens and total_tokens > 1_500_000:
        score -= 0.2
    if total_tokens and total_tokens > 600_000 and write_actions == 0 and verification_commands == 0:
        score -= 0.2
    return max(1.0, min(5.0, round(score, 1)))


def filter_sessions(
    sessions: list[SessionSummary],
    *,
    days: int,
    limit: int,
    include_incomplete: bool,
) -> list[SessionSummary]:
    cutoff = datetime.now(tz=sessions[0].created_at.tzinfo if sessions else None) - timedelta(days=days)
    filtered = [
        session
        for session in sessions
        if session.updated_at >= cutoff and (include_incomplete or session.is_complete)
    ]
    filtered.sort(key=lambda session: session.updated_at, reverse=True)
    return filtered[:limit]


def handle_list(sessions: list[SessionSummary], *, unrated_only: bool) -> None:
    visible = [session for session in sessions if not unrated_only or session.manual_rating is None]
    if not visible:
        print("No matching sessions found.")
        return

    print(
        "idx  date              id        project              bucket      effort  ag  pl  tokens    quality        title"
    )
    for index, session in enumerate(visible, start=1):
        print(
            f"{index:>3}  "
            f"{session.updated_at:%Y-%m-%d %H:%M}  "
            f"{short_session_id(session.session_id):<13}  "
            f"{project_short_label(session):<19}  "
            f"{trim_snippet(display_task_bucket(session.task_bucket), 10):<10}  "
            f"{session.reasoning_effort:<6}  "
            f"{flag(session.agent_used)}   "
            f"{flag(session.planning_used)}   "
            f"{format_tokens(session.total_tokens):>8}  "
            f"{format_quality(session):<13}  "
            f"{trim_snippet(session.title, 52)}"
        )


def handle_rate(
    args: argparse.Namespace,
    *,
    sessions: list[SessionSummary],
    ratings_path: Path,
    timezone: ZoneInfo,
) -> int:
    if args.session and args.score is not None:
        target = find_session_by_id(args.session, sessions)
        if target is None:
            print(f"Session not found in current filter window: {args.session}", file=sys.stderr)
            return 1
        return save_rating(
            ratings_path=ratings_path,
            session=target,
            score=args.score,
            outcome=args.outcome,
            notes=args.notes,
            timezone=timezone,
        )

    candidates = [session for session in sessions if session.manual_rating is None][: args.latest]
    if not candidates:
        print("No unrated sessions available in the current filter window.")
        return 0

    handle_list(candidates, unrated_only=False)
    selection = input("Choose a session by index or full id: ").strip()
    target = pick_session(selection, candidates)
    if target is None:
        print("Could not match that session.", file=sys.stderr)
        return 1

    score_text = input("Score 1-5 (1 unusable, 5 strong): ").strip()
    score = safe_int(score_text)
    if score is None:
        print("Score must be a number from 1 to 5.", file=sys.stderr)
        return 1

    outcome = input("Outcome label [kept/minor-edit/heavy-edit/discard] (default kept): ").strip() or "kept"
    notes = input("Optional note: ").strip()
    return save_rating(
        ratings_path=ratings_path,
        session=target,
        score=score,
        outcome=outcome,
        notes=notes,
        timezone=timezone,
    )


def pick_session(selection: str, sessions: list[SessionSummary]) -> SessionSummary | None:
    if not selection:
        return None
    index = safe_int(selection)
    if index is not None and 1 <= index <= len(sessions):
        return sessions[index - 1]
    return find_session_by_id(selection, sessions)


def find_session_by_id(selection: str, sessions: list[SessionSummary]) -> SessionSummary | None:
    normalized = selection.strip()
    if not normalized:
        return None

    exact_matches = [session for session in sessions if session.session_id == normalized]
    if exact_matches:
        return exact_matches[0]

    prefix_matches = [session for session in sessions if session.session_id.startswith(normalized)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


def save_rating(
    *,
    ratings_path: Path,
    session: SessionSummary,
    score: int,
    outcome: str,
    notes: str,
    timezone: ZoneInfo,
) -> int:
    if score < 1 or score > 5:
        print("Score must be between 1 and 5.", file=sys.stderr)
        return 1

    ratings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session.session_id,
        "score": score,
        "outcome": outcome.strip() or "kept",
        "notes": notes.strip(),
        "recorded_at": datetime.now(timezone).isoformat(timespec="seconds"),
    }
    with ratings_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")

    print(f"Saved rating {score}/5 for {session.session_id} ({trim_snippet(session.title, 60)}).")
    return 0


def handle_report(args: argparse.Namespace, *, sessions: list[SessionSummary], timezone: ZoneInfo) -> int:
    summary = build_report_payload(sessions, report_mode=args.mode)
    paths = write_report_bundle(
        summary,
        timezone=timezone,
        recent_limit=args.recent_limit,
        markdown_path=resolve_report_output_path(Path(args.markdown_path), DEFAULT_MARKDOWN_PATH, args.mode),
        json_path=resolve_report_output_path(Path(args.json_path), DEFAULT_JSON_PATH, args.mode),
        html_path=resolve_report_output_path(Path(args.html_path), DEFAULT_HTML_PATH, args.mode),
        write_index=False,
    )
    print(render_markdown_report(summary, recent_limit=args.recent_limit, timezone=timezone))
    print()
    print(f"Saved Markdown report to {paths['markdown_path']}")
    print(f"Saved JSON report to {paths['json_path']}")
    print(f"Saved HTML dashboard to {paths['html_path']}")
    return 0


def resolve_report_output_path(path: Path, default_path: Path, report_mode: str) -> Path:
    if path != default_path:
        return path
    if report_mode == DEFAULT_REPORT_MODE:
        return path
    return path.with_name(f"{path.stem}-{report_mode}{path.suffix}")


def handle_serve(args: argparse.Namespace, *, sessions: list[SessionSummary], timezone: ZoneInfo) -> int:
    output_dir = Path(args.output_dir)
    refresh_seconds = max(args.refresh_seconds, 0)
    refresh_lock = threading.Lock()
    state = {
        "last_refresh_monotonic": 0.0,
        "last_refresh_at": "",
    }

    def rebuild_dashboard(live_sessions: list[SessionSummary]) -> dict[str, Path]:
        summary = build_report_payload(live_sessions, report_mode="hybrid")
        paths = write_report_bundle(
            summary,
            timezone=timezone,
            recent_limit=args.recent_limit,
            markdown_path=output_dir / "latest-report.md",
            json_path=output_dir / "latest-report.json",
            html_path=output_dir / "index.html",
            write_index=False,
        )
        state["last_refresh_monotonic"] = time.monotonic()
        state["last_refresh_at"] = datetime.now(timezone).isoformat(timespec="seconds")
        return paths

    def maybe_refresh_dashboard(*, force: bool = False) -> dict[str, Path]:
        with refresh_lock:
            html_path = output_dir / "index.html"
            recently_refreshed = (
                refresh_seconds > 0
                and (time.monotonic() - float(state["last_refresh_monotonic"])) < refresh_seconds
                and html_path.exists()
            )
            if not force and recently_refreshed:
                return {
                    "markdown_path": output_dir / "latest-report.md",
                    "json_path": output_dir / "latest-report.json",
                    "html_path": html_path,
                }
            live_sessions = load_filtered_sessions_for_args(args, timezone=timezone)
            return rebuild_dashboard(live_sessions)

    main_paths = rebuild_dashboard(sessions)
    urls = dashboard_urls(args.host, args.port, main_paths["html_path"].name)
    print(f"Serving dashboard from {output_dir.resolve()}", flush=True)
    for label, url in urls:
        print(f"{label}: {url}", flush=True)
    if refresh_seconds > 0:
        print(
            f"Live refresh is enabled: the dashboard will rebuild from fresh Codex data at most once every {refresh_seconds} seconds.",
            flush=True,
        )
    else:
        print("Live refresh is disabled: the current served page stays static until you rerun `serve`.", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    class LiveDashboardHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            request_path = self.path.split("?", 1)[0]
            if request_path in {"/", "/index.html", "/latest-report.json", "/latest-report.md"}:
                try:
                    maybe_refresh_dashboard()
                except Exception as exc:  # pragma: no cover - defensive logging
                    print(f"Dashboard refresh failed: {exc}", file=sys.stderr, flush=True)
            super().do_GET()

    handler = partial(LiveDashboardHandler, directory=str(output_dir.resolve()))
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.", flush=True)
    finally:
        server.server_close()
    return 0


def build_json_payload(summary: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone).isoformat(timespec="seconds"),
        "summary": summary["summary"],
        "measurement_confidence": summary["measurement_confidence"],
        "label_guide": summary["label_guide"],
        "usage_reference": summary["usage_reference"],
        "burn_summary": summary["burn_summary"],
        "expensive_patterns": summary["expensive_patterns"],
        "findings": summary["findings"],
        "recommendations": summary["recommendations"],
        "next_steps": summary["next_steps"],
        "factor_breakdowns": summary["factor_breakdowns"],
        "project_usage_rows": summary["project_usage_rows"],
        "daily_usage_rows": summary["daily_usage_rows"],
        "heavy_session_rows": summary["heavy_session_rows"],
        "rating_guide": rating_guide(),
        "sessions": [session.export_dict() for session in summary["sessions"]],
    }


def write_report_bundle(
    summary: dict[str, Any],
    *,
    timezone: ZoneInfo,
    recent_limit: int,
    markdown_path: Path,
    json_path: Path,
    html_path: Path,
    write_index: bool,
) -> dict[str, Path]:
    markdown = render_markdown_report(summary, recent_limit=recent_limit, timezone=timezone)
    html_text = render_html_report(summary, recent_limit=recent_limit, timezone=timezone)
    json_payload = build_json_payload(summary, timezone)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    html_path.write_text(html_text + "\n", encoding="utf-8")
    if write_index:
        write_dashboard_index(
            html_path.parent,
            basic_html_path=html_path.parent / "dashboard-basic.html",
            hybrid_html_path=html_path.parent / "dashboard.html",
            generated_at=datetime.now(timezone).isoformat(timespec="seconds"),
        )
    return {
        "markdown_path": markdown_path,
        "json_path": json_path,
        "html_path": html_path,
    }


def write_dashboard_index(
    output_dir: Path,
    *,
    basic_html_path: Path,
    hybrid_html_path: Path,
    generated_at: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.html"
    basic_name = basic_html_path.name
    hybrid_name = hybrid_html_path.name
    index_html = f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Codex Usage Dashboards</title>
    <style>
      body {{
        margin: 0;
        font-family: "Avenir Next", "Gill Sans", "Trebuchet MS", sans-serif;
        background: linear-gradient(180deg, #f6f1e7, #efe4d2);
        color: #1f1c1a;
      }}
      .page {{
        width: min(860px, calc(100vw - 32px));
        margin: 36px auto;
      }}
      .hero {{
        border-radius: 28px;
        padding: 28px;
        background: rgba(255, 250, 244, 0.92);
        box-shadow: 0 18px 60px rgba(66, 41, 25, 0.12);
      }}
      h1, h2 {{
        font-family: "Iowan Old Style", Georgia, serif;
        margin: 0;
      }}
      p {{
        color: #6b6257;
        line-height: 1.55;
      }}
      .cards {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
        margin-top: 20px;
      }}
      .card {{
        border-radius: 22px;
        padding: 22px;
        border: 1px solid rgba(73, 57, 41, 0.14);
        background: rgba(255, 251, 245, 0.92);
      }}
      .cta {{
        display: inline-block;
        margin-top: 14px;
        padding: 11px 16px;
        border-radius: 999px;
        background: rgba(181, 82, 51, 0.12);
        color: #b55233;
        text-decoration: none;
        font-weight: 700;
      }}
      code {{
        display: inline-block;
        margin-top: 8px;
        padding: 8px 10px;
        border-radius: 12px;
        background: rgba(29, 25, 22, 0.96);
        color: #f7f1e7;
      }}
      @media (max-width: 760px) {{
        .cards {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="page">
      <section class="hero">
        <h1>Codex Usage Dashboards</h1>
        <p>Generated {html.escape(generated_at)}. Basic is the clearest factual view. Hybrid keeps the same telemetry but adds cautious interpretation when evidence is strong enough.</p>
        <div class="cards">
          <article class="card">
            <h2>Basic Dashboard</h2>
            <p>Best default. Shows measured tokens, project mix, daily burn, pressure, and heaviest sessions without leaning on weak advice.</p>
            <a class="cta" href="{html.escape(basic_name)}">Open Basic</a>
          </article>
          <article class="card">
            <h2>Hybrid Dashboard</h2>
            <p>Same telemetry, plus tentative project-aware interpretation. Use when you want context, not certainty.</p>
            <a class="cta" href="{html.escape(hybrid_name)}">Open Hybrid</a>
          </article>
        </div>
      </section>
    </div>
  </body>
</html>"""
    index_path.write_text(index_html + "\n", encoding="utf-8")
    return index_path


def dashboard_urls(host: str, port: int, page_name: str) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    if host in {"0.0.0.0", "::"}:
        urls.append(("Local", f"http://127.0.0.1:{port}/{page_name}"))
        for ip in detect_local_ips():
            urls.append(("LAN", f"http://{ip}:{port}/{page_name}"))
    elif host == "localhost":
        urls.append(("Local", f"http://localhost:{port}/{page_name}"))
    else:
        urls.append(("Dashboard", f"http://{host}:{port}/{page_name}"))
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append((label, url))
    return deduped


def detect_local_ips() -> list[str]:
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip and not ip.startswith("127."):
            ips.append(ip)
    try:
        result = subprocess.run(
            ["ifconfig"],
            capture_output=True,
            text=True,
            check=False,
        )
        for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", result.stdout):
            if ip and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return dedupe_lines(ips)


def token_count(session: SessionSummary) -> int:
    return session.total_tokens or 0


def percent_value(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return (part / total) * 100.0


def format_percent(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def build_report_payload(sessions: list[SessionSummary], *, report_mode: str) -> dict[str, Any]:
    completed = [session for session in sessions if session.is_complete]
    rated = [session for session in completed if session.manual_rating is not None]
    proxy_only = [session for session in completed if session.manual_rating is None]
    project_counts = Counter(session.inferred_project for session in completed)
    confidence_counts = Counter(session.project_confidence for session in completed)
    scoped_sessions = [session for session in completed if is_project_scoped(session)]
    top_project, top_project_sessions = project_counts.most_common(1)[0] if project_counts else ("n/a", 0)
    total_tokens_sum = sum(token_count(session) for session in completed)

    summary = {
        "report_mode": report_mode,
        "session_count": len(completed),
        "rated_count": len(rated),
        "proxy_only_count": len(proxy_only),
        "total_tokens_sum": total_tokens_sum,
        "manual_rating_average": round(mean(session.manual_rating for session in rated), 2) if rated else None,
        "average_quality": round(mean(session.quality_score for session in completed), 2) if completed else None,
        "max_primary_used_percent": round(
            max((session.primary_used_percent_max or 0.0) for session in completed),
            1,
        )
        if completed
        else None,
        "max_secondary_used_percent": round(
            max((session.secondary_used_percent_max or 0.0) for session in completed),
            1,
        )
        if completed
        else None,
        "xhigh_count": sum(1 for session in completed if session.reasoning_effort == "xhigh"),
        "medium_count": sum(1 for session in completed if session.reasoning_effort == "medium"),
        "planning_count": sum(1 for session in completed if session.planning_used),
        "agent_count": sum(1 for session in completed if session.agent_used),
        "distinct_projects": len(project_counts),
        "top_project": top_project,
        "top_project_sessions": top_project_sessions,
        "project_scoped_sessions": len(scoped_sessions),
        "high_confidence_project_sessions": confidence_counts.get("high", 0),
        "medium_confidence_project_sessions": confidence_counts.get("medium", 0),
        "low_confidence_project_sessions": confidence_counts.get("low", 0),
    }

    measurement_confidence = build_measurement_confidence(completed, summary)
    project_usage_rows = build_project_usage_rows(completed)
    daily_usage_rows = build_daily_usage_rows(completed)
    heavy_session_rows = build_heavy_session_rows(completed)
    burn_summary = build_burn_summary(summary, daily_usage_rows)
    expensive_patterns = build_expensive_patterns(completed, total_tokens_sum=total_tokens_sum)
    label_guide = build_label_guide()
    usage_reference = build_usage_reference(
        completed,
        summary,
        project_usage_rows=project_usage_rows,
        daily_usage_rows=daily_usage_rows,
        heavy_session_rows=heavy_session_rows,
    )
    findings = build_findings(completed, summary, report_mode=report_mode)
    recommendations = build_recommendations(completed, summary, report_mode=report_mode)
    next_steps = build_next_steps(completed, summary, report_mode=report_mode)
    factor_breakdowns = {
        "reasoning": group_breakdown(completed, lambda session: session.reasoning_effort),
        "planning": group_breakdown(completed, lambda session: "planned" if session.planning_used else "no-plan"),
        "agents": group_breakdown(completed, lambda session: "agents" if session.agent_used else "no-agents"),
        "task_bucket": group_breakdown(completed, lambda session: display_task_bucket(session.task_bucket)),
        "project": group_breakdown(completed, lambda session: display_project_name(session.inferred_project)),
    }

    return {
        "summary": summary,
        "measurement_confidence": measurement_confidence,
        "label_guide": label_guide,
        "usage_reference": usage_reference,
        "burn_summary": burn_summary,
        "expensive_patterns": expensive_patterns,
        "findings": findings,
        "recommendations": recommendations,
        "next_steps": next_steps,
        "factor_breakdowns": factor_breakdowns,
        "project_usage_rows": project_usage_rows,
        "daily_usage_rows": daily_usage_rows,
        "heavy_session_rows": heavy_session_rows,
        "sessions": completed,
    }


def build_measurement_confidence(
    sessions: list[SessionSummary],
    summary: dict[str, Any],
) -> list[str]:
    lines = [
        "Measured directly: session ids, timestamps, model, reasoning effort, token counts, rate-limit peaks, and whether planning (`update_plan`), subagents (`spawn_agent`), and file edits (`apply_patch`) were used.",
        "Estimated: duration is thread lifespan from `created_at` to `updated_at`, not active keyboard time.",
        f"Inferred: project attribution is heuristic. This window has {summary['high_confidence_project_sessions']} high-confidence, {summary['medium_confidence_project_sessions']} medium-confidence, and {summary['low_confidence_project_sessions']} low-confidence project labels.",
    ]
    if summary["rated_count"] == 0:
        lines.append("Quality is currently proxy-only, so any advice about ROI or habit value should be treated as tentative.")
    else:
        lines.append(
            f"Quality has {summary['rated_count']} manual anchor ratings and {summary['proxy_only_count']} proxy-only sessions in this window."
        )
    if not sessions:
        lines.append("No completed sessions are available in this filter window.")
    return lines


def build_project_usage_rows(sessions: list[SessionSummary]) -> list[dict[str, Any]]:
    groups: dict[str, list[SessionSummary]] = defaultdict(list)
    total_tokens = sum(token_count(session) for session in sessions)
    for session in sessions:
        groups[session.inferred_project].append(session)

    rows: list[dict[str, Any]] = []
    for project, items in sorted(
        groups.items(),
        key=lambda pair: (-sum(token_count(item) for item in pair[1]), -len(pair[1]), pair[0]),
    ):
        project_tokens = sum(token_count(item) for item in items)
        confidence_mix = Counter(item.project_confidence for item in items)
        rows.append(
            {
                "project": project,
                "sessions": len(items),
                "total_tokens": project_tokens,
                "token_share": percent_value(project_tokens, total_tokens),
                "primary_peak": round(max((item.primary_used_percent_max or 0.0) for item in items), 1),
                "secondary_peak": round(max((item.secondary_used_percent_max or 0.0) for item in items), 1),
                "confidence_mix": (
                    f"h{confidence_mix.get('high', 0)}/m{confidence_mix.get('medium', 0)}/l{confidence_mix.get('low', 0)}"
                ),
            }
        )
    return rows


def build_daily_usage_rows(sessions: list[SessionSummary]) -> list[dict[str, Any]]:
    groups: dict[str, list[SessionSummary]] = defaultdict(list)
    for session in sessions:
        groups[session.updated_at.strftime("%Y-%m-%d")].append(session)

    rows: list[dict[str, Any]] = []
    for day, items in sorted(groups.items(), key=lambda pair: pair[0], reverse=True):
        rows.append(
            {
                "day": day,
                "sessions": len(items),
                "total_tokens": sum(token_count(item) for item in items),
                "primary_peak": round(max((item.primary_used_percent_max or 0.0) for item in items), 1),
                "secondary_peak": round(max((item.secondary_used_percent_max or 0.0) for item in items), 1),
                "xhigh_sessions": sum(1 for item in items if item.reasoning_effort == "xhigh"),
                "agent_sessions": sum(1 for item in items if item.agent_used),
                "planned_sessions": sum(1 for item in items if item.planning_used),
            }
        )
    return rows


def build_heavy_session_rows(sessions: list[SessionSummary], *, limit: int = 5) -> list[dict[str, Any]]:
    ordered = sorted(sessions, key=lambda session: token_count(session), reverse=True)
    rows: list[dict[str, Any]] = []
    for session in ordered[:limit]:
        rows.append(
            {
                "when": session.updated_at.strftime("%Y-%m-%d %H:%M"),
                "id": short_session_id(session.session_id),
                "project": project_label(session),
                "tokens": token_count(session),
                "effort": session.reasoning_effort,
                "ag": yes_no(session.agent_used),
                "pl": yes_no(session.planning_used),
                "title": trim_snippet(session.title, 58),
            }
        )
    return rows


def build_usage_reference(
    sessions: list[SessionSummary],
    summary: dict[str, Any],
    *,
    project_usage_rows: list[dict[str, Any]],
    daily_usage_rows: list[dict[str, Any]],
    heavy_session_rows: list[dict[str, Any]],
) -> list[str]:
    if not sessions:
        return ["No completed sessions are available in this filter window."]

    total_tokens = summary["total_tokens_sum"]
    lines: list[str] = []
    if total_tokens > 0:
        xhigh_tokens = sum(token_count(session) for session in sessions if session.reasoning_effort == "xhigh")
        agent_tokens = sum(token_count(session) for session in sessions if session.agent_used)
        planned_tokens = sum(token_count(session) for session in sessions if session.planning_used)
        low_conf_tokens = sum(token_count(session) for session in sessions if session.project_confidence == "low")
        lines.append(
            f"`xhigh` accounts for {format_percent(percent_value(xhigh_tokens, total_tokens))} of measured tokens across {summary['xhigh_count']} sessions."
        )
        lines.append(
            f"Subagents were used in {summary['agent_count']} sessions and account for {format_percent(percent_value(agent_tokens, total_tokens))} of measured tokens."
        )
        lines.append(
            f"Planning was used in {summary['planning_count']} sessions and accounts for {format_percent(percent_value(planned_tokens, total_tokens))} of measured tokens."
        )
        lines.append(
            f"Low-confidence project attribution still covers {format_percent(percent_value(low_conf_tokens, total_tokens))} of measured tokens in this window."
        )

    if project_usage_rows:
        top_project = project_usage_rows[0]
        lines.append(
            f"`{display_project_name(top_project['project'])}` is the heaviest inferred project by measured tokens: {format_tokens(top_project['total_tokens'])} "
            f"({format_percent(top_project['token_share'])}) across {top_project['sessions']} sessions."
        )
    if daily_usage_rows:
        busiest_day = max(daily_usage_rows, key=lambda row: row["total_tokens"])
        lines.append(
            f"{busiest_day['day']} is the busiest day in this window by measured tokens: {format_tokens(busiest_day['total_tokens'])} "
            f"across {busiest_day['sessions']} sessions."
        )
    if heavy_session_rows:
        top_heavy = heavy_session_rows[0]
        lines.append(
            f"The heaviest recent session is {top_heavy['id']} in `{top_heavy['project']}` at {format_tokens(top_heavy['tokens'])}."
        )
    return lines


def build_burn_summary(summary: dict[str, Any], daily_usage_rows: list[dict[str, Any]]) -> list[str]:
    if not daily_usage_rows:
        return ["No active days were found in this window."]
    active_days = len(daily_usage_rows)
    total_tokens = summary["total_tokens_sum"]
    avg_per_day = total_tokens / max(active_days, 1)
    busiest_day = max(daily_usage_rows, key=lambda row: row["total_tokens"])
    high_pressure_days = sum(
        1
        for row in daily_usage_rows
        if row["primary_peak"] >= 75.0 or row["secondary_peak"] >= 75.0
    )
    return [
        f"Active days in this window: {active_days}. Average measured burn is {format_number(avg_per_day)} tokens per active day.",
        f"Busiest day: {busiest_day['day']} with {format_number(busiest_day['total_tokens'])} tokens across {busiest_day['sessions']} sessions.",
        f"High-pressure days: {high_pressure_days} of {active_days} active days reached at least 75% on primary or secondary usage.",
    ]


def build_expensive_patterns(
    sessions: list[SessionSummary],
    *,
    total_tokens_sum: int,
    limit: int = 6,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, bool, bool], list[SessionSummary]] = defaultdict(list)
    for session in sessions:
        key = (
            session.inferred_project,
            session.task_bucket,
            session.reasoning_effort,
            session.agent_used,
            session.planning_used,
        )
        groups[key].append(session)

    rows: list[dict[str, Any]] = []
    for (project, bucket, effort, agent_used, planning_used), items in groups.items():
        if len(items) < 2:
            continue
        total_tokens = sum(token_count(item) for item in items)
        confidence_mix = Counter(item.project_confidence for item in items)
        rows.append(
            {
                "project": display_project_name(project),
                "task_bucket": display_task_bucket(bucket),
                "reasoning": effort,
                "subagents": "yes" if agent_used else "no",
                "planning": "yes" if planning_used else "no",
                "sessions": len(items),
                "total_tokens": total_tokens,
                "token_share": percent_value(total_tokens, total_tokens_sum),
                "avg_tokens": round(total_tokens / max(len(items), 1)),
                "confidence_mix": f"h{confidence_mix.get('high', 0)}/m{confidence_mix.get('medium', 0)}/l{confidence_mix.get('low', 0)}",
            }
        )
    rows.sort(key=lambda row: (-row["total_tokens"], -row["sessions"], row["project"], row["task_bucket"]))
    return rows[:limit]


def build_label_guide() -> list[dict[str, str]]:
    return [
        {
            "label": "Subagents",
            "description": "Sessions that used the `spawn_agent` tool to delegate part of the work.",
        },
        {
            "label": "Planning",
            "description": "Sessions that used the `update_plan` tool to track a multi-step plan.",
        },
        {
            "label": "Shared Workspace",
            "description": "Work mostly touched shared folders like `scripts`, `docs`, `Journal`, or `tmp` instead of one project folder.",
        },
        {
            "label": "Root-Level Or Conceptual",
            "description": "No single project folder stood out; often brainstorming, review, or root-level work.",
        },
        {
            "label": "Multiple Projects",
            "description": "The session touched more than one project folder, so project-level interpretation is weaker.",
        },
        {
            "label": "Project Confidence",
            "description": "How strongly the path evidence supports the inferred project label. High means path evidence was concentrated; low means interpretation is weak.",
        },
        {
            "label": "Daily Burn",
            "description": "Measured tokens per day. This is the clearest simple burn-rate view and is usually more useful than a fancier chart.",
        },
    ]


def build_findings(
    sessions: list[SessionSummary],
    summary: dict[str, Any],
    *,
    report_mode: str,
) -> list[str]:
    findings: list[str] = []
    if not sessions:
        return ["No completed sessions were found in the current window."]

    findings.append(report_mode_description(report_mode))

    if summary["rated_count"] == 0:
        findings.append(
            f"Quality is proxy-only right now: 0 of {summary['session_count']} completed sessions have a manual rating."
        )
    else:
        findings.append(
            f"You have {summary['rated_count']} manually rated sessions and {summary['proxy_only_count']} proxy-only sessions in the current window."
        )
    findings.append(
        "Proxy quality is inferred from completion signals, output shape, write actions, verification commands, and obvious failure language."
    )

    findings.append(
        f"The dominant inferred project is `{display_project_name(summary['top_project'])}` ({summary['top_project_sessions']} of {summary['session_count']} sessions)."
    )
    findings.append(
        f"{summary['project_scoped_sessions']} sessions are specific enough for within-project comparisons; "
        f"confidence split is high {summary['high_confidence_project_sessions']}, "
        f"medium {summary['medium_confidence_project_sessions']}, low {summary['low_confidence_project_sessions']}."
    )

    reasoning_mix = Counter(session.reasoning_effort for session in sessions)
    top_reasoning, top_count = reasoning_mix.most_common(1)[0]
    findings.append(
        f"`{top_reasoning}` is your most common reasoning setting in this window ({top_count} of {summary['session_count']} sessions)."
    )

    task_mix = Counter(session.task_bucket for session in sessions)
    top_task, task_count = task_mix.most_common(1)[0]
    findings.append(
        f"`{display_task_bucket(top_task)}` is your dominant task bucket here ({task_count} sessions), which matters when comparing settings."
    )

    if report_mode != "basic":
        reasoning_buckets = comparable_buckets(
            sessions,
            label_for=lambda session: session.reasoning_effort,
            required_labels={label for label in ("medium", "xhigh") if summary.get(f"{label}_count", 0)},
            scope_for=lambda session: comparison_scope(session, report_mode),
        )
        if not reasoning_buckets:
            findings.append(
                "`medium` and `xhigh` are not yet being used within the same inferred project and task bucket often enough for a fair comparison."
            )
        else:
            findings.append(
                f"Comparable effort data exists in {len(reasoning_buckets)} inferred project/task scopes."
            )

    if summary["agent_count"] == 0:
        findings.append("No completed sessions in this window used `spawn_agent`, so the tool cannot judge agent value yet.")
    if summary["planning_count"] == 0:
        findings.append("No completed sessions in this window used `update_plan`, so plan-vs-no-plan advice is not available yet.")

    max_primary = summary.get("max_primary_used_percent") or 0.0
    max_secondary = summary.get("max_secondary_used_percent") or 0.0
    if max_primary < 50.0 and max_secondary < 50.0:
        findings.append(
            f"Observed usage pressure stays low so far: primary peaked at {max_primary:.1f}% and secondary at {max_secondary:.1f}%."
        )
    elif max_primary < 80.0 and max_secondary < 80.0:
        findings.append(
            f"Observed usage pressure is moderate: primary peaked at {max_primary:.1f}% and secondary at {max_secondary:.1f}%."
        )
    else:
        findings.append(
            f"Observed usage pressure is high in this window: primary peaked at {max_primary:.1f}% and secondary at {max_secondary:.1f}%."
        )
    return findings


def build_recommendations(
    sessions: list[SessionSummary],
    summary: dict[str, Any],
    *,
    report_mode: str,
) -> list[str]:
    recommendations: list[str] = []
    if not sessions:
        return recommendations
    reasoning_comparable = count_comparable_reasoning_samples(sessions, report_mode=report_mode)

    if report_mode == "basic":
        recommendations.append(
            "Use `--mode basic` when you want a clean factual dashboard only. Switch to `--mode hybrid` for project-scoped habit suggestions."
        )
        if summary["project_scoped_sessions"] < 6:
            recommendations.append(
                "Most recent sessions are still too mixed or shared to support fair within-project ROI comparisons; basic mode is the safer read for now."
            )
        return dedupe_lines(recommendations)

    reasoning_recommendation = compare_factor_efficiency(
        sessions,
        report_mode=report_mode,
        factor_name="reasoning",
        label_for=lambda session: session.reasoning_effort,
        cheaper_label="medium",
        expensive_label="xhigh",
    )
    if reasoning_recommendation:
        recommendations.append(reasoning_recommendation)

    planning_recommendation = compare_binary_factor(
        sessions,
        report_mode=report_mode,
        factor_name="planning",
        enabled_label="planned",
        enabled_filter=lambda session: session.planning_used,
        disabled_label="no-plan",
        disabled_filter=lambda session: not session.planning_used,
    )
    if planning_recommendation:
        recommendations.append(planning_recommendation)

    agent_recommendation = compare_binary_factor(
        sessions,
        report_mode=report_mode,
        factor_name="agents",
        enabled_label="agents",
        enabled_filter=lambda session: session.agent_used,
        disabled_label="no-agents",
        disabled_filter=lambda session: not session.agent_used,
    )
    if agent_recommendation:
        recommendations.append(agent_recommendation)

    if not recommendations:
        recommendations.append(
            "There is not enough like-for-like same-project evidence yet to tell you to lower reasoning, skip planning, or stop using agents."
        )

    max_primary = summary.get("max_primary_used_percent") or 0.0
    max_secondary = summary.get("max_secondary_used_percent") or 0.0
    if (
        summary["average_quality"]
        and summary["average_quality"] >= 4.1
        and summary["project_scoped_sessions"] >= 6
        and (max_primary >= 75.0 or max_secondary >= 75.0)
    ):
        throughput_confidence = "Low-confidence proxy" if summary["rated_count"] == 0 else "Medium-confidence"
        if reasoning_comparable == 0:
            recommendations.append(
                f"{throughput_confidence}: overall project-scoped output looks strong and usage pressure is high, so buying more usage may help more than habit tuning right now."
            )
        else:
            recommendations.append(
                f"{throughput_confidence}: project-scoped quality looks strong while usage pressure is high; buying more usage may be more valuable than squeezing settings harder."
            )
    elif max_primary < 50.0 and max_secondary < 50.0:
        recommendations.append(
            "There is no evidence yet that you need more usage; focus on habit calibration before spending more."
        )
    if summary["project_scoped_sessions"] < 6:
        recommendations.append(
            "Treat habit suggestions as tentative until you have more sessions that stay within one inferred project and task bucket."
        )

    return dedupe_lines(recommendations)


def compare_factor_efficiency(
    sessions: list[SessionSummary],
    *,
    report_mode: str,
    factor_name: str,
    label_for,
    cheaper_label: str,
    expensive_label: str,
) -> str | None:
    cheaper, expensive = comparable_sessions(
        sessions,
        scope_for=lambda session: comparison_scope(session, report_mode),
        label_for=label_for,
        first_label=cheaper_label,
        second_label=expensive_label,
    )
    if len(cheaper) < 3 or len(expensive) < 3:
        return None

    cheaper_quality = mean(session.quality_score for session in cheaper)
    expensive_quality = mean(session.quality_score for session in expensive)
    cheaper_tokens = mean((session.total_tokens or 0) for session in cheaper)
    expensive_tokens = mean((session.total_tokens or 0) for session in expensive)
    if cheaper_tokens <= 0 or expensive_tokens <= 0:
        return None

    confidence = recommendation_confidence(cheaper + expensive)
    quality_gap = expensive_quality - cheaper_quality
    token_ratio = expensive_tokens / cheaper_tokens
    if quality_gap <= 0.25 and token_ratio >= 1.35:
        return (
            f"{confidence}: for `{factor_name}`, `{expensive_label}` looks expensive relative to `{cheaper_label}` in comparable project scopes: "
            f"{token_ratio:.1f}x the tokens for only {quality_gap:+.2f} average quality difference."
        )
    if quality_gap >= 0.6 and token_ratio <= 1.8:
        return (
            f"{confidence}: for `{factor_name}`, `{expensive_label}` appears to earn its keep in comparable project scopes: "
            f"{quality_gap:+.2f} better average quality for {token_ratio:.1f}x the tokens."
        )
    return None


def compare_binary_factor(
    sessions: list[SessionSummary],
    *,
    report_mode: str,
    factor_name: str,
    enabled_label: str,
    enabled_filter,
    disabled_label: str,
    disabled_filter,
) -> str | None:
    enabled, disabled = comparable_binary_sessions(
        sessions,
        scope_for=lambda session: comparison_scope(session, report_mode),
        enabled_filter=enabled_filter,
        disabled_filter=disabled_filter,
    )
    if len(enabled) < 3 or len(disabled) < 3:
        return None

    enabled_quality = mean(session.quality_score for session in enabled)
    disabled_quality = mean(session.quality_score for session in disabled)
    enabled_tokens = mean((session.total_tokens or 0) for session in enabled)
    disabled_tokens = mean((session.total_tokens or 0) for session in disabled)
    if enabled_tokens <= 0 or disabled_tokens <= 0:
        return None

    confidence = recommendation_confidence(enabled + disabled)
    quality_gap = enabled_quality - disabled_quality
    token_ratio = enabled_tokens / disabled_tokens
    if quality_gap <= 0.2 and token_ratio >= 1.25:
        return (
            f"{confidence}: `{enabled_label}` sessions are not clearly outperforming `{disabled_label}` for `{factor_name}` inside comparable project scopes; "
            f"they cost {token_ratio:.1f}x the tokens with only {quality_gap:+.2f} quality difference."
        )
    if quality_gap >= 0.5 and token_ratio <= 1.8:
        return (
            f"{confidence}: `{enabled_label}` appears worthwhile for `{factor_name}` in comparable project scopes: "
            f"{quality_gap:+.2f} average quality at {token_ratio:.1f}x token cost."
        )
    return None


def build_next_steps(
    sessions: list[SessionSummary],
    summary: dict[str, Any],
    *,
    report_mode: str,
) -> list[str]:
    steps: list[str] = []
    comparable = count_comparable_reasoning_samples(sessions, report_mode=report_mode)
    if comparable < 6:
        steps.append(
            "Create a small same-project A/B sample before changing your default effort: run a few comparable tasks once at `medium` and once at `xhigh`."
        )

    if sum(1 for session in sessions if session.agent_used) == 0:
        steps.append(
            "If you want agent advice, deliberately log a few sessions that use `spawn_agent` on the same kind of task you normally do solo."
        )

    if sum(1 for session in sessions if session.planning_used) == 0:
        steps.append(
            "If you want plan advice, capture at least a few similar sessions with and without `update_plan`."
        )
    if report_mode != "basic" and summary["project_scoped_sessions"] < max(4, summary["session_count"] // 2):
        steps.append(
            "For better inference, keep a few sessions tightly scoped to one project folder so the tool can compare like with like."
        )
    return steps


def count_comparable_reasoning_samples(sessions: list[SessionSummary], *, report_mode: str) -> int:
    bucket_efforts: dict[str, set[str]] = defaultdict(set)
    comparable = 0
    for session in sessions:
        scope = comparison_scope(session, report_mode)
        if scope is None:
            continue
        bucket_efforts[scope].add(session.reasoning_effort)
    for session in sessions:
        scope = comparison_scope(session, report_mode)
        if scope is not None and len(bucket_efforts[scope]) >= 2:
            comparable += 1
    return comparable


def comparable_buckets(
    sessions: list[SessionSummary],
    *,
    label_for,
    required_labels: set[str],
    scope_for,
) -> list[str]:
    bucket_groups: dict[str, set[str]] = defaultdict(set)
    for session in sessions:
        scope = scope_for(session)
        if scope is None:
            continue
        bucket_groups[scope].add(label_for(session))
    return [
        bucket
        for bucket, labels in bucket_groups.items()
        if required_labels.issubset(labels)
    ]


def comparable_sessions(
    sessions: list[SessionSummary],
    *,
    scope_for,
    label_for,
    first_label: str,
    second_label: str,
) -> tuple[list[SessionSummary], list[SessionSummary]]:
    grouped: dict[str, dict[str, list[SessionSummary]]] = defaultdict(lambda: defaultdict(list))
    for session in sessions:
        scope = scope_for(session)
        if scope is None:
            continue
        grouped[scope][label_for(session)].append(session)

    first: list[SessionSummary] = []
    second: list[SessionSummary] = []
    for bucket_groups in grouped.values():
        first_bucket = bucket_groups.get(first_label, [])
        second_bucket = bucket_groups.get(second_label, [])
        if first_bucket and second_bucket:
            first.extend(first_bucket)
            second.extend(second_bucket)
    return first, second


def comparable_binary_sessions(
    sessions: list[SessionSummary],
    *,
    scope_for,
    enabled_filter,
    disabled_filter,
) -> tuple[list[SessionSummary], list[SessionSummary]]:
    grouped: dict[str, dict[str, list[SessionSummary]]] = defaultdict(lambda: defaultdict(list))
    for session in sessions:
        scope = scope_for(session)
        if scope is None:
            continue
        bucket = grouped[scope]
        if enabled_filter(session):
            bucket["enabled"].append(session)
        elif disabled_filter(session):
            bucket["disabled"].append(session)

    enabled: list[SessionSummary] = []
    disabled: list[SessionSummary] = []
    for bucket_groups in grouped.values():
        enabled_bucket = bucket_groups.get("enabled", [])
        disabled_bucket = bucket_groups.get("disabled", [])
        if enabled_bucket and disabled_bucket:
            enabled.extend(enabled_bucket)
            disabled.extend(disabled_bucket)
    return enabled, disabled


def group_breakdown(sessions: list[SessionSummary], key_func) -> list[dict[str, Any]]:
    groups: dict[str, list[SessionSummary]] = defaultdict(list)
    for session in sessions:
        groups[key_func(session)].append(session)

    rows: list[dict[str, Any]] = []
    for label, items in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        rows.append(
            {
                "label": label,
                "sessions": len(items),
                "manual_ratings": sum(1 for item in items if item.manual_rating is not None),
                "average_quality": round(mean(item.quality_score for item in items), 2),
                "average_tokens": round(mean((item.total_tokens or 0) for item in items), 0),
                "average_minutes": round(mean(item.duration_minutes for item in items), 1),
                "quality_per_100k_tokens": round(
                    mean(item.quality_score / max((item.total_tokens or 100_000) / 100_000, 1e-9) for item in items),
                    2,
                ),
            }
        )
    return rows


def dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return unique


def render_markdown_report(
    payload: dict[str, Any],
    *,
    recent_limit: int,
    timezone: ZoneInfo,
) -> str:
    summary = payload["summary"]
    sessions: list[SessionSummary] = payload["sessions"]
    project_usage_rows = payload["project_usage_rows"]
    daily_usage_rows = payload["daily_usage_rows"]
    heavy_session_rows = payload["heavy_session_rows"]
    expensive_patterns = payload["expensive_patterns"]
    lines: list[str] = []
    lines.append("# Codex Usage Monitor")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Snapshot")
    lines.append(f"- Completed sessions analyzed: {summary['session_count']}")
    lines.append(f"- Total measured tokens: {format_number(summary['total_tokens_sum'])}")
    lines.append(
        f"- Project coverage: {summary['project_scoped_sessions']} project-scoped sessions across "
        f"{summary['distinct_projects']} inferred project buckets"
    )
    lines.append(f"- Dominant inferred project: {display_project_name(summary['top_project'])} ({summary['top_project_sessions']} sessions)")
    lines.append(
        f"- Usage pressure peak: primary {summary['max_primary_used_percent']}%, "
        f"secondary {summary['max_secondary_used_percent']}%"
    )
    lines.append(
        f"- Setting coverage: medium {summary['medium_count']}, "
        f"xhigh {summary['xhigh_count']}, planning {summary['planning_count']}, agents {summary['agent_count']}"
    )
    lines.append("")
    lines.append("## Measurement Confidence")
    for item in payload["measurement_confidence"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Usage Reference")
    for item in payload["usage_reference"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Repeated Expensive Patterns")
    lines.append("")
    if expensive_patterns:
        lines.append("| project | task | effort | subagents | planning | sessions | tokens | share | avg/session | confidence mix |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for row in expensive_patterns:
            lines.append(
                f"| {row['project']} | {row['task_bucket']} | {row['reasoning']} | {row['subagents']} | {row['planning']} | "
                f"{row['sessions']} | {format_number(row['total_tokens'])} | {format_percent(row['token_share'])} | "
                f"{format_number(row['avg_tokens'])} | {row['confidence_mix']} |"
            )
    else:
        lines.append("- No repeated expensive patterns surfaced in this window yet.")
    lines.append("")
    lines.append("## Project Usage")
    lines.append("")
    lines.append("| project | sessions | tokens | share | primary peak | secondary peak | confidence mix |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in project_usage_rows:
        lines.append(
            f"| {display_project_name(row['project'])} | {row['sessions']} | {format_number(row['total_tokens'])} | {format_percent(row['token_share'])} | "
            f"{format_percent(row['primary_peak'])} | {format_percent(row['secondary_peak'])} | {row['confidence_mix']} |"
        )
    lines.append("")
    lines.append("## Heaviest Sessions")
    lines.append("")
    lines.append("| when | id | project | tokens | effort | ag | pl | title |")
    lines.append("| --- | --- | --- | ---: | --- | ---: | ---: | --- |")
    for row in heavy_session_rows:
        lines.append(
            f"| {row['when']} | {row['id']} | {row['project']} | {format_number(row['tokens'])} | {row['effort']} | {row['ag']} | {row['pl']} | {row['title']} |"
        )
    lines.append("")
    lines.append("## Advisory Read")
    if payload["recommendations"]:
        for recommendation in payload["recommendations"]:
            lines.append(f"- {recommendation}")
    else:
        lines.append("- No high-confidence habit changes surfaced in this window.")
    lines.append("")
    lines.append("## Factor Breakdown")
    for section_name, rows in payload["factor_breakdowns"].items():
        lines.append(f"### {section_name.replace('_', ' ').title()}")
        lines.append("")
        lines.append("| label | sessions | manual | avg quality | avg tokens | avg mins | quality / 100k |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in rows:
            lines.append(
                f"| {row['label']} | {row['sessions']} | {row['manual_ratings']} | "
                f"{row['average_quality']} | {int(row['average_tokens']):,} | "
                f"{row['average_minutes']} | {row['quality_per_100k_tokens']} |"
            )
    lines.append("")
    lines.append("## Burn Summary")
    for item in payload["burn_summary"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Daily Burn")
    lines.append("")
    lines.append("| day | sessions | tokens | primary peak | secondary peak | xhigh | agents | planned |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in daily_usage_rows:
        lines.append(
            f"| {row['day']} | {row['sessions']} | {format_number(row['total_tokens'])} | {format_percent(row['primary_peak'])} | "
            f"{format_percent(row['secondary_peak'])} | {row['xhigh_sessions']} | {row['agent_sessions']} | {row['planned_sessions']} |"
        )
    lines.append("")
    lines.append("## Recent Sessions")
    lines.append("")
    lines.append("| when | id | project | bucket | effort | ag | pl | tokens | quality | title |")
    lines.append("| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |")
    for session in sessions[:recent_limit]:
        lines.append(
            f"| {session.updated_at:%Y-%m-%d %H:%M} | {short_session_id(session.session_id)} | {project_label(session)} | {display_task_bucket(session.task_bucket)} | "
            f"{session.reasoning_effort} | {yes_no(session.agent_used)} | {yes_no(session.planning_used)} | "
            f"{format_tokens(session.total_tokens)} | {format_quality(session)} | "
            f"{trim_snippet(session.title, 48)} |"
        )
    lines.append("")
    lines.append("## Next Steps")
    for step in payload["next_steps"]:
        lines.append(f"- {step}")
    lines.append("")
    lines.append("## Label Guide")
    for item in payload["label_guide"]:
        lines.append(f"- {item['label']}: {item['description']}")
    return "\n".join(lines)


def render_html_report(
    payload: dict[str, Any],
    *,
    recent_limit: int,
    timezone: ZoneInfo,
) -> str:
    summary = payload["summary"]
    sessions: list[SessionSummary] = payload["sessions"]
    measurement_confidence = "".join(f"<li>{inline_markdown(item)}</li>" for item in payload["measurement_confidence"])
    usage_reference = "".join(f"<li>{inline_markdown(item)}</li>" for item in payload["usage_reference"])
    burn_summary = "".join(f"<li>{inline_markdown(item)}</li>" for item in payload["burn_summary"])
    recommendations = "".join(
        f"<li>{inline_markdown(item)}</li>" for item in (payload["recommendations"] or ["No strong recommendation yet."])
    )
    next_steps = "".join(f"<li>{inline_markdown(item)}</li>" for item in payload["next_steps"])
    factor_sections = "".join(
        render_factor_section(section_name, rows)
        for section_name, rows in payload["factor_breakdowns"].items()
    )
    project_usage_table = render_usage_table(
        headers=["Project", "Sessions", "Tokens", "Share", "Primary", "Secondary", "Confidence"],
        rows=[
            [
                html.escape(display_project_name(str(row["project"]))),
                str(row["sessions"]),
                format_number(row["total_tokens"]),
                format_percent(row["token_share"]),
                format_percent(row["primary_peak"]),
                format_percent(row["secondary_peak"]),
                html.escape(str(row["confidence_mix"])),
            ]
            for row in payload["project_usage_rows"]
        ],
    )
    daily_usage_table = render_usage_table(
        headers=["Day", "Sessions", "Tokens", "Primary", "Secondary", "Xhigh", "Agents", "Planned"],
        rows=[
            [
                html.escape(str(row["day"])),
                str(row["sessions"]),
                format_number(row["total_tokens"]),
                format_percent(row["primary_peak"]),
                format_percent(row["secondary_peak"]),
                str(row["xhigh_sessions"]),
                str(row["agent_sessions"]),
                str(row["planned_sessions"]),
            ]
            for row in payload["daily_usage_rows"]
        ],
    )
    heavy_session_table = render_usage_table(
        headers=["When", "ID", "Project", "Tokens", "Effort", "Ag", "Pl", "Title"],
        rows=[
            [
                html.escape(str(row["when"])),
                html.escape(str(row["id"])),
                html.escape(str(row["project"])),
                format_number(row["tokens"]),
                html.escape(str(row["effort"])),
                html.escape(str(row["ag"])),
                html.escape(str(row["pl"])),
                html.escape(str(row["title"])),
            ]
            for row in payload["heavy_session_rows"]
        ],
    )
    expensive_patterns_table = render_usage_table(
        headers=["Project", "Task", "Effort", "Subagents", "Planning", "Sessions", "Tokens", "Share", "Avg/session", "Confidence"],
        rows=[
            [
                html.escape(str(row["project"])),
                html.escape(str(row["task_bucket"])),
                html.escape(str(row["reasoning"])),
                html.escape(str(row["subagents"])),
                html.escape(str(row["planning"])),
                str(row["sessions"]),
                format_number(row["total_tokens"]),
                format_percent(row["token_share"]),
                format_number(row["avg_tokens"]),
                html.escape(str(row["confidence_mix"])),
            ]
            for row in payload["expensive_patterns"]
        ],
    )
    label_guide = "".join(
        (
            "<tr>"
            f"<td>{html.escape(item['label'])}</td>"
            f"<td>{html.escape(item['description'])}</td>"
            "</tr>"
        )
        for item in payload["label_guide"]
    )
    session_rows = "".join(render_session_row(session) for session in sessions[:recent_limit])
    generated_at = datetime.now(timezone).isoformat(timespec="seconds")

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Codex Usage Monitor</title>
    <style>
      :root {{
        --bg: #f6f1e7;
        --bg-alt: #efe4d2;
        --panel: rgba(255, 251, 245, 0.88);
        --panel-strong: rgba(255, 248, 239, 0.96);
        --ink: #1f1c1a;
        --muted: #6b6257;
        --line: rgba(73, 57, 41, 0.14);
        --accent: #b55233;
        --accent-soft: #f1c5ae;
        --teal: #2b6b6d;
        --gold: #b9852f;
        --good: #2c7a53;
        --warn: #aa6e21;
        --bad: #a64031;
        --shadow: 0 18px 60px rgba(66, 41, 25, 0.14);
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        font-family: "Avenir Next", "Gill Sans", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(241, 197, 174, 0.45), transparent 30%),
          radial-gradient(circle at top right, rgba(43, 107, 109, 0.18), transparent 28%),
          linear-gradient(180deg, var(--bg) 0%, #f8f4ed 45%, var(--bg-alt) 100%);
      }}

      .page {{
        width: min(1200px, calc(100vw - 32px));
        margin: 24px auto 48px;
      }}

      .hero {{
        position: relative;
        overflow: hidden;
        border: 1px solid var(--line);
        border-radius: 28px;
        padding: 28px;
        background:
          linear-gradient(135deg, rgba(255, 247, 237, 0.96), rgba(255, 252, 248, 0.88)),
          radial-gradient(circle at bottom right, rgba(185, 133, 47, 0.10), transparent 35%);
        box-shadow: var(--shadow);
      }}

      .hero::after {{
        content: "";
        position: absolute;
        right: -80px;
        top: -60px;
        width: 240px;
        height: 240px;
        border-radius: 50%;
        background: radial-gradient(circle, rgba(181, 82, 51, 0.18), transparent 70%);
      }}

      .eyebrow {{
        display: inline-flex;
        gap: 8px;
        align-items: center;
        padding: 7px 12px;
        border-radius: 999px;
        background: rgba(43, 107, 109, 0.10);
        color: var(--teal);
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.02em;
      }}

      h1, h2, h3 {{
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        margin: 0;
        line-height: 1.05;
      }}

      h1 {{
        margin-top: 14px;
        font-size: clamp(2.1rem, 5vw, 3.6rem);
        max-width: 12ch;
      }}

      .hero-copy {{
        margin-top: 14px;
        max-width: 62ch;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1.55;
      }}

      .hero-grid {{
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 14px;
        margin-top: 24px;
      }}

      .metric-card {{
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 18px;
        background: var(--panel);
        backdrop-filter: blur(8px);
      }}

      .metric-label {{
        color: var(--muted);
        font-size: 0.84rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}

      .metric-value {{
        margin-top: 10px;
        font-size: clamp(1.5rem, 4vw, 2.2rem);
        font-weight: 700;
      }}

      .metric-note {{
        margin-top: 6px;
        color: var(--muted);
        font-size: 0.92rem;
      }}

      .layout {{
        display: grid;
        grid-template-columns: minmax(0, 1.3fr) minmax(320px, 0.7fr);
        gap: 20px;
        margin-top: 20px;
      }}

      .panel {{
        border: 1px solid var(--line);
        border-radius: 24px;
        padding: 22px;
        background: var(--panel-strong);
        box-shadow: 0 16px 50px rgba(66, 41, 25, 0.08);
      }}

      .panel + .panel {{
        margin-top: 18px;
      }}

      .section-title {{
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 16px;
      }}

      .section-kicker {{
        color: var(--muted);
        font-size: 0.95rem;
      }}

      ul.clean {{
        margin: 0;
        padding-left: 18px;
        color: var(--ink);
        line-height: 1.55;
      }}

      ul.clean li + li {{
        margin-top: 10px;
      }}

      .factor-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 14px;
      }}

      .factor-card {{
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 18px;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.9), rgba(250, 243, 236, 0.86));
      }}

      table {{
        width: 100%;
        border-collapse: collapse;
      }}

      th {{
        text-align: left;
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--muted);
        padding-bottom: 8px;
      }}

      td {{
        padding: 10px 0;
        border-top: 1px solid var(--line);
        vertical-align: top;
        font-size: 0.95rem;
      }}

      .filters {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-bottom: 14px;
      }}

      .filter-chip {{
        border: 1px solid var(--line);
        background: rgba(255, 251, 245, 0.92);
        color: var(--ink);
        border-radius: 999px;
        padding: 9px 14px;
        font: inherit;
        cursor: pointer;
        transition: transform 140ms ease, background 140ms ease, border-color 140ms ease;
      }}

      .filter-chip:hover {{
        transform: translateY(-1px);
        border-color: rgba(181, 82, 51, 0.35);
      }}

      .filter-chip.active {{
        background: rgba(181, 82, 51, 0.12);
        border-color: rgba(181, 82, 51, 0.35);
        color: var(--accent);
      }}

      .search {{
        width: 100%;
        border-radius: 16px;
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.9);
        padding: 13px 15px;
        font: inherit;
        color: var(--ink);
        margin-bottom: 16px;
      }}

      .session-table td {{
        padding: 14px 10px 14px 0;
      }}

      .session-title {{
        font-weight: 700;
      }}

      .session-meta {{
        margin-top: 6px;
        color: var(--muted);
        font-size: 0.9rem;
        line-height: 1.45;
      }}

      .pill {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border-radius: 999px;
        padding: 5px 10px;
        font-size: 0.78rem;
        font-weight: 700;
        white-space: nowrap;
      }}

      .pill.proxy {{
        background: rgba(170, 110, 33, 0.14);
        color: var(--warn);
      }}

      .pill.manual {{
        background: rgba(44, 122, 83, 0.12);
        color: var(--good);
      }}

      .badge-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }}

      .command-list code {{
        display: block;
        padding: 12px 14px;
        border-radius: 14px;
        background: rgba(29, 25, 22, 0.96);
        color: #f7f1e7;
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        font-size: 0.88rem;
        overflow-x: auto;
      }}

      .command-list code + code {{
        margin-top: 10px;
      }}

      .muted {{
        color: var(--muted);
      }}

      .hidden {{
        display: none;
      }}

      @media (max-width: 980px) {{
        .layout,
        .hero-grid,
        .factor-grid {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="page">
      <section class="hero">
        <div class="eyebrow">Codex Usage Monitor</div>
        <h1>Usage patterns, made visible.</h1>
        <p class="hero-copy">
          A direct view of where tokens go, which setups repeat, and where usage pressure builds.
          Hard telemetry stays near the top. Lighter interpretation stays lower on the page.
          This view was last generated at {html.escape(generated_at)}. Refresh the page when you want newer data from the running monitor.
        </p>
        <div class="hero-grid">
          <article class="metric-card">
            <div class="metric-label">Sessions analyzed</div>
            <div class="metric-value">{summary['session_count']}</div>
            <div class="metric-note">Completed sessions in the current window</div>
          </article>
          <article class="metric-card">
            <div class="metric-label">Measured tokens</div>
            <div class="metric-value">{format_tokens(summary['total_tokens_sum'])}</div>
            <div class="metric-note">Subagents {summary['agent_count']}, planning {summary['planning_count']}</div>
          </article>
          <article class="metric-card">
            <div class="metric-label">Usage pressure</div>
            <div class="metric-value">{summary['max_primary_used_percent']}%</div>
            <div class="metric-note">Secondary peak: {summary['max_secondary_used_percent']}%</div>
          </article>
          <article class="metric-card">
            <div class="metric-label">Project coverage</div>
            <div class="metric-value">{summary['project_scoped_sessions']}</div>
            <div class="metric-note">{summary['distinct_projects']} inferred buckets, top {html.escape(display_project_name(str(summary['top_project'])))}</div>
          </article>
        </div>
      </section>

      <div class="layout">
        <main>
          <section class="panel">
            <div class="section-title">
              <h2>Measurement Confidence</h2>
              <div class="section-kicker">Generated {html.escape(generated_at)}</div>
            </div>
            <ul class="clean">{measurement_confidence}</ul>
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Usage Reference</h2>
              <div class="section-kicker">Harder signals first</div>
            </div>
            <ul class="clean">{usage_reference}</ul>
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Repeated Expensive Patterns</h2>
              <div class="section-kicker">Recurring setups that consume a lot</div>
            </div>
            {expensive_patterns_table if payload["expensive_patterns"] else "<p class='muted'>No repeated expensive patterns surfaced in this window yet.</p>"}
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Project usage</h2>
              <div class="section-kicker">Measured tokens and confidence mix</div>
            </div>
            {project_usage_table}
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Heaviest sessions</h2>
              <div class="section-kicker">Biggest measured token consumers</div>
            </div>
            {heavy_session_table}
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Advisory read</h2>
              <div class="section-kicker">Low-confidence unless proven otherwise</div>
            </div>
            <ul class="clean">{recommendations}</ul>
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Factor breakdown</h2>
              <div class="section-kicker">Quality vs cost by habit</div>
            </div>
            <div class="factor-grid">
              {factor_sections}
            </div>
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Burn summary</h2>
              <div class="section-kicker">Simple burn-rate read</div>
            </div>
            <ul class="clean">{burn_summary}</ul>
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Daily burn</h2>
              <div class="section-kicker">Pressure and setup by day</div>
            </div>
            {daily_usage_table}
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Recent sessions</h2>
              <div class="section-kicker">Showing {min(recent_limit, len(sessions))} sessions</div>
            </div>
            <div class="filters">
              <button class="filter-chip active" data-filter="all">All</button>
              <button class="filter-chip" data-filter="agents">Agents</button>
              <button class="filter-chip" data-filter="planned">Planned</button>
              <button class="filter-chip" data-filter="xhigh">Xhigh</button>
              <button class="filter-chip" data-filter="medium">Medium</button>
            </div>
            <input class="search" id="session-search" placeholder="Search title, project, note, task bucket, or session id" />
            <table class="session-table">
              <thead>
                <tr>
                  <th>Session</th>
                  <th>Setup</th>
                  <th>Cost</th>
                  <th>Quality signal</th>
                </tr>
              </thead>
              <tbody id="session-rows">
                {session_rows}
              </tbody>
            </table>
          </section>
        </main>

        <aside>
          <section class="panel">
            <div class="section-title">
              <h2>Label guide</h2>
              <div class="section-kicker">What the dashboard terms mean</div>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Meaning</th>
                </tr>
              </thead>
              <tbody>
                {label_guide}
              </tbody>
            </table>
          </section>

          <section class="panel">
            <div class="section-title">
              <h2>Next steps</h2>
              <div class="section-kicker">Best next moves</div>
            </div>
            <ul class="clean">{next_steps}</ul>
          </section>

          <section class="panel command-list">
            <div class="section-title">
              <h2>Useful commands</h2>
              <div class="section-kicker">Run in this repo</div>
            </div>
            <code>python3 scripts/codex_usage_monitor.py serve --days 21 --limit 30 --refresh-seconds 60 --port 8769</code>
            <code>python3 scripts/codex_usage_monitor.py report --days 21 --limit 30</code>
            <code>python3 scripts/codex_usage_monitor.py list --days 21 --limit 20</code>
          </section>
        </aside>
      </div>
    </div>

    <script>
      const chips = Array.from(document.querySelectorAll(".filter-chip"));
      const rows = Array.from(document.querySelectorAll("#session-rows tr"));
      const search = document.getElementById("session-search");
      let activeFilter = "all";

      function matchesFilter(row) {{
        switch (activeFilter) {{
          case "agents":
            return row.dataset.agent === "yes";
          case "planned":
            return row.dataset.planned === "yes";
          case "xhigh":
            return row.dataset.effort === "xhigh";
          case "medium":
            return row.dataset.effort === "medium";
          default:
            return true;
        }}
      }}

      function matchesSearch(row) {{
        const needle = search.value.trim().toLowerCase();
        if (!needle) return true;
        return row.dataset.search.includes(needle);
      }}

      function applyFilters() {{
        rows.forEach((row) => {{
          const visible = matchesFilter(row) && matchesSearch(row);
          row.classList.toggle("hidden", !visible);
        }});
      }}

      chips.forEach((chip) => {{
        chip.addEventListener("click", () => {{
          activeFilter = chip.dataset.filter;
          chips.forEach((item) => item.classList.toggle("active", item === chip));
          applyFilters();
        }});
      }});

      search.addEventListener("input", applyFilters);
      applyFilters();
    </script>
  </body>
</html>"""


def render_factor_section(section_name: str, rows: list[dict[str, Any]]) -> str:
    table_rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(row['label']))}</td>"
            f"<td>{row['sessions']}</td>"
            f"<td>{row['manual_ratings']}</td>"
            f"<td>{row['average_quality']}</td>"
            f"<td>{format_number(row['average_tokens'])}</td>"
            f"<td>{row['quality_per_100k_tokens']}</td>"
            "</tr>"
        )
        for row in rows
    )
    return (
        "<article class='factor-card'>"
        f"<div class='section-title'><h3>{html.escape(section_name.replace('_', ' ').title())}</h3>"
        f"<div class='section-kicker'>{len(rows)} groups</div></div>"
        "<table>"
        "<thead><tr><th>Label</th><th>Sessions</th><th>Manual</th><th>Avg quality</th><th>Avg tokens</th><th>Q / 100k</th></tr></thead>"
        f"<tbody>{table_rows}</tbody>"
        "</table>"
        "</article>"
    )


def render_usage_table(headers: list[str], rows: list[list[str]]) -> str:
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    row_html = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table>"


def render_session_row(session: SessionSummary) -> str:
    notes = session.manual_notes or ""
    outcome = session.manual_outcome or ""
    quality_pill = (
        f"<span class='pill {'manual' if session.manual_rating is not None else 'proxy'}'>"
        f"{html.escape(format_quality(session))}"
        "</span>"
    )
    badges = [
        f"<span class='pill proxy'>{html.escape(project_label(session))}</span>",
        f"<span class='pill proxy'>{html.escape(display_task_bucket(session.task_bucket))}</span>",
        f"<span class='pill proxy'>{html.escape(session.reasoning_effort)}</span>",
    ]
    if session.agent_used:
        badges.append("<span class='pill manual'>agents</span>")
    if session.planning_used:
        badges.append("<span class='pill manual'>planned</span>")

    search_blob = " ".join(
        [
            session.session_id,
            session.title,
            display_project_name(session.inferred_project),
            session.project_confidence,
            display_task_bucket(session.task_bucket),
            session.reasoning_effort,
            notes,
            outcome,
        ]
    ).lower()

    note_html = f"<div class='session-meta'>{html.escape(trim_snippet(notes, 120))}</div>" if notes else ""
    outcome_html = f"<div class='session-meta'>{html.escape(outcome)}</div>" if outcome else ""

    return (
        f"<tr data-agent=\"{'yes' if session.agent_used else 'no'}\" "
        f"data-planned=\"{'yes' if session.planning_used else 'no'}\" "
        f"data-effort=\"{html.escape(session.reasoning_effort)}\" "
        f"data-search=\"{html.escape(search_blob)}\">"
        "<td>"
        f"<div class='session-title'>{html.escape(trim_snippet(session.title, 92))}</div>"
        f"<div class='session-meta'>{html.escape(short_session_id(session.session_id))} | "
        f"{session.updated_at:%Y-%m-%d %H:%M} | {html.escape(session.model)}</div>"
        f"<div class='session-meta'>{html.escape(project_hint(session.inferred_project))}</div>"
        f"{note_html}"
        "</td>"
        "<td>"
        f"<div class='badge-row'>{''.join(badges)}</div>"
        f"{outcome_html}"
        "</td>"
        "<td>"
        f"<div class='session-title'>{html.escape(format_tokens(session.total_tokens))}</div>"
        f"<div class='session-meta'>{session.duration_minutes:.1f} mins</div>"
        "</td>"
        f"<td>{quality_pill}<div class='session-meta'>{html.escape(session.quality_source)}</div></td>"
        "</tr>"
    )


def rating_guide() -> list[dict[str, str]]:
    return [
        {
            "score": "1",
            "label": "Miss",
            "description": "Wrong direction or not usable. You would not want the same setup again.",
        },
        {
            "score": "2",
            "label": "Weak",
            "description": "Partly useful, but heavy rewriting, correction, or rework was needed.",
        },
        {
            "score": "3",
            "label": "Mixed",
            "description": "Usable enough, but not efficient. You are unsure the cost was worth it.",
        },
        {
            "score": "4",
            "label": "Good",
            "description": "Solid result with minor cleanup. You would probably use the same setup again.",
        },
        {
            "score": "5",
            "label": "Strong",
            "description": "Very good result. Fast enough, clear enough, and worth repeating on similar tasks.",
        },
    ]


def inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    parts = escaped.split("`")
    if len(parts) == 1:
        return escaped
    chunks: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            chunks.append(f"<code>{part}</code>")
        else:
            chunks.append(part)
    return "".join(chunks)


def format_number(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        rounded = int(round(value))
    else:
        rounded = value
    return f"{rounded:,}"


def format_tokens(value: int | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def format_quality(session: SessionSummary) -> str:
    if session.manual_rating is not None:
        return f"{session.manual_rating}/5 manual"
    return f"{session.proxy_quality:.1f} proxy"


def flag(enabled: bool) -> str:
    return "Y" if enabled else "-"


def yes_no(enabled: bool) -> str:
    return "yes" if enabled else "no"


def short_session_id(session_id: str) -> str:
    return session_id[:13]


if __name__ == "__main__":
    raise SystemExit(main())
