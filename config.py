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
    alert_chat_id: int
    base_dir: Path
    allow_create: bool
    launch_command: str
    log_level: str


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = Path(path) if path is not None else _HERE / "config.toml"
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    tg = data.get("telegram", {})
    projects = data.get("projects", {})
    launch = data.get("launch", {})
    logging_cfg = data.get("logging", {})

    token = str(tg.get("bot_token", "")).strip()
    if not token:
        raise SystemExit("bot_token is not set — put it in config.toml")

    base_dir = Path(str(projects.get("base_dir", ""))).expanduser()
    if not base_dir.is_dir():
        raise SystemExit(f"base_dir does not exist or is not a folder: {base_dir}")

    allowed_list = [int(x) for x in tg.get("allowed_user_ids", [])]
    # Where to send error alerts: explicit alert_chat_id or the first whitelist entry.
    alert_chat_id = int(tg.get("alert_chat_id") or (allowed_list[0] if allowed_list else 0))

    return Config(
        bot_token=token,
        allowed_user_ids=frozenset(allowed_list),
        alert_chat_id=alert_chat_id,
        base_dir=base_dir.resolve(),
        allow_create=bool(projects.get("allow_create", True)),
        launch_command=str(launch.get("command", 'wt.exe -d "{path}" claude.exe')),
        log_level=str(logging_cfg.get("level", "INFO")),
    )
