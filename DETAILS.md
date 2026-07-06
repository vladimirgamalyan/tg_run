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
- launches **`bot.py`** directly (with `--hidden`) — there is no separate
  supervisor process. The scheduler's built-in "Restart on failure"
  (`RestartCount` 3×, 1 min) is configured as a best-effort fallback, but on
  this system it does **not** reliably fire even on a non-zero exit code, so
  treat a crash as needing a manual restart (`Start-ScheduledTask tg_run` —
  check the cause in `bot.log` first);
- launches via the **base GUI `pythonw.exe`** (its path is taken from
  `.venv\pyvenv.cfg`, the `home` key) — no console is created, no window. The
  venv `.venv\Scripts\pythonw.exe` won't do: on Windows 11 it still opens a
  console window. The venv packages are wired up by the bot itself
  (`site.addsitedir` in `bot.py`);
- with no run-time limit.

At logon the network is often not up yet, so before its first API call the bot
**waits for Telegram to become reachable** (a `getMe` probe, retried every 5 s)
instead of crashing and relying on the unreliable scheduler restart.

In the task manager / process list you'll see a single `pythonw.exe` process
running `bot.py --hidden` — that one process does the polling.

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
- Launching is only possible inside one of the configured `base_dirs`; folder
  paths are validated per segment, escaping the roots (`..`, absolute paths,
  UNC paths) is forbidden.
- Only the configured Claude Code command is launched — arbitrary commands and
  paths are not supported.
- **The trust chain to keep in mind:** anyone who controls an allowed Telegram
  account can open a Claude Code session on this PC in an auto-trusted folder
  with `--remote-control`, and whoever is signed in to the claude.ai account
  can drive that session — effectively code execution on the machine. Protect
  both accounts accordingly.
- `ensure_trusted` rewrites `~/.claude.json` (read–modify–write with an atomic
  replace). If a running Claude Code instance saves the file in that same
  instant, its change can be lost. The window is tiny and the bot writes only
  when the folder isn't trusted yet, but the race exists by design.

## Folder-path validation

Both `/claude <folder>` and the inline buttons run the input through
`validate_path` (`bot.py`). It normalises first, then rejects: an invalid path
gets a plain "⛔ Invalid folder name." and never reaches the filesystem. The
rules:

- surrounding whitespace and double quotes are stripped (`"my folder"` →
  `my folder`; a space **inside** a name is allowed); backslashes are
  normalised to `/`, then the path is split into segments and each segment is
  checked on its own;
- rejected: an empty path, a leading or trailing slash and empty segments
  (`a//b`) — this also kills absolute and UNC paths;
- rejected segments: `.`, `..`, or anything containing `..` (traversal);
- rejected: the Windows-forbidden characters `< > : " | ? *` and control
  characters (`0x00–0x1F`) — `:` also blocks drive-absolute paths like `C:/x`;
- rejected: `%` — legal in a Windows folder name, but the launch command runs
  through `cmd.exe`, which expands `%VAR%` even inside quotes;
- rejected: the Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`,
  `COM1`–`COM9`, `LPT1`–`LPT9`), with or without an extension;
- rejected: a segment with leading/trailing whitespace or a **trailing dot**.
  Windows silently trims these, so `foo.` would land on disk as `foo` — a
  mismatch with the name the bot echoed back — hence it refuses instead;
- finally, the resolved path must stay **strictly inside the root**
  (`resolve()` + `is_relative_to`, see `resolve_under`) — a safety net on top
  of the segment checks, e.g. against symlinks/junctions pointing outside.

When the same relative path exists in several roots, `/claude` replies with one
button per root (`callback_data = "run:<root index>:<path>"`) and launches in
the one you pick.

## Creating a folder from the button

When `/claude` names a folder that doesn't exist (and `allow_create` is on), the
reply carries an inline button *"➕ Create in \<root\>"* per root
(`callback_data = "new:<root index>:<path>"`, built in `cmd_claude`). A button
is only offered for roots where the **parent** of the path already exists:
`experiments/newproj` needs an existing `experiments`. Missing chains are never
created (`mkdir` without `parents`), so a typo in the group name can't silently
grow a whole new tree. The button is **not** removed or disabled after use — it
stays under the message, so it can be pressed again.

Pressing it a second time is **idempotent and safe**. The handler
(`cb_new` → `create_and_run`) re-resolves the path through `resolve_under` (the
symlink/junction safety net applies at press time, not only when the button was
offered) and re-checks `target.exists()` before doing anything: if the folder
was already created by the first press, it does **not** re-create it and does
**not** launch a second Claude Code terminal — it just replies that the folder
already exists and suggests `/claude <name>`.

The check-then-create region (`target.exists()` … `target.mkdir(exist_ok=False)`)
contains no `await`, so on the single-threaded asyncio loop even a rapid
double-tap is safe: exactly one press creates the folder and launches, the others
see `exists()` and get the "already exists" reply — no duplicate folder, no
`FileExistsError`. Edge cases: if the folder was deleted between presses, a later
press re-creates and launches it; if a non-directory with that name exists, the
reply says it exists and is not a folder.

## Logs

- Written to `bot.log` next to the script, with **rotation** (up to 1 MB × 5
  files) — straight from Python, so they work even under `pythonw.exe` without
  a console.
- The level — `[logging] level` in `config.toml`.
- Unhandled exceptions and fatal crashes are logged with a traceback **before**
  the process exits — so the cause isn't lost after a restart.
- An error in a command handler is logged with its traceback; polling keeps
  running. There are no Telegram error alerts — the log is the single place to
  look.

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
