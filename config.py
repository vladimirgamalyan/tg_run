from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Project root — the folder this file lives in. Config files are resolved
# relative to it, not to the current working directory, so the bot works no
# matter where it is launched from.
_HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    base_dirs: tuple[Path, ...]
    allow_create: bool
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
        key = os.path.normcase(str(base_dir))
        if key in seen:
            continue
        seen.add(key)
        base_dirs.append(base_dir)

    raw_ids = tg.get("allowed_user_ids", [])
    if not isinstance(raw_ids, list):
        raise SystemExit("allowed_user_ids must be a list of numeric Telegram IDs")
    try:
        allowed_list = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        raise SystemExit("allowed_user_ids entries must be numeric Telegram IDs")

    return Config(
        bot_token=token,
        allowed_user_ids=frozenset(allowed_list),
        base_dirs=tuple(base_dirs),
        allow_create=bool(projects.get("allow_create", True)),
        # Keep in sync with the documented default in config.example.toml.
        launch_command=str(launch.get(
            "command",
            'wt.exe -w new -d "{path}" pwsh -NoLogo -NoExit -Command claude --remote-control',
        )),
        log_level=str(logging_cfg.get("level", "INFO")),
    )
