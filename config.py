from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Project root — the folder this file lives in. Config files are resolved
# relative to it, not to the current working directory, so the bot works no
# matter where it is launched from.
_HERE = Path(__file__).resolve().parent

# Default launch command per platform, used when `command` is absent from
# config.toml. macOS: `quoted form of` asks AppleScript to shell-quote {path}
# safely (spaces, $, backticks, ...) before Terminal's own shell sees it; the
# outer `osascript -e '...'` wrapping relies on {path} never containing a
# literal `'`, which validate_path in bot.py rejects on macOS for exactly this
# reason. `do script` runs before `activate` so that on a cold start (Terminal
# not yet running) a single window opens — leading with `activate` would launch
# Terminal with its own default window and `do script` would then open a second.
_DEFAULT_LAUNCH_COMMANDS = {
    "win32": 'wt.exe -w new -d "{path}" pwsh -NoLogo -NoExit -Command claude --remote-control',
    "darwin": (
        "osascript "
        "-e 'tell application \"Terminal\" to do script "
        '"cd " & quoted form of "{path}" & " && claude --remote-control"\' '
        "-e 'tell application \"Terminal\" to activate'"
    ),
}


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    base_dirs: tuple[Path, ...]
    allow_create: bool
    favorites: tuple[str, ...]
    launch_command: str
    log_level: str


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = Path(path) if path is not None else _HERE / "config.toml"
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SystemExit(f"Cannot read {cfg_path}: {e}")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"Invalid TOML in {cfg_path}: {e}")

    tg = data.get("telegram", {})
    projects = data.get("projects", {})
    launch = data.get("launch", {})
    logging_cfg = data.get("logging", {})

    token = str(tg.get("bot_token", "")).strip()
    if not token:
        raise SystemExit("bot_token is not set — put it in config.toml")

    raw_dirs = projects.get("base_dirs")
    if raw_dirs is None:
        raise SystemExit("base_dirs is not set — put a list of folders in config.toml")
    if not isinstance(raw_dirs, list):
        raise SystemExit('base_dirs must be a list of paths, e.g. ["C:/projects"]')
    if not raw_dirs:
        raise SystemExit("base_dirs is empty — put at least one folder in config.toml")

    base_dirs: list[Path] = []
    seen: set[str] = set()
    for raw in raw_dirs:
        if not str(raw).strip():
            raise SystemExit("base_dirs entry is empty — put a folder path in config.toml")
        base_dir = Path(str(raw)).expanduser()
        if not base_dir.is_dir():
            raise SystemExit(f"base_dirs entry does not exist or is not a folder: {base_dir}")
        base_dir = base_dir.resolve()
        # On macOS the default launch command embeds this base_dir prefix into a
        # single-quoted `osascript -e '...'` argument and an AppleScript string
        # literal; a ' " or \ in the path would break that quoting and the launch
        # would silently fail. validate_path guards the relative segment typed
        # over Telegram, but not this prefix — so reject it here.
        if sys.platform == "darwin" and any(c in str(base_dir) for c in "'\"\\"):
            raise SystemExit(
                "base_dirs entry contains a character (' \" \\) that breaks the "
                f"macOS launch command: {base_dir}"
            )
        key = os.path.normcase(str(base_dir))
        if key in seen:
            continue
        seen.add(key)
        base_dirs.append(base_dir)

    # Favorite project paths, shown as one-tap buttons by /favorite. Same form
    # as a /claude argument ("name" or "group/name"); validated at press time in
    # bot.py, so here we only drop blanks and keep the configured order.
    raw_favorites = projects.get("favorites", [])
    if not isinstance(raw_favorites, list):
        raise SystemExit('favorites must be a list of project paths, e.g. ["group/proj"]')
    favorites = tuple(f for raw in raw_favorites if (f := str(raw).strip()))

    raw_ids = tg.get("allowed_user_ids", [])
    if not isinstance(raw_ids, list):
        raise SystemExit("allowed_user_ids must be a list of numeric Telegram IDs")
    try:
        allowed_list = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        raise SystemExit("allowed_user_ids entries must be numeric Telegram IDs")

    # Fail fast on a platform with no built-in default (e.g. Linux) rather than
    # replying "Launching..." and letting the shell error out on wt.exe later.
    launch_command = launch.get("command")
    if launch_command is None:
        launch_command = _DEFAULT_LAUNCH_COMMANDS.get(sys.platform)
        if launch_command is None:
            raise SystemExit(
                f"No built-in launch command for platform {sys.platform!r} — "
                "set [launch] command in config.toml"
            )

    return Config(
        bot_token=token,
        allowed_user_ids=frozenset(allowed_list),
        base_dirs=tuple(base_dirs),
        allow_create=bool(projects.get("allow_create", True)),
        favorites=favorites,
        launch_command=str(launch_command),
        log_level=str(logging_cfg.get("level", "INFO")),
    )
