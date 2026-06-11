"""Stateful FDA demo: reminders (T-7, T-1), overdue/passed-unresolved, and a
ClinicalTrials.gov status change -- all need persistent DB state. Offline.

    PYTHONPATH=. python3 tests/demo_fda_stateful.py
"""
from __future__ import annotations

from datetime import timedelta

from src.core.config import load_config
from src.core.db import MemoryBackend, now_utc
from src.core.telegram import TelegramClient
from src.core.summarizer import Summarizer
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.feeds.fda_catalysts import FdaCatalystFeed
from src.jobs.poll_fda import FixtureCtSource


def make_ctx(db, cfg, label):
    stats = RunStats(label)
    tg = TelegramClient("", cfg["delivery"]["disclaimer"], dry_run=True)
    sm = Summarizer(cfg["summarizer"]["backend"])
    disp = AlertDispatcher(db, tg, cfg, feed="fda", stats=stats)
    return stats, sm, disp


def main():
    setup_logging(False)
    cfg = load_config()
    db = MemoryBackend()
    feed = FdaCatalystFeed(cfg)
    today = now_utc().date()

    # Catalysts with dates relative to today.
    db.upsert_catalyst({"ticker": "ABCD", "company": "Abcd Demo Bio Corp",
                        "drug": "Drugzumab", "catalyst_type": "PDUFA",
                        "expected_date": (today + timedelta(days=7)).isoformat(),
                        "date_precision": "day", "status": "upcoming",
                        "needs_review": False, "source_url": "https://example.com/abcd"})
    db.upsert_catalyst({"ticker": "NVAX", "company": "Novax Demo Therapeutics Inc",
                        "drug": "NVX-101", "catalyst_type": "PDUFA",
                        "expected_date": (today + timedelta(days=1)).isoformat(),
                        "date_precision": "day", "status": "upcoming",
                        "needs_review": False, "source_url": "https://example.com/nvax"})
    db.upsert_catalyst({"ticker": "QRST", "company": "Qrst Biosciences",
                        "drug": "Trialide", "catalyst_type": "PDUFA",
                        "expected_date": (today - timedelta(days=6)).isoformat(),
                        "date_precision": "day", "status": "upcoming",
                        "needs_review": False, "source_url": "https://example.com/qrst"})

    print("\n########## REMINDERS (daily job) ##########")
    stats, sm, disp = make_ctx(db, cfg, "reminders")
    feed.run_reminders(db, disp, sm, stats)
    stats.log()

    print("\n########## OVERDUE / passed-unresolved ##########")
    stats, sm, disp = make_ctx(db, cfg, "overdue")
    feed.run_overdue(db, disp, sm, stats)
    stats.log()

    print("\n########## CLINICALTRIALS.GOV change ##########")
    # Baseline: trial was Recruiting last time we checked.
    db.upsert_ct_trial({"nct_id": "NCT09990001", "ticker": "ABCD",
                        "sponsor": "Abcd Demo Bio Corp", "overall_status": "Recruiting",
                        "primary_completion_date": "2026-09-30",
                        "last_checked": now_utc().isoformat()})
    stats, sm, disp = make_ctx(db, cfg, "ct")
    feed.run_clinicaltrials(db, disp, sm, stats, FixtureCtSource("tests/fixtures/fda"))
    stats.log()

    print("\n########## REMINDERS re-run (idempotent) ##########")
    stats, sm, disp = make_ctx(db, cfg, "reminders-2")
    feed.run_reminders(db, disp, sm, stats)
    stats.log()


if __name__ == "__main__":
    main()
