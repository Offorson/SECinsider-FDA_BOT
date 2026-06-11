"""Entrypoint: hourly job — release 24h-delayed copies to the FREE channels.

Every real-time alert was queued in ``delayed_queue`` when it went to the paid
channel. This job posts items whose ``available_at`` has passed to the matching
free channel, then marks them released. Idempotent and crash-safe:

* a per-item free-channel claim in ``alerts_sent`` (key = base + ":free") means a
  crash after posting but before marking-released never double-posts;
* a Telegram failure releases the claim and leaves the row unreleased, so it
  simply retries next hour — never lost.

    python -m src.jobs.release_delayed --dry-run
    python -m src.jobs.release_delayed

Exit 0 normally; 1 only on Supabase outage.
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.core.config import load_config
from src.core.db import get_db
from src.core.telegram import TelegramClient, TelegramError
from src.core.alerting import RunStats, setup_logging

log = logging.getLogger("release_delayed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Release 24h-delayed alerts to free channels")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    stats = RunStats("release_delayed")

    if not args.dry_run:
        cfg.secrets.require("supabase_url", "supabase_key", "telegram_bot_token")

    db = get_db(cfg, dry_run=args.dry_run)
    try:
        db.healthcheck()
    except Exception as exc:  # noqa: BLE001
        log.error("Supabase unreachable (%s) — exiting 1.", exc)
        return 1

    telegram = TelegramClient(
        bot_token=cfg.secrets.telegram_bot_token,
        disclaimer=cfg["delivery"]["disclaimer"],
        max_retries=cfg["delivery"]["telegram_max_retries"],
        backoff_base_sec=cfg["delivery"]["telegram_backoff_base_sec"],
        dry_run=args.dry_run,
    )
    stale_minutes = cfg.get_path("idempotency.pending_stale_minutes", 30)

    due = db.get_due_delayed()
    stats.inc("due", len(due))
    for row in due:
        free_key = f"{row['dedup_key']}:free"
        claimed = db.claim_alert(free_key, row.get("feed", ""),
                                 row.get("free_channel", ""), "delayed_release",
                                 {"id": row.get("id")}, stale_minutes)
        if not claimed:
            # already released by a prior run -> just mark the row done.
            db.mark_delayed_released(row["id"])
            stats.inc("skipped_already_released")
            continue
        try:
            telegram.send(row["free_channel"], row["html"])
        except TelegramError as exc:
            db.release_alert(free_key)  # retry next hour; row stays unreleased
            log.error("Free release failed (%s); will retry: %s", row["dedup_key"], exc)
            stats.inc("send_failed")
            continue
        db.confirm_alert(free_key)
        db.mark_delayed_released(row["id"])
        stats.inc("released")

    stats.log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
