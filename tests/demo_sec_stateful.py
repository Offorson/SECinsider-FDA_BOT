"""Stateful SEC demo: proves cross-feed catalyst note, cluster upgrade, and
idempotency -- all of which need a persistent DB shared across runs. Uses one
in-memory backend across three passes. Offline; no network, no Telegram.

    PYTHONPATH=. python3 tests/demo_sec_stateful.py
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from src.core.config import load_config
from src.core.db import MemoryBackend
from src.core.telegram import TelegramClient
from src.core.summarizer import Summarizer
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.feeds.sec_form4 import SecForm4Feed, FeedEntry

FILINGS = {
    "nvax_ceo.xml": ("0001111111-26-000001", "1234567"),
    "nvax_cfo.xml": ("0002222222-26-000002", "1234567"),
    "nvax_dir.xml": ("0003333333-26-000003", "1234567"),
    "nvax_pres.xml": ("0007777777-26-000007", "1234567"),
}


class ListSource:
    def __init__(self, fixture_dir, files):
        self.dir = Path(fixture_dir)
        self.files = files

    def get_feed_entries(self):
        out = []
        for f in self.files:
            acc, cik = FILINGS[f]
            url = "https://www.sec.gov/Archives/edgar/data/%s/x/%s-index.htm" % (cik, acc)
            out.append(FeedEntry(accession=acc, cik=cik, fixture_file=f, index_url=url))
        return out

    def get_filing_xml(self, entry):
        return (self.dir / entry.fixture_file).read_bytes()


def run_pass(label, db, cfg, feed, files):
    stats = RunStats(label)
    telegram = TelegramClient("", cfg["delivery"]["disclaimer"], dry_run=True)
    summarizer = Summarizer(cfg["summarizer"]["backend"])
    dispatcher = AlertDispatcher(db, telegram, cfg, feed="sec", stats=stats)
    source = ListSource("tests/fixtures/sec", files)
    print("\n########## PASS: %s (%s) ##########" % (label, files))
    feed.run(db, dispatcher, summarizer, stats, source)
    stats.log()


def main():
    setup_logging(False)
    cfg = load_config()
    db = MemoryBackend()
    feed = SecForm4Feed(cfg)

    soon = (date(2026, 6, 11) + timedelta(days=40)).isoformat()
    db.upsert_catalyst({
        "ticker": "NVAX", "company": "Novax Demo Therapeutics Inc",
        "drug": "NVX-101", "catalyst_type": "PDUFA", "expected_date": soon,
        "date_precision": "day", "status": "upcoming", "needs_review": False,
        "source_url": "https://example.com/pdufa",
    })

    run_pass("run-1 cluster-forms", db, cfg, feed,
             ["nvax_ceo.xml", "nvax_cfo.xml", "nvax_dir.xml"])
    run_pass("run-2 fourth-insider", db, cfg, feed, ["nvax_pres.xml"])
    run_pass("run-3 no-new-filings", db, cfg, feed, ["nvax_ceo.xml"])

    print("\nclusters in db:", [(c["ticker"], c["member_count"], c["status"])
                                for c in db.clusters])
    print("alerts_sent keys:", list(db.alerts.keys()))


if __name__ == "__main__":
    main()
