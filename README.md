![tg_run](hero.png)

# tg_run — launch Claude Code from Telegram

Text your home PC (Windows 11 or macOS) from Telegram to launch Claude Code in
any project folder. The bot opens a terminal with `claude` running there,
already in `--remote-control` mode — so you can keep steering the session from
your phone ([see below](#continue-from-your-phone)).

## Commands

| Command | Action |
|---|---|
| `/claude <folder>` | Open a terminal with Claude Code in a project folder |
| `/favorite` | Launch a preconfigured project (`favorites` in `config.toml`) from buttons |
| `/list [folder]` | List projects (or the contents of a subfolder) |

Projects can live in several root directories (`base_dirs` in `config.toml`) —
for example, on different drives. `<folder>` is a folder name or a relative
path like `group/proj`, so nested project folders work too. If the name exists
in several roots, the bot asks where to launch; if it is not found, the bot
offers buttons to create it and launch right away.

## Continue from your phone

Once the bot has opened Claude Code on the PC, you can keep steering that same
session from the [Claude mobile app](https://claude.ai/download) (or
[claude.ai/code](https://claude.ai/code)) via Claude Code's
[Remote Control](https://code.claude.com/docs/en/remote-control). Claude keeps
running locally — the phone is just a window into the session.

Every launched session shows up on your phone automatically: the bot starts
Claude Code with the `--remote-control` flag (see the `command` in
`config.toml`), so Remote Control is on per session — no need to enable it
globally in Claude Code settings or type `/remote-control` by hand.

The launch folder is also marked as trusted automatically, so the session isn't
blocked by Claude Code's "Do you trust the files in this folder?" dialog — which
you couldn't answer remotely. Before each launch the bot records the folder as
trusted in `~/.claude.json` (details in
[DETAILS.md](DETAILS.md#folder-trust-dialog)).

Requires signing in with a claude.ai account (Pro/Max/Team/Enterprise) via
`/login` and Claude Code v2.1.51+. Then open the app, tap **Code**, and pick the
session from the list.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Windows 11 or macOS.

**Windows:**

```powershell
git clone <repo-url> C:\tools\tg_run   # clone into a PERMANENT folder (see note below)
cd C:\tools\tg_run

copy config.example.toml config.toml    # then edit config.toml: bot_token, base_dirs, allowed_user_ids

uv sync                                 # create .venv and install dependencies
uv run bot.py                           # first run in the foreground to check it works
```

**macOS:**

```bash
git clone <repo-url> ~/tools/tg_run     # clone into a PERMANENT folder (see note below)
cd ~/tools/tg_run

cp config.example.toml config.toml      # then edit config.toml: bot_token, base_dirs, allowed_user_ids

uv sync                                 # create .venv and install dependencies
uv run bot.py                           # first run in the foreground to check it works
```

Get your token from [@BotFather](https://t.me/BotFather) and put it into
`bot_token`. Find your Telegram ID (e.g. via
[@userinfobot](https://t.me/userinfobot)), put it into `allowed_user_ids` in
`config.toml`, and restart. Once it works, set up autostart
(below).

> **The bot runs from this folder — keep it in a permanent location.** Nothing
> is copied elsewhere: the code, `.venv`, `config.toml` and `bot.log`
> all live here, and the autostart mechanism points at this path. If you
> delete or move the folder, the bot stops working (after moving, re-run
> `install_task.ps1` / `install_agent.sh` to re-register with the new path).
> Put it somewhere stable, not in Downloads or a temp folder.

> **macOS target:** designed for a desktop Mac (Mac mini/Studio/iMac) that's
> always on and logged in, the same way the Windows setup assumes an
> always-on desktop PC. On a MacBook, display/system sleep will make the bot
> unreachable until the machine wakes — disable sleep for it in System
> Settings > Battery/Energy, or don't rely on it while the lid is closed.

## Configuration

- `config.toml` (not committed; copy from `config.example.toml`):
  - `bot_token` — the Telegram bot token from @BotFather;
  - `base_dirs` — the list of project root directories (launching is only possible inside them);
  - `allowed_user_ids` — the list of Telegram IDs allowed to launch;
  - `allow_create` — whether new folders may be created;
  - `command` — the terminal launch command (`{path}` is substituted).

## Autostart in the background

**Windows:** to run unattended and start at every log on, register a Task
Scheduler task (from the project folder):

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

Manage it:

```powershell
Start-ScheduledTask tg_run                                      # start
Stop-ScheduledTask  tg_run                                      # stop
powershell -ExecutionPolicy Bypass -File .\restart_task.ps1     # restart (pick up a code change)
powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1   # remove
```

**macOS:** to run unattended and start at every log in, register a LaunchAgent
(from the project folder):

```bash
./install_agent.sh
```

Manage it:

```bash
launchctl kickstart -k gui/$(id -u)/com.tgrun.bot   # (re)start
launchctl bootout gui/$(id -u)/com.tgrun.bot        # stop
./restart_agent.sh                                  # restart (pick up a code change)
./uninstall_agent.sh                                # remove
```

The first time the bot launches Claude Code, macOS will ask permission for it
to control Terminal.app (System Settings > Privacy & Security > Automation) —
trigger a `/claude` command once right after installing, while at the
keyboard, to grant it.

Don't also run `uv run bot.py` at the same time: two polling clients on one
token cause a `409 Conflict`.

## More

See **[DETAILS.md](DETAILS.md)** for how it works under the hood — the autostart
mechanism (Task Scheduler / LaunchAgent, no-console launch), logging, the
security model, window behavior, and Claude Code's folder-trust dialog.
