"""Shared alert dispatch + run instrumentation.

``AlertDispatcher`` is the single choke-point every alert flows through, so the
idempotency contract lives in exactly one place:

    paid send (claim -> send -> confirm/release)  +  enqueue 24h-delayed free copy

Both steps are idempotent (DB unique constraints on ``alerts_sent.dedup_key`` and
``delayed_queue.dedup_key``), so any job can crash and re-run with no duplicates.

Also exposes ``setup_logging`` and ``RunStats`` for the structured, human-readable
per-run logs the spec requires.
"""
from __future__ import annotations

import logging
import sys
from datetime import timedelta
from typing import Optional

from src.core.db import now_utc
from src.core.telegram import TelegramError

log = logging.getLogger("alerting")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


class RunStats:
    """Lightweight counters; print a one-line summary at job end."""

    def __init__(self, job: str) -> None:
        self.job = job
        self.counts: dict[str, int] = {}

    def inc(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def summary(self) -> str:
        parts = " ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"[{self.job}] {parts or 'no activity'}"

    def log(self) -> None:
        log.info(self.summary())


class AlertDispatcher:
    def __init__(self, db, telegram, cfg, feed: str, stats: Optional[RunStats] = None):
        self.db = db
        self.telegram = telegram
        self.cfg = cfg
        self.feed = feed
        self.stats = stats
        self.delay_hours = cfg["delivery"]["delay_hours"]
        self.stale_minutes = cfg.get_path("idempotency.pending_stale_minutes", 30)

    def dispatch(self, base_key: str, alert_type: str, paid_channel: str,
                 free_channel: str, html_text: str, payload: Optional[dict] = None) -> bool:
        """Send a real-time alert to the paid channel (idempotently) and queue a
        24h-delayed copy for the free channel. Returns True if a NEW paid post was
        made this run, False if it was already sent/queued before."""
        payload = payload or {}
        paid_key = f"{base_key}:paid"

        newly_sent = False
        claimed = self.db.claim_alert(paid_key, self.feed, paid_channel,
                                       alert_type, payload, self.stale_minutes)
        if claimed:
            try:
                self.telegram.send(paid_channel, html_text)
            except TelegramError as exc:
                # Leave it unconfirmed -> retried next run. Never marked sent.
                self.db.release_alert(paid_key)
                log.error("Paid send failed (%s); will retry next run: %s",
                          base_key, exc)
                if self.stats:
                    self.stats.inc("send_failed")
                return False
            self.db.confirm_alert(paid_key)
            newly_sent = True
            if self.stats:
                self.stats.inc("alerted")
        else:
            if self.stats:
                self.stats.inc("skipped_already_sent")

        # Enqueue the delayed free copy (idempotent regardless of paid outcome).
        available_at = now_utc() + timedelta(hours=self.delay_hours)
        if self.db.enqueue_delayed(base_key, self.feed, free_channel,
                                   html_text, available_at):
            if self.stats:
                self.stats.inc("queued_delayed")

        return newly_sent
