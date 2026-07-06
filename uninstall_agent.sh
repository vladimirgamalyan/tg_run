#!/bin/bash
# Stops and removes the tg_run LaunchAgent.
# Run: ./uninstall_agent.sh

set -euo pipefail

LABEL="com.tgrun.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    # `|| true`: during a KeepAlive crash-restart loop bootout can return
    # non-zero ("Operation now in progress"); don't let set -e abort the script
    # before the plist below is removed.
    launchctl bootout "$DOMAIN/$LABEL" || true
    echo "Agent '$LABEL' stopped."
else
    echo "Agent '$LABEL' not loaded — nothing to stop."
fi

if [ -f "$PLIST" ]; then
    rm "$PLIST"
    echo "Removed $PLIST"
else
    echo "$PLIST not found — nothing to remove."
fi
