"""Bot auto-restart supervisor.

Launched by a Task Scheduler task (via the base GUI pythonw.exe — no window)
and keeps bot.py alive: restarts it on an abnormal exit, but not forever.
A normal bot exit (code 0) is not restarted by the supervisor.

Why a separate supervisor rather than the scheduler's "Restart on failure":
the built-in restart does not fire on this system even on a non-zero exit code
(verified: a killed process gave LastTaskResult=0xFFFFFFFF and no restart
happened).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE / "bot.py"
LOG_FILE = HERE / "bot.log"

# How many times in a row to restart on FAST crashes before giving up.
MAX_RETRIES = 3
# Pause between restarts, seconds.
RETRY_DELAY = 15
# If the bot ran longer than this (sec) — treat the run as successful and reset
# the attempt counter: isolated failures over time should not exhaust the limit.
HEALTHY_UPTIME = 60

logger = logging.getLogger("tg_run.supervisor")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(fh)


def main() -> int:
    setup_logging()
    logger.info("Supervisor started, watching %s", BOT.name)
    failures = 0
    while True:
        started = time.monotonic()
        try:
            # Same interpreter (base GUI pythonw) → the bot has no window either.
            proc = subprocess.run([sys.executable, str(BOT), "--hidden"], cwd=str(HERE))
        except Exception:
            logger.exception("Failed to start bot.py — supervisor is exiting")
            return 1

        code = proc.returncode
        uptime = time.monotonic() - started

        if code == 0:
            logger.info("bot.py exited normally (code 0) — supervisor is exiting")
            return 0

        if uptime >= HEALTHY_UPTIME:
            logger.warning(
                "bot.py crashed (code %s) after %.0f s of running — restarting, counter reset",
                code, uptime,
            )
            failures = 0
        else:
            failures += 1
            logger.warning(
                "bot.py crashed (code %s) in %.0f s — fast attempt %d/%d",
                code, uptime, failures, MAX_RETRIES,
            )

        if failures > MAX_RETRIES:
            logger.critical(
                "Fast-restart limit (%d) exhausted — supervisor gives up. "
                "Investigate the cause in the log and restart the task manually.",
                MAX_RETRIES,
            )
            return 1

        time.sleep(RETRY_DELAY)


if __name__ == "__main__":
    sys.exit(main())
