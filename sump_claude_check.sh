#!/bin/bash
# sump_claude_check.sh — durable LOCAL "Claude wakes up and is smart" pass.
# Invoked by launchd (com.sump.claude-check) every few hours. Runs the claude
# CLI headlessly with a tight, cost-capped prompt to reason about the sump
# situation on top of the deterministic sump_assess.py fast layer.
#
# Local + on this Mac on purpose: it needs LAN access to the Shelly and the
# local logs, which a remote/cloud agent cannot reach.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Long-lived OAuth token from `claude setup-token` (Claude Max). Stored outside
# the repo with 600 perms so it is never committed. Headless claude --print reads
# CLAUDE_CODE_OAUTH_TOKEN from the environment.
TOKEN_FILE="${CLAUDE_OAUTH_TOKEN_FILE:-$HOME/.config/sump/claude_oauth_token}"
if [ -r "$TOKEN_FILE" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")"
fi

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
PROMPT="$(cat "$SCRIPT_DIR/sump_claude_prompt.txt")"

echo "===== claude-check $STAMP =====" >> sump_claude_check.launchd.log

# --print: headless. Tight allowed tools. Cheap model. Hard dollar cap so a
# stuck run can never run away. Portable timeout backstop (macOS lacks
# /usr/bin/timeout): run claude in the background, kill it after 300s.
claude \
    --print \
    --model sonnet \
    --allowedTools "Bash Read" \
    --max-budget-usd 0.50 \
    "$PROMPT" >> sump_claude_check.launchd.log 2>&1 &
CLAUDE_PID=$!
( sleep 300; kill -TERM "$CLAUDE_PID" 2>/dev/null; sleep 5; kill -KILL "$CLAUDE_PID" 2>/dev/null ) &
WATCH_PID=$!
wait "$CLAUDE_PID"
RC=$?
kill "$WATCH_PID" 2>/dev/null
echo "----- claude-check exit=$RC $(date '+%H:%M:%S') -----" >> sump_claude_check.launchd.log
exit 0
