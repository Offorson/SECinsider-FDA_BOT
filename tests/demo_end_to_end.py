"""Full end-to-end dry-run across ONE shared DB: seed -> poll_sec -> poll_fda ->
daily_digest -> release_delayed. Offline; prints every alert that would post.

    PYTHONPATH=. python3 tests/demo_end_to_end.py
"""
from __future__ import annotations

from datetime import timedelta

from src.core.config import load_config
from src.core.db import MemoryBackend, now_utc
from src.core.telegram import TelegramClient, TelegramError
from src.core.summarizer import Summarizer
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.feeds.sec_form4 import SecForm4Feed, FixtureSource
from src.feeds.fda_catalysts import FdaCatalystFeed
from src.jobs.poll_fda import FixturePressSource, FixtureCtSource
from src.jobs import daily_digest as dd


def ctx(db, cfg, feedname, label):
    stats = RunStats(label)
    tg = TelegramClient("paid-token", cfg["delivery"]["disclaimer"], dry_run=True)
    sm = Summarizer(cfg["summarizer"]["backend"])
    disp = AlertDispatcher(db, tg, cfg, feed=feedname, stats=stats)
    return stats, sm, disp, tg


def banner(t):
    print("\n" + "#" * 70 + "\n# " + t + "\n" + "#" * 70)


def main():
    setup_logging(False)
    cfg = load_config()
    db = MemoryBackend()
    today = now_utc().date()

    banner("STAGE 0 — seed catalysts (cross-feed + reminder + overdue setup)")
    db.upsert_catalyst({"ticker": "NVAX", "company": "Novax Demo Therapeutics Inc",
                        "drug": "NVX-101", "catalyst_type": "PDUFA",
                        "expected_date": (today + timedelta(days=40)).isoformat(),
                        "date_precision": "day", "status": "upcoming",
                        "needs_review": False, "source_url": "https://example.com/nvax"})
    db.upsert_catalyst({"ticker": "ABCD", "company": "Abcd Demo Bio Corp",
                        "drug": "Drugzumab", "catalyst_type": "PDUFA",
                        "expected_date": (today + timedelta(days=7)).isoformat(),
                        "date_precision": "day", "status": "upcoming",
                        "needs_review": False, "source_url": "https://example.com/abcd"})
    db.upsert_catalyst({"ticker": "ZZZZ", "company": "Zzzz Pharma",
                        "drug": "Zedrug", "catalyst_type": "PDUFA",
                        "expected_date": (today - timedelta(days=4)).isoformat(),
                        "date_precision": "day", "status": "upcoming",
                        "needs_review": False, "source_url": "https://example.com/zzzz"})
    print("seeded catalysts:", [(c["ticker"], c["expected_date"]) for c in db.catalysts])

    banner("STAGE 1 — poll_sec (every 10 min)")
    stats, sm, disp, _ = ctx(db, cfg, "sec", "poll_sec")
    SecForm4Feed(cfg).run(db, disp, sm, stats, FixtureSource("tests/fixtures/sec"))
    stats.log()

    banner("STAGE 2 — poll_fda (every 15 min): press + clinicaltrials")
    stats, sm, disp, _ = ctx(db, cfg, "fda", "poll_fda")
    fda = FdaCatalystFeed(cfg)
    fda.run_press(db, disp, sm, stats, FixturePressSource("tests/fixtures/fda"))
    # baseline a trial so the CT change fires
    db.upsert_ct_trial({"nct_id": "NCT09990001", "ticker": "ABCD",
                        "sponsor": "Abcd Demo Bio Corp", "overall_status": "Recruiting",
                        "primary_completion_date": "2026-09-30",
                        "last_checked": now_utc().isoformat()})
    fda.run_clinicaltrials(db, disp, sm, stats, FixtureCtSource("tests/fixtures/fda"))
    stats.log()

    banner("STAGE 3 — daily_digest (reminders + overdue + digests)")
    stats, sm, disp, _ = ctx(db, cfg, "fda", "daily_digest")
    sec_disp = AlertDispatcher(db, disp.telegram, cfg, feed="sec", stats=stats)
    fda.run_reminders(db, disp, sm, stats)
    fda.run_overdue(db, disp, sm, stats)
    fda_digest = dd._build_fda_digest(db, today)
    if fda_digest:
        disp.dispatch("fda:digest:%s" % today, "digest",
                      cfg["delivery"]["channels"]["fda_paid"],
                      cfg["delivery"]["channels"]["fda_free"], fda_digest)
    sec_digest = dd._build_sec_digest(db)
    if sec_digest:
        sec_disp.dispatch("sec:digest:%s" % today, "digest",
                          cfg["delivery"]["channels"]["sec_paid"],
                          cfg["delivery"]["channels"]["sec_free"], sec_digest)
    stats.log()

    banner("STAGE 4 — release_delayed (hourly): 24h-old copies -> FREE channels")
    print("delayed_queue size before:", len(db.delayed))
    # time-warp: pretend 25h elapsed so everything queued is now due
    for d in db.delayed:
        d["available_at"] = now_utc() - timedelta(hours=1)
    stats = RunStats("release_delayed")
    tg = TelegramClient("paid-token", cfg["delivery"]["disclaimer"], dry_run=True)
    stale = cfg.get_path("idempotency.pending_stale_minutes", 30)
    for row in db.get_due_delayed():
        free_key = row["dedup_key"] + ":free"
        if not db.claim_alert(free_key, row["feed"], row["free_channel"],
                              "delayed_release", {}, stale):
            db.mark_delayed_released(row["id"]); continue
        try:
            tg.send(row["free_channel"], row["html"])
        except TelegramError:
            db.release_alert(free_key); continue
        db.confirm_alert(free_key); db.mark_delayed_released(row["id"])
        stats.inc("released")
    stats.log()

    banner("STAGE 5 — re-run release_delayed (idempotency check)")
    stats = RunStats("release_delayed_again")
    for row in db.get_due_delayed():
        stats.inc("still_due")
    print("items still due after release:", stats.counts.get("still_due", 0), "(expect 0)")


if __name__ == "__main__":
    main()
