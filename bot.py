from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

# --- autostart without a visible window -------------------------------------
# To avoid any window at all, the scheduler task launches the base Python GUI
# interpreter (pythonw.exe, subsystem=GUI — no console is created), NOT the
# venv trampoline .venv\Scripts\pythonw.exe: that one still opens a console
# window on Windows 11. The base python does not activate the venv itself, so
# we add its site-packages to the path manually (idempotent: under a venv the
# path is already there).
import site

_VENV_SITE = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
if _VENV_SITE.is_dir() and str(_VENV_SITE) not in sys.path:
    site.addsitedir(str(_VENV_SITE))

# Safety net in case the bot is started via the CONSOLE interpreter with the
# --hidden flag (e.g. manually): hide the window. On Win11 with Windows
# Terminal this does not always work, so the primary mechanism is the GUI
# pythonw above, and this is only a fallback.
HIDE_CONSOLE = sys.platform == "win32" and "--hidden" in sys.argv

if HIDE_CONSOLE:
    import ctypes

    _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if _hwnd:
        ctypes.windll.user32.ShowWindow(_hwnd, 0)  # SW_HIDE

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, Message, TelegramObject, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config, load_config

logger = logging.getLogger("tg_run")

# The log is written next to the script, with rotation. Under autostart
# (pythonw.exe with no console window) stdout/stderr go nowhere, so an explicit
# file handler is needed rather than redirecting output.
LOG_FILE = Path(__file__).with_name("bot.log")


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    handlers: list[logging.Handler] = [file_handler]
    # Write to the console only when stderr is an interactive terminal: with
    # --hidden the window is hidden, under pythonw without a console
    # sys.stderr == None, and under launchd on macOS stderr is redirected to
    # launchd.log (a file). The isatty() check keeps the full log stream out of
    # that unrotated file, which would otherwise duplicate every bot.log line.
    if sys.stderr is not None and not HIDE_CONSOLE and sys.stderr.isatty():
        try:
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        handlers.append(stream_handler)

    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)


def _log_unhandled(exc_type, exc_value, exc_tb) -> None:
    """Last line of defense: any unhandled exception is written to the log
    before the process dies — otherwise the cause can't be recovered after a
    restart."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))


# Assigned in the __main__ block before the dispatcher starts. Importing this
# module must stay side-effect free (no logging setup, no config load): an
# accidental import (e.g. from a test) used to install the excepthook and
# write the importer's crashes into the production bot.log.
config: Config

public_router = Router(name="public")
secure_router = Router(name="secure")

# Characters not allowed in Windows folder names, plus control characters.
# `%` is legal in a folder name but rejected anyway: the launch command runs
# through cmd.exe (shell=True), and cmd expands %VAR% even inside quotes.
# `/` and `\` are excluded here: they separate path segments and are handled
# by validate_path; every individual segment must not contain them anyway
# because splitting removes them.
#
# `'` (\x27) is rejected only on macOS: the default launch command there wraps
# {path} in a single-quoted `osascript -e '...'` shell argument, and an embedded
# `'` would break out of that quoting. On Windows the wt.exe command double-
# quotes {path} and cmd.exe handles `'` fine, so rejecting it there would need-
# lessly block existing folders like `O'Brien-app`.
_INVALID = re.compile(
    r'[<>:"|?*%\x00-\x1f\x27]' if sys.platform == "darwin"
    else r'[<>:"|?*%\x00-\x1f]'
)

# Windows reserved device names — invalid as folder names, with or without an
# extension ("con", "con.txt").
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{d}" for d in "123456789"}
    | {f"LPT{d}" for d in "123456789"}
)

# Maximum callback_data length in Telegram — 64 bytes.
_CB_LIMIT = 64


def esc(text: str) -> str:
    """Escape text for parse_mode=HTML."""
    return html.escape(text)


TG_TEXT_LIMIT = 4000  # conservative; Telegram hard limit is 4096 UTF-16 units


def tg_len(text: str) -> int:
    """Length in UTF-16 code units — the units Telegram limits count in
    (a character outside the BMP, e.g. an emoji, counts as 2)."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def clip(text: str, limit: int = TG_TEXT_LIMIT) -> str:
    """Truncate PLAIN text to `limit` UTF-16 units, appending an ellipsis when
    cut. Pass raw text BEFORE esc()/tag-wrapping so an HTML entity or tag is
    never split at the boundary."""
    if tg_len(text) <= limit:
        return text
    units = 0
    for i, c in enumerate(text):
        units += 2 if ord(c) > 0xFFFF else 1
        if units > limit - 1:  # keep one unit for the ellipsis
            return text[:i].rstrip() + "…"
    return text


def validate_path(raw: str) -> str | None:
    """Return a safe relative path ("name" or "group/name", normalized to
    forward slashes) or None."""
    path = raw.strip().strip('"').strip().replace("\\", "/")
    # An empty segment rejects a leading/trailing slash, "a//b", and UNC paths.
    segments = path.split("/") if path else []
    if not segments:
        return None
    for seg in segments:
        if not seg or seg in (".", ".."):
            return None
        # `..` guards against traversal. A trailing "." (or space, already
        # removed by the strip above for the outer segments) is silently
        # trimmed by Windows — "foo." lands on disk as "foo", a mismatch with
        # the name we echo back — so reject it too. `:` in _INVALID also
        # rejects drive-absolute paths like "C:/x".
        if _INVALID.search(seg) or ".." in seg or seg != seg.strip() or seg.endswith("."):
            return None
        if seg.split(".", 1)[0].upper() in _RESERVED:
            return None
    return "/".join(segments)


def resolve_under(root: Path, relpath: str) -> Path | None:
    """Resolve `relpath` inside `root`; None if it escapes the root (a safety
    net on top of the segment checks, e.g. against symlinks/junctions)."""
    target = (root / relpath).resolve()
    if target == root or not target.is_relative_to(root):
        return None
    return target


# Claude Code settings file, where the list of trusted folders is stored.
CLAUDE_CONFIG = Path.home() / ".claude.json"


def ensure_trusted(path: Path) -> None:
    """Mark a folder as trusted in ~/.claude.json so that Claude Code does not
    show the interactive "Do you trust the files in this folder?" dialog on
    startup.

    Claude stores path keys with forward slashes (K:/projects/foo), so we use
    as_posix() rather than str() (which yields backslashes on Windows).
    """
    key = path.as_posix()
    try:
        data = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s — folder not pre-approved", CLAUDE_CONFIG, e)
        return

    projects = data.setdefault("projects", {})
    entry = projects.get(key)
    if isinstance(entry, dict):
        if entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted — leave the file untouched
        entry["hasTrustDialogAccepted"] = True
    else:
        projects[key] = {
            "allowedTools": [],
            "disabledMcpjsonServers": [],
            "enabledMcpjsonServers": [],
            "hasClaudeMdExternalIncludesApproved": False,
            "hasClaudeMdExternalIncludesWarningShown": False,
            "hasTrustDialogAccepted": True,
            "mcpContextUris": [],
            "mcpServers": {},
            "projectOnboardingSeenCount": 0,
        }

    try:
        tmp = CLAUDE_CONFIG.with_name(CLAUDE_CONFIG.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CLAUDE_CONFIG)  # atomic replace
        logger.info("Folder marked as trusted: %s", key)
    except OSError as e:
        logger.warning("Failed to write %s: %s", CLAUDE_CONFIG, e)


# Background launch tasks are kept referenced here so the event loop doesn't
# garbage-collect them mid-flight (asyncio only holds a weak reference to a task).
_launch_tasks: set[asyncio.Task[None]] = set()


async def _run_launch(cmd: str) -> None:
    """Run the launch command and log a non-zero exit with its stderr. The
    launcher (wt.exe / osascript) spawns the terminal and returns promptly, so
    this does not block on the Claude session. On macOS the common first-run
    failure — Automation permission not granted, where osascript exits non-zero
    with 'Not authorized to send Apple events to Terminal. (-1743)' — would
    otherwise leave no trace at all while the bot reports success."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = (stderr or b"").decode(errors="replace").strip()
        logger.error("Launch command failed (exit %s): %s", proc.returncode, detail)


def launch_claude(path: Path) -> None:
    ensure_trusted(path)
    cmd = config.launch_command.format(path=str(path))
    logger.info("Launching terminal: %s", cmd)
    task = asyncio.create_task(_run_launch(cmd))
    _launch_tasks.add(task)
    task.add_done_callback(_launch_tasks.discard)


HELP_TEXT = (
    "🤖 <b>Claude Code launcher</b>\n\n"
    "<b>/claude &lt;folder&gt;</b> — open a terminal with Claude Code in a project folder "
    "(offers to create it if missing). Nested folders work too: "
    "<code>/claude group/proj</code>\n"
    "<b>/list [folder]</b> — list projects (or the contents of a subfolder)\n"
)


# --- access control ---------------------------------------------------------

class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = getattr(event, "from_user", None) or data.get("event_from_user")
        if user is not None and user.id in self.allowed:
            return await handler(event, data)
        logger.warning("Access denied: user_id=%s", getattr(user, "id", None))
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ No access", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("⛔ No access. Add your Telegram ID to config.toml.")
        return None


# --- public commands (no access check) --------------------------------------

@public_router.message(Command("start", "help", ignore_case=True))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


# --- protected commands -----------------------------------------------------

@secure_router.message(Command("claude", ignore_case=True))
async def cmd_claude(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Usage: <code>/claude folder_name</code>")
        return
    safe = validate_path(raw)
    if safe is None:
        await message.answer("⛔ Invalid folder name.")
        return

    matches = [
        (i, target)
        for i, root in enumerate(config.base_dirs)
        if (target := resolve_under(root, safe)) is not None and target.is_dir()
    ]

    if len(matches) == 1:
        launch_claude(matches[0][1])
        await message.answer(f"▶️ Launching Claude Code in <b>{esc(safe)}</b>")
        return

    if matches:
        # The same relative path exists in several roots — ask which one.
        builder = InlineKeyboardBuilder()
        dropped = 0
        for i, _target in matches:
            cb = f"run:{i}:{safe}"
            if len(cb.encode()) <= _CB_LIMIT:
                builder.button(text=f"▶️ {config.base_dirs[i].as_posix()}", callback_data=cb)
            else:
                dropped += 1
        builder.adjust(1)
        if dropped == len(matches):
            await message.answer("⛔ Name is too long for the selection buttons.")
            return
        text = f"📁 <b>{esc(safe)}</b> exists in several places. Where to launch?"
        if dropped:
            text += f"\n⚠️ {dropped} of {len(matches)} locations don't fit the button limit and are not shown."
        await message.answer(text, reply_markup=builder.as_markup())
        return

    # Folder not found — offer to create it, but only in roots where the
    # parent folder already exists (no mkdir -p: a typo in the group name must
    # not silently create a whole new tree).
    lines = [f"📁 Folder <b>{esc(safe)}</b> not found."]
    builder = InlineKeyboardBuilder()
    has_buttons = False
    if config.allow_create:
        creatable = [
            i
            for i, root in enumerate(config.base_dirs)
            if (target := resolve_under(root, safe)) is not None and target.parent.is_dir()
        ]
        dropped = 0
        for i in creatable:
            cb = f"new:{i}:{safe}"
            if len(cb.encode()) > _CB_LIMIT:
                dropped += 1
                continue
            builder.button(
                text=f"➕ Create in {config.base_dirs[i].as_posix()}", callback_data=cb
            )
            has_buttons = True
        if has_buttons:
            lines.append("\nCreate a new folder and launch?")
            if dropped:
                lines.append(
                    f"⚠️ {dropped} of {len(creatable)} locations don't fit the button limit "
                    "and are not shown."
                )
        elif dropped:
            lines.append("\nName is too long to create via the button.")
        elif "/" in safe:
            lines.append("\nThe parent folder does not exist either — create it first.")
    builder.adjust(1)

    await message.answer(
        "\n".join(lines),
        reply_markup=builder.as_markup() if has_buttons else None,
    )


@secure_router.message(Command("list", ignore_case=True))
async def cmd_list(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    sub: str | None = None
    if raw:
        sub = validate_path(raw)
        if sub is None:
            await message.answer("⛔ Invalid folder name.")
            return

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # One group per root (or per root containing the requested subfolder):
    # a header with the folder path plus its subfolders, most recent on top.
    groups: list[tuple[str, list[Path] | OSError]] = []
    for root in config.base_dirs:
        folder = root if sub is None else resolve_under(root, sub)
        if folder is None or not folder.is_dir():
            continue
        header = f"📂 {esc(folder.as_posix())}"
        try:
            dirs = [p for p in folder.iterdir() if p.is_dir()]
        except OSError as e:
            groups.append((header, e))
            continue
        dirs.sort(key=mtime, reverse=True)
        groups.append((header, dirs))

    if sub is not None and not groups:
        await message.answer(f"📁 Folder <b>{esc(sub)}</b> not found.")
        return
    if all(isinstance(dirs, list) and not dirs for _, dirs in groups):
        await message.answer("Folder is empty." if sub else "No projects yet.")
        return

    # Build line by line so truncation never falls inside an escaped HTML entity
    # (a name with & < > " ' would otherwise break Telegram's HTML parsing).
    footer_reserve = 64  # room for the "… and N more" marker
    limit = TG_TEXT_LIMIT - footer_reserve
    lines: list[str] = []
    length = 0
    shown = 0
    hidden = 0
    out_of_room = False

    def try_add(line: str) -> bool:
        nonlocal length
        if out_of_room or length + tg_len(line) + 1 > limit:
            return False
        lines.append(line)
        length += tg_len(line) + 1
        return True

    for gi, (header, dirs) in enumerate(groups):
        if gi > 0 and not try_add(""):
            out_of_room = True
        elif not try_add(header):
            out_of_room = True
        elif isinstance(dirs, OSError):
            try_add(f"• ⛔ error: {esc(str(dirs))}")
        if isinstance(dirs, OSError):
            continue
        for p in dirs:
            if try_add(f"• {esc(p.name)}"):
                shown += 1
            else:
                out_of_room = True
                hidden += 1
    if hidden:
        lines.append(f"… and {hidden} more ({shown} shown)")
    await message.answer(clip("\n".join(lines)))


# Catches everything that didn't match the commands above (registered last, so
# it fires only for unknown commands and arbitrary text). The access check is
# performed by the same middleware as for the other secure_router commands.
@secure_router.message()
async def cmd_fallback(message: Message) -> None:
    text = message.text or ""
    if text.startswith("/"):
        prefix = "❓ Unknown command.\n\n"
    else:
        prefix = "🤔 I only understand commands.\n\n"
    await message.answer(prefix + HELP_TEXT)


# --- button handlers --------------------------------------------------------

def parse_root_callback(data: str) -> tuple[Path, str] | None:
    """Parse "<prefix>:<root index>:<relative path>" callback data; None if the
    index or path is invalid (e.g. a stale button from an older bot version)."""
    parts = data.split(":", 2)
    # isascii() too: isdigit() alone accepts Unicode digits like "²" that
    # int() then rejects with a ValueError.
    if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()):
        return None
    idx = int(parts[1])
    if idx >= len(config.base_dirs):
        return None
    safe = validate_path(parts[2])
    if safe is None:
        return None
    return config.base_dirs[idx], safe


@secure_router.callback_query(F.data.startswith("run:"))
async def cb_run(callback: CallbackQuery) -> None:
    parsed = parse_root_callback(callback.data or "")
    if parsed is None:
        await callback.answer("Invalid button", show_alert=True)
        return
    root, safe = parsed
    target = resolve_under(root, safe)
    if target is None or not target.is_dir():
        await callback.answer("Folder no longer exists", show_alert=True)
        return
    await callback.answer("Launching…")
    launch_claude(target)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"▶️ Launching Claude Code in <b>{esc(safe)}</b> ({esc(root.as_posix())})"
        )


@secure_router.callback_query(F.data.startswith("new:"))
async def cb_new(callback: CallbackQuery) -> None:
    parsed = parse_root_callback(callback.data or "")
    if parsed is None:
        await callback.answer("Invalid button", show_alert=True)
        return
    await callback.answer("Creating…")
    if isinstance(callback.message, Message):
        await create_and_run(callback.message, *parsed)


# --- shared create-and-launch logic -----------------------------------------

async def create_and_run(message: Message, root: Path, safe: str) -> None:
    if not config.allow_create:
        await message.answer("⛔ Folder creation is disabled in the config.")
        return
    # Re-resolve at execution time, not only when the button was offered: the
    # button stays pressable forever, and a symlink/junction created since
    # could make the path escape the root.
    target = resolve_under(root, safe)
    if target is None:
        await message.answer("⛔ Path escapes the project roots.")
        return
    if target.exists():
        if target.is_dir():
            await message.answer(
                f"📁 Folder <b>{esc(safe)}</b> already exists — no need to create a new one.\n"
                f"Launch Claude Code in it: <code>/claude {esc(safe)}</code>"
            )
        else:
            await message.answer(f"⛔ <b>{esc(safe)}</b> already exists and is not a folder.")
        return
    try:
        target.mkdir(parents=False, exist_ok=False)
    except OSError as e:
        await message.answer(f"⛔ Failed to create folder: {esc(str(e))}")
        return
    launch_claude(target)
    await message.answer(f"✅ Created <b>{esc(safe)}</b> and launching Claude Code")


# --- error handling ----------------------------------------------------------

async def on_error(event: ErrorEvent) -> None:
    """Catches exceptions from handlers and writes the traceback to the log.
    Returning here does not kill polling — the bot keeps running."""
    logger.error("Error while handling update", exc_info=event.exception)


# --- startup ----------------------------------------------------------------

async def main() -> None:
    if not config.allowed_user_ids:
        logger.warning(
            "allowed_user_ids is empty: no user can access the bot. "
            "Put your Telegram ID into config.toml and restart."
        )

    bot = Bot(
        config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.errors.register(on_error)

    access = AccessMiddleware(config.allowed_user_ids)
    secure_router.message.middleware(access)
    secure_router.callback_query.middleware(access)

    dp.include_router(public_router)
    dp.include_router(secure_router)

    # Under autostart the bot comes up at logon, when the network is often not
    # ready yet — do not die on the first API call. bot.me() doubles as the
    # probe: its result is cached, so start_polling's own me() call later needs
    # no network round-trip.
    while True:
        try:
            await bot.me()
            break
        except TelegramNetworkError as e:
            logger.warning("Telegram is unreachable (%s); retrying in 5 s", e)
            await asyncio.sleep(5)

    # Telegram command menu. /claude takes a folder argument; sent from the menu
    # without one it replies with a usage hint, which is still useful.
    try:
        await bot.set_my_commands([
            BotCommand(command="claude", description="Launch Claude Code in a project folder"),
            BotCommand(command="list", description="List projects"),
            BotCommand(command="help", description="Help and command list"),
        ])
    except TelegramNetworkError as e:
        # The menu is cosmetic — a network blip here must not kill the bot.
        logger.warning("Failed to set the command menu: %s", e)

    logger.info("Bot started. base_dirs=%s", [p.as_posix() for p in config.base_dirs])
    await dp.start_polling(bot)


if __name__ == "__main__":
    setup_logging()
    sys.excepthook = _log_unhandled

    try:
        config = load_config()
    except SystemExit:
        # Invalid/missing token, no base_dirs, etc. Log the cause, otherwise it
        # is lost under autostart.
        logger.critical("Failed to load configuration", exc_info=True)
        raise

    logging.getLogger().setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logger.critical("Fatal error — process is exiting", exc_info=True)
        # A non-zero exit code signals the scheduler to restart the task.
        sys.exit(1)
