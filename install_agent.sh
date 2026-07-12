#!/bin/bash
# Registers the tg_run bot as a macOS LaunchAgent (auto-start at login).
#
# Why a LaunchAgent and not a LaunchDaemon: the bot opens a VISIBLE Terminal.app
# window via AppleScript, and that only works from the logged-in user's GUI
# session — a LaunchDaemon runs outside any user session and could not open
# windows at all. Unlike on Windows, no console-hiding trick is needed here:
# a LaunchAgent process has no window of its own regardless.
#
# Run (from the project folder):
#   ./install_agent.sh
#
# First run only: macOS will prompt to let this process control Terminal.app
# (System Settings > Privacy & Security > Automation). That prompt needs a
# human to click "Allow" and will not appear/succeed unattended — trigger a
# /run command once via Telegram right after installing, while you're at
# the keyboard, to grant it.

set -euo pipefail

LABEL="com.tgrun.bot"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python3"
BOT="$PROJECT_DIR/bot.py"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG="$PROJECT_DIR/launchd.log"
DOMAIN="gui/$(id -u)"

if [ ! -x "$PYTHON" ]; then
    echo "error: $PYTHON not found. Create the environment first: uv sync" >&2
    exit 1
fi
if [ ! -f "$BOT" ]; then
    echo "error: bot.py not found in $PROJECT_DIR" >&2
    exit 1
fi

# Idempotency: if the agent is already loaded, unload it first — bootstrap
# would otherwise fail with "already loaded" for an existing label. bootout is
# asynchronous, so wait for the job to actually disappear before bootstrapping,
# otherwise the immediate bootstrap can race and fail with an I/O error.
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 0.1
    done
fi

# Escape XML metacharacters so a project path containing & < or > (all legal in
# a macOS folder name) can't produce a malformed plist. & must be replaced
# first, before < and >, or the entities themselves get double-escaped.
xml_escape() {
    local s=$1
    s=${s//&/&amp;}
    s=${s//</&lt;}
    s=${s//>/&gt;}
    printf '%s' "$s"
}
PYTHON_XML=$(xml_escape "$PYTHON")
BOT_XML=$(xml_escape "$BOT")
PROJECT_DIR_XML=$(xml_escape "$PROJECT_DIR")
LOG_XML=$(xml_escape "$LOG")

mkdir -p "$PLIST_DIR"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_XML</string>
        <string>$BOT_XML</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR_XML</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$LOG_XML</string>
    <key>StandardErrorPath</key>
    <string>$LOG_XML</string>
</dict>
</plist>
PLIST_EOF

# RunAtLoad above already starts the job — no kickstart needed. A `kickstart -k`
# here would SIGKILL the just-started bot and relaunch it, which looks like a
# crash in bot.log.
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "Agent '$LABEL' registered and started."
echo "Logs: $PROJECT_DIR/bot.log (launchd stdout/stderr backstop: $LOG)"
echo "Note: do not keep a second bot instance running manually (Telegram will return 409 Conflict)."
echo "First launch: macOS will ask to let this process control Terminal.app — trigger a /run command once now, at the keyboard, to grant it."
