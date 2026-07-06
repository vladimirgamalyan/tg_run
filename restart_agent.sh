#!/bin/bash
# Restarts the tg_run LaunchAgent so a code change is picked up.
#
# Unlike the Windows restart_task.ps1, no PID-hunting is needed: launchctl
# kickstart -k stops the current instance and starts a fresh one atomically.
#
# Run (from the project folder):
#   ./restart_agent.sh

set -euo pipefail

LABEL="com.tgrun.bot"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
DOMAIN="gui/$(id -u)"

if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "error: agent '$LABEL' not found. Install it first: ./install_agent.sh" >&2
    exit 1
fi

launchctl kickstart -k "$DOMAIN/$LABEL"
echo "Bot restarted. Logs: $PROJECT_DIR/bot.log"
