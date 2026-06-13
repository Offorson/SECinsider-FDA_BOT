"""Offline proof that the SEC digest only re-posts when the cluster set changes."""
from src.core.config import load_config
from src.core.db import MemoryBackend
from src.core.telegram import TelegramClient
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.jobs import daily_digest as dd


def run_once(db, disp, label):
    res = dd._build_sec_digest(db)
    assert res, "expected a digest"
    html, fp = res
    sent = disp.dispatch(f"sec:digest:{fp}", "digest", "sec_paid", "sec_free", html)
    print(f"{label}: fp={fp}  -> {'POSTED' if sent else 'skipped (dedup)'}")
    return sent


def main():
    setup_logging(False)
    cfg = load_config()
    db = MemoryBackend()
    tg = TelegramClient("", cfg["delivery"]["disclaimer"], dry_run=True)
    disp = AlertDispatcher(db, tg, cfg, feed="sec", stats=RunStats("t"))

    clusters = [
        {"ticker": "QNT",  "member_count": 10, "combined_value": 24649920},
        {"ticker": "INIO", "member_count": 4,  "combined_value": 4559976},
        {"ticker": "SSMR", "member_count": 5,  "combined_value": 804128},
    ]
    db.get_active_clusters = lambda: [dict(c) for c in clusters]

    print("=== Day 1 (first time this set is seen) ===")
    assert run_once(db, disp, "Day 1") is True

    print("\n=== Day 2 (identical clusters — should NOT re-post) ===")
    assert run_once(db, disp, "Day 2") is False

    print("\n=== Day 3 (value drifts as a buy ages out, count unchanged — still no re-post) ===")
    clusters[0]["combined_value"] = 22000000
    assert run_once(db, disp, "Day 3") is False

    print("\n=== Day 4 (QNT gains an insider: 10 -> 11 — real change, re-posts) ===")
    clusters[0]["member_count"] = 11
    assert run_once(db, disp, "Day 4") is True

    print("\n=== Day 5 (same 11 — quiet again) ===")
    assert run_once(db, disp, "Day 5") is False

    print("\nALL ASSERTIONS PASSED ✓")


if __name__ == "__main__":
    main()
