"""Worker process: `python -m workers [--kinds a,b] [--once]`.

Redis only wakes workers early; PostgreSQL is the source of truth. If Redis is unavailable the loop
falls back to polling, so no work is lost.
"""

import argparse
import logging
import os
import socket
import time

import redis

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.maintenance import expire_uploads
from workers import automation, dispatcher
from workers.registry import HANDLERS
from workers.runtime import run_one

WAKE_KEY = "prodauto:wake"
POLL_SECONDS = 5
MAINTENANCE_SECONDS = 600
TICK_SECONDS = 60  # schedules, exception scan and reminders (M7)
log = logging.getLogger("workers")


def notify(client: redis.Redis | None) -> None:
    if client is None:
        return
    try:
        client.lpush(WAKE_KEY, "1")
        client.ltrim(WAKE_KEY, 0, 99)
    except redis.RedisError:
        log.warning("redis wake-up unavailable; workers will poll")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kinds", default="all", help="comma-separated job kinds, or 'all'")
    parser.add_argument("--once", action="store_true", help="drain due work once and exit")
    args = parser.parse_args()

    configure_logging()
    kinds = sorted(HANDLERS) if args.kinds == "all" else args.kinds.split(",")
    unknown = set(kinds) - set(HANDLERS)
    if unknown:
        raise SystemExit(f"unknown job kinds: {sorted(unknown)}")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        client: redis.Redis | None = redis.Redis.from_url(get_settings().redis_url, socket_timeout=POLL_SECONDS + 5)
        client.ping()
    except redis.RedisError:
        log.warning("redis unavailable at start; polling every %ss", POLL_SECONDS)
        client = None

    log.info("worker %s handling %s", worker_id, kinds)
    next_maintenance = next_tick = 0.0
    while True:
        if time.monotonic() >= next_tick:
            try:
                automation.enqueue_ticks()
            except Exception:  # noqa: BLE001 - a failed tick must not stop job processing
                log.exception("automation tick failed")
            next_tick = time.monotonic() + TICK_SECONDS
        if time.monotonic() >= next_maintenance:
            try:
                if expired := expire_uploads():
                    log.info("expired %s abandoned uploads", expired)
            except Exception:  # noqa: BLE001 - housekeeping must not stop job processing
                log.exception("maintenance failed")
            next_maintenance = time.monotonic() + MAINTENANCE_SECONDS
        if dispatcher.dispatch_batch():
            notify(client)
        while run_one(kinds, worker_id):
            pass
        if args.once:
            return
        try:
            if client is not None:
                client.brpop([WAKE_KEY], timeout=POLL_SECONDS)
            else:
                time.sleep(POLL_SECONDS)
        except redis.RedisError:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
