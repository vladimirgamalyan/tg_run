# tg_run — how it works

Detailed notes on the autostart mechanism, logging, security and Claude Code's
folder-trust dialog. For installation and usage see [README.md](README.md).

## Autostart on Windows

For the terminal window to be **visible** on the desktop, the bot must run in
an interactive user session, not as a session-0 service. A service via NSSM
won't do: the terminal window would be invisible. Hence — a Task Scheduler task
with an "At log on" trigger, registered by `install_task.ps1`.

The script registers the `tg_run` task and starts it immediately. What it does:

- **starts at log on** of the current user, in their interactive session
  (`wt.exe` windows are visible on screen);
- launches the **supervisor** (`supervisor.py`), which keeps `bot.py` alive and
  **restarts it on crash** — up to 3 times, with a 15 s pause between attempts.
  If the bot ran for ≥ 60 s before crashing, the counter is reset (isolated
  failures don't accumulate). Once those 3 attempts are exhausted (the bot keeps
  crashing fast), the supervisor writes `CRITICAL` to the log and stops —
  protection against an infinite loop on an unrecoverable
  error (bad token, no `base_dir`). The scheduler's built-in "Restart on
  failure" is **not used**: on this system it does not fire even on a non-zero
  exit code;
- launches via the **base GUI `pythonw.exe`** (its path is taken from
  `.venv\pyvenv.cfg`, the `home` key) — no console is created, no window. The
  venv `.venv\Scripts\pythonw.exe` won't do: on Windows 11 it still opens a
  console window. The venv packages are wired up by the bot itself
  (`site.addsitedir` in `bot.py`);
- with no run-time limit.

In the task manager / process list you'll see two `pythonw.exe` processes: the
supervisor (`supervisor.py`) and its child bot (`bot.py --hidden`) — this is
normal, only the bot does the polling. If the supervisor gave up after a series
of crashes — fix the cause from the log and start the task again:
`Start-ScheduledTask tg_run`. The restart parameters (number of attempts,
pauses) are constants at the top of `supervisor.py`.

## Managing the task

```powershell
Start-ScheduledTask   tg_run     # start
Stop-ScheduledTask    tg_run     # stop
Get-ScheduledTask     tg_run | Get-ScheduledTaskInfo   # status, last exit code
powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1   # remove the task
```

The task is also visible in the GUI: `taskschd.msc` → "Task Scheduler Library" → `tg_run`.

> **Do not keep a second instance running manually** (`uv run bot.py`): two
> polling clients on one token cause the Telegram error `409 Conflict`.

## Security

- Access by a Telegram User ID whitelist only.
- Launching is only possible inside `base_dir`; folder names are validated,
  escaping the base directory (`..`, absolute paths, separators) is forbidden.
- Only the configured Claude Code command is launched — arbitrary commands and
  paths are not supported.

## Logs and error notifications

- Written to `bot.log` next to the script, with **rotation** (up to 1 MB × 5
  files) — straight from Python, so they work even under `pythonw.exe` without
  a console.
- The level — `[logging] level` in `config.toml`.
- Unhandled exceptions and fatal crashes are logged with a traceback **before**
  the process exits — so the cause isn't lost after a restart.
- On an error in a command handler the bot sends an **alert to Telegram** (with
  anti-flood). The recipient is `[telegram] alert_chat_id`, or if it's `0` — the
  first of `allowed_user_ids`.

## A note on window behavior

- `-w new` forces Windows Terminal to open a **separate new window**. Without
  it, `wt` by default adds a tab to an already open window — easy to miss.
- The terminal opens in **PowerShell 7** (`pwsh`); claude runs inside it.
- `-NoExit` keeps the window open after `claude` exits (you can see the output
  and run it again). Remove `-NoExit` if you want the window to close.
- `--remote-control` is passed to `claude` (pwsh's `-Command` appends trailing
  tokens to the command it runs), enabling Remote Control for that session
  regardless of the global setting.

## Folder trust dialog

On the first launch in a new folder, Claude Code interactively asks "Do you
trust the files in this folder?", and the session is unavailable until you
answer — a blocker for remote launching. So **before every launch the bot marks
the folder as trusted**: it appends to `~/.claude.json` an entry
`projects["<path>"].hasTrustDialogAccepted = true` (the key uses forward
slashes, as Claude does; the write is atomic, existing projects are preserved).

The `--dangerously-skip-permissions` flag is deliberately **not** used: in
version 2.1.x it does not dismiss the trust dialog anyway, yet it triggers a
separate one-off "Bypass Permissions mode" confirmation. Ordinary permission
prompts during a session remain — with a remote connection you can confirm them
manually.
