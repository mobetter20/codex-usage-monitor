#!/bin/zsh
set -euo pipefail

ROOT="${CODEX_USAGE_MONITOR_ROOT:-${0:A:h:h}}"
PORT="${CODEX_USAGE_MONITOR_PORT:-8769}"
REFRESH_SECONDS="${CODEX_USAGE_MONITOR_REFRESH_SECONDS:-60}"
DAYS="${CODEX_USAGE_MONITOR_DAYS:-21}"
LIMIT="${CODEX_USAGE_MONITOR_LIMIT:-20}"
RECENT_LIMIT="${CODEX_USAGE_MONITOR_RECENT_LIMIT:-12}"

cd "$ROOT"
mkdir -p tmp/codex_usage

exec /usr/bin/env python3 scripts/codex_usage_monitor.py serve \
  --days "$DAYS" \
  --limit "$LIMIT" \
  --recent-limit "$RECENT_LIMIT" \
  --refresh-seconds "$REFRESH_SECONDS" \
  --port "$PORT"
