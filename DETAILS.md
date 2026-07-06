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

## Autostart on macOS

The equivalent mechanism is a **LaunchAgent** (not a LaunchDaemon): the bot
opens a visible `Terminal.app` window via AppleScript, which only works from
the logged-in user's GUI session — a LaunchDaemon runs outside any user
session and could not open windows. `install_agent.sh` registers it.

Unlike on Windows, there is **no console-hiding trick needed at all**: a
process started by `launchd` has no window of its own regardless of which
Python interpreter runs it, so the bot's own venv interpreter
(`.venv/bin/python3`) is used directly — no base/venv trampoline distinction,
no `--hidden` flag.

What the LaunchAgent does:

- **starts at login** of the current user (`RunAtLoad`), in their GUI session
  (`Terminal.app` windows are visible on screen);
- **restarts on crash** (`KeepAlive.SuccessfulExit = false`, throttled to at
  most once per minute via `ThrottleInterval`) — more reliably than the Windows
  scheduler's restart count, since `launchd` supervises the process directly
  rather than through a scheduler polling exit codes. `launchd` has no
  give-up-after-N option, so a persistent startup failure (e.g. a typo in
  `config.toml`) restarts on that interval indefinitely — check `bot.log`;
- stdout/stderr go to `launchd.log` next to the script, as a low-volume
  backstop (the full log stream stays in the rotated `bot.log`; under `launchd`
  stderr is not a TTY, so the app does not add its console handler) —
  `bot.log` (the app's own `RotatingFileHandler`) is still the primary log.

```bash
launchctl kickstart -k gui/$(id -u)/com.tgrun.bot   # (re)start
launchctl bootout   gui/$(id -u)/com.tgrun.bot      # stop (unloads the job — no auto-restart)
launchctl print     gui/$(id -u)/com.tgrun.bot      # status
./uninstall_agent.sh                                # remove
```

`restart_agent.sh` just runs `launchctl kickstart -k`, which atomically stops
and restarts the job — no PID-hunting is needed like on Windows, since
`launchctl` owns the process directly and always knows which one it started.

> **Do not keep a second instance running manually** (`uv run bot.py`): two
> polling clients on one token cause the Telegram error `409 Conflict`.

**Automation permission.** The first time the bot runs `osascript` to control
`Terminal.app`, macOS shows a one-time "`<process>` wants access to control
`Terminal`" prompt (System Settings → Privacy & Security → Automation). It
needs a human to click "Allow" and cannot be granted unattended — trigger a
`/claude` command once right after installing, while at the keyboard.

**Using iTerm2 instead of Terminal.app:** set `command` in `config.toml`. A TOML
multi-line literal string (`'''...'''`) avoids having to escape the quotes
`osascript`/AppleScript need:

```toml
command = '''osascript -e 'tell application "iTerm" to activate' -e 'tell application "iTerm" to create window with default profile command ("bash -c " & quoted form of ("cd " & quoted form of "{path}" & " && claude --remote-control"))' '''
```

(iTerm runs a window's `command` directly — argv-style, not through a shell —
so `cd … && …` must be wrapped in an explicit `bash -c`, otherwise iTerm tries
to exec the literal token `cd`. The inner `quoted form of` shell-quotes
`{path}`; the outer one quotes the whole `bash -c` argument as a single token.
`create window with default profile` takes no working directory of its own,
which is why the command `cd`s in itself.)

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
- rejected **on macOS only**: `'` — legal in a folder name on either OS, but
  the macOS default launch command embeds `{path}` inside a single-quoted
  `osascript -e '...'` shell argument (see [A note on window
  behavior](#a-note-on-window-behavior)); a literal `'` would break out of that
  quoting. On Windows the command double-quotes `{path}`, so `'` is allowed
  there;
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

## Default launch commands

When `[launch] command` is left unset in `config.toml`, the bot uses a built-in
default for the current OS (`_DEFAULT_LAUNCH_COMMANDS` in `config.py`). `{path}`
is replaced with the absolute folder path at launch time. To customize (e.g. to
close the window after `claude` exits), copy the relevant line into
`config.toml` and edit it.

**Windows** — Windows Terminal, a separate new window running PowerShell 7:

```toml
command = 'wt.exe -w new -d "{path}" pwsh -NoLogo -NoExit -Command claude --remote-control'
```

**macOS** — Terminal.app via AppleScript. `do script` runs before `activate` so
a cold start (Terminal not yet running) opens a single window:

```toml
command = '''osascript -e 'tell application "Terminal" to do script "cd " & quoted form of "{path}" & " && claude --remote-control"' -e 'tell application "Terminal" to activate' '''
```

On an unlisted platform (e.g. Linux) there is no built-in default — the bot
refuses to start until `[launch] command` is set.

## A note on window behavior

**Windows:**

- `-w new` forces Windows Terminal to open a **separate new window**. Without
  it, `wt` by default adds a tab to an already open window — easy to miss.
- The terminal opens in **PowerShell 7** (`pwsh`); claude runs inside it.
- `-NoExit` keeps the window open after `claude` exits (you can see the output
  and run it again). Remove `-NoExit` if you want the window to close.
- `--remote-control` is passed to `claude` (pwsh's `-Command` appends trailing
  tokens to the command it runs), enabling Remote Control for that session
  regardless of the global setting.

**macOS:**

- `do script` with no target window opens a **separate new Terminal.app
  window** each time (mirroring `-w new` on Windows); it runs *before*
  `activate` so a cold start (Terminal not yet running) opens just one window,
  and `activate` then brings it to the front.
- `quoted form of` asks AppleScript to shell-quote `{path}` against shell
  metacharacters (spaces, `$`, backticks, …) before Terminal's own shell sees
  it. It does **not** protect the AppleScript layer: a literal `"` would
  terminate the surrounding AppleScript string *before* `quoted form of` runs,
  and a literal `'` would break out of the *outer* `osascript -e '...'` shell
  quoting. Both are handled instead by `validate_path` in `bot.py`, which
  rejects `"` (and, on macOS only, `'`) in folder names.
- The window stays open after `claude` exits — same rationale as `-NoExit` on
  Windows, so you can see the output.

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
