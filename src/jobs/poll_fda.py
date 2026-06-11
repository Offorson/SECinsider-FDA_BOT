"""Entrypoint: poll FDA press feeds + ClinicalTrials.gov for catalyst news.

Run by GitHub Actions every ~15 minutes. Reminders and overdue checks run in the
daily job (``daily_digest``), not here. Local usage:

    python -m src.jobs.poll_fda --dry-run --fixtures tests/fixtures/fda
    python -m src.jobs.poll_fda --dry-run        # live feeds, print only
    python -m src.jobs.poll_fda                   # live: posts to Telegram

Exit codes: 0 normal or upstream feed outage; 1 only if Supabase is unreachable.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys

import feedparser

from src.core.config import load_config
from src.core.db import get_db
from src.core.telegram import TelegramClient
from src.core.summarizer import Summarizer
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.feeds.fda_catalysts import (FdaCatalystFeed, LivePressSource,
                                     LiveCtSource, PressItem)

log = logging.getLogger("poll_fda")


class FixturePressSource:
    """Reads local RSS/Atom files (press*.xml) for offline runs."""

    def __init__(self, fixture_dir: str) -> None:
        self.dir = fixture_dir

    def get_items(self):
        items = []
        for path in sorted(glob.glob(os.path.join(self.dir, "press*.xml"))):
            parsed = feedparser.parse(open(path, "rb").read())
            feed_name = os.path.basename(path)
            for e in parsed.entries:
                guid = e.get("id") or e.get("link") or e.get("title", "")
                items.append(PressItem(
                    guid=guid, title=e.get("title", ""),
                    summary=re.sub("<[^>]+>", " ", e.get("summary", "")),
                    link=e.get("link", ""), feed=feed_name))
        return items


class FixtureCtSource:
    """Reads ct.json: {company_name: [ {nct_id, overall_status, ...}, ... ]}."""

    def __init__(self, fixture_dir: str) -> None:
        path = os.path.join(fixture_dir, "ct.json")
        self.data = json.loads(open(path).read()) if os.path.exists(path) else {}

    def get_studies(self, company: str):
        return self.data.get(company, [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FDA catalyst poller")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--fixtures", metavar="DIR",
                    help="use local fixture feeds instead of live (offline)")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    stats = RunStats("poll_fda")

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
    summarizer = Summarizer(
        backend=cfg["summarizer"]["backend"],
        gemini_api_key=cfg.secrets.gemini_api_key,
        gemini_model=cfg["summarizer"].get("gemini_model", "gemini-1.5-flash"),
    )
    dispatcher = AlertDispatcher(db, telegram, cfg, feed="fda", stats=stats)
    feed = FdaCatalystFeed(cfg)

    if args.fixtures:
        press_source = FixturePressSource(args.fixtures)
        ct_source = FixtureCtSource(args.fixtures)
    else:
        ua = cfg.secrets.sec_user_agent
        press_source = LivePressSource(cfg, user_agent=ua)
        ct_source = LiveCtSource(cfg, user_agent=ua)

    feed.run_press(db, dispatcher, summarizer, stats, press_source, limit=args.limit)
    feed.run_clinicaltrials(db, dispatcher, summarizer, stats, ct_source)

    stats.log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
