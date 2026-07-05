from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
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
    # Write to the console only when it is visible: with --hidden the window is
    # hidden, and under pythonw without a console sys.stderr == None. In both
    # cases — file only.
    if sys.stderr is not None and not HIDE_CONSOLE:
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


setup_logging()
sys.excepthook = _log_unhandled

try:
    config: Config = load_config()
except SystemExit:
    # Invalid/missing token, no base_dir, etc. Log the cause, otherwise it is
    # lost under autostart. The scheduler will restart the process.
    logger.critical("Failed to load configuration", exc_info=True)
    raise

logging.getLogger().setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

public_router = Router(name="public")
secure_router = Router(name="secure")

# Characters not allowed in Windows folder names, plus control characters.
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Maximum callback_data length in Telegram — 64 bytes.
_CB_LIMIT = 64


def esc(text: str) -> str:
    """Escape text for parse_mode=HTML."""
    return html.escape(text)


def validate_name(raw: str) -> str | None:
    """Return a safe folder name (exactly one level inside base_dir) or None."""
    name = raw.strip().strip('"').strip()
    if not name or name in (".", ".."):
        return None
    if _INVALID.search(name) or ".." in name:
        return None
    base = config.base_dir
    target = (base / name).resolve()
    # Must be a direct child of base_dir: no nesting and no escaping outside.
    if target.parent != base:
        return None
    return name


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


def launch_claude(path: Path) -> None:
    ensure_trusted(path)
    cmd = config.launch_command.format(path=str(path))
    logger.info("Launching terminal: %s", cmd)
    subprocess.Popen(cmd, shell=True)


HELP_TEXT = (
    "🤖 <b>Claude Code launcher</b>\n\n"
    "<b>/claude &lt;folder&gt;</b> — open a terminal with Claude Code in a project folder\n"
    "<b>/claude_new &lt;folder&gt;</b> — create a new folder and launch\n"
    "<b>/list</b> — list projects\n"
    "<b>/id</b> — show your Telegram ID\n"
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
            await event.answer("⛔ No access. Send /id and add your ID to config.toml.")
        return None


# --- public commands (no access check) --------------------------------------

@public_router.message(Command("start", "help", ignore_case=True))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@public_router.message(Command("id", ignore_case=True))
async def cmd_id(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    allowed = user.id in config.allowed_user_ids
    logger.info("WHOAMI user_id=%s username=%s allowed=%s", user.id, user.username, allowed)
    status = "✅ access granted" if allowed else "⛔ no access — add your ID to config.toml"
    await message.answer(f"Your Telegram ID: <code>{user.id}</code>\n{status}")


# --- protected commands -----------------------------------------------------

@secure_router.message(Command("claude", ignore_case=True))
async def cmd_claude(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Usage: <code>/claude folder_name</code>")
        return
    safe = validate_name(raw)
    if safe is None:
        await message.answer("⛔ Invalid folder name.")
        return

    target = config.base_dir / safe
    if target.is_dir():
        launch_claude(target)
        await message.answer(f"▶️ Launching Claude Code in <b>{esc(safe)}</b>")
        return

    # Folder not found — offer to create it.
    lines = [f"📁 Folder <b>{esc(safe)}</b> not found."]
    builder = InlineKeyboardBuilder()
    has_buttons = False
    if config.allow_create:
        cb = f"new:{safe}"
        if len(cb.encode()) <= _CB_LIMIT:
            builder.button(text=f"➕ Create \"{safe}\" and launch", callback_data=cb)
            has_buttons = True
            lines.append("\nCreate a new folder and launch?")
        else:
            lines.append(f"\nTo create: <code>/claude_new {esc(safe)}</code>")
    builder.adjust(1)

    await message.answer(
        "\n".join(lines),
        reply_markup=builder.as_markup() if has_buttons else None,
    )


@secure_router.message(Command("claude_new", ignore_case=True))
async def cmd_claude_new(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Usage: <code>/claude_new folder_name</code>")
        return
    safe = validate_name(raw)
    if safe is None:
        await message.answer("⛔ Invalid folder name.")
        return
    await create_and_run(message, safe)


@secure_router.message(Command("list", ignore_case=True))
async def cmd_list(message: Message) -> None:
    try:
        dirs = [p for p in config.base_dir.iterdir() if p.is_dir()]
    except OSError as e:
        await message.answer(f"⛔ Error reading folder: {esc(str(e))}")
        return
    if not dirs:
        await message.answer("No projects yet.")
        return

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # Most recent (by folder mtime) on top.
    dirs.sort(key=mtime, reverse=True)
    text = "📂 Projects (most recent on top):\n" + "\n".join(f"• {esc(p.name)}" for p in dirs)
    await message.answer(text[:4000])


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

@secure_router.callback_query(F.data.startswith("new:"))
async def cb_new(callback: CallbackQuery) -> None:
    safe = validate_name((callback.data or "").split(":", 1)[1])
    if safe is None:
        await callback.answer("Invalid name", show_alert=True)
        return
    await callback.answer("Creating…")
    if isinstance(callback.message, Message):
        await create_and_run(callback.message, safe)


# --- shared create-and-launch logic -----------------------------------------

async def create_and_run(message: Message, safe: str) -> None:
    if not config.allow_create:
        await message.answer("⛔ Folder creation is disabled in the config.")
        return
    target = config.base_dir / safe
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


# --- error notifications ----------------------------------------------------

# Throttling so that a storm of errors does not turn into a storm of Telegram
# messages.
_last_alert_ts = 0.0
_ALERT_MIN_INTERVAL = 30.0


async def notify_admin(bot: Bot, text: str) -> None:
    """Send the admin a short error notification (with anti-flood)."""
    chat_id = config.alert_chat_id
    if not chat_id:
        return
    global _last_alert_ts
    now = time.monotonic()
    if now - _last_alert_ts < _ALERT_MIN_INTERVAL:
        return
    _last_alert_ts = now
    try:
        await bot.send_message(chat_id, text[:4000])
    except Exception:
        logger.exception("Failed to send admin notification")


async def on_error(event: ErrorEvent, bot: Bot) -> None:
    """Catches exceptions from handlers: writes the traceback to the log and
    sends an alert to the admin. Returning here does not kill polling — the bot
    keeps running."""
    logger.error("Error while handling update", exc_info=event.exception)
    await notify_admin(bot, f"⚠️ Bot error:\n<code>{esc(repr(event.exception))}</code>")


async def send_crash_alert(text: str) -> None:
    """One-off emergency message on a fatal process crash — sent with a separate
    client, since the main one may have failed to start (or is already closed)."""
    chat_id = config.alert_chat_id
    if not chat_id:
        return
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.send_message(chat_id, text[:4000])
    finally:
        await bot.session.close()


# --- startup ----------------------------------------------------------------

async def main() -> None:
    if not config.allowed_user_ids:
        logger.warning(
            "allowed_user_ids is empty: only the /id command is available. "
            "Send /id to the bot, put your ID into config.toml and restart."
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

    # In the Telegram command menu we keep only /help: commands that take an
    # argument (/claude, /claude_new) would be sent from the menu without a
    # folder and be useless.
    await bot.set_my_commands([BotCommand(command="help", description="Help and command list")])

    logger.info("Bot started. base_dir=%s", config.base_dir)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logger.critical("Fatal error — process is exiting", exc_info=True)
        try:
            asyncio.run(send_crash_alert(
                "💥 The bot crashed with a fatal error and will be restarted by the scheduler. "
                "Details in bot.log."
            ))
        except Exception:
            logger.exception("Failed to send crash alert")
        # A non-zero exit code signals the scheduler to restart the task.
        sys.exit(1)
