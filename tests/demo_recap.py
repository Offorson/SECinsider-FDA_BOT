"""Offline test of the weekly recap with a mock price source. No network."""
from datetime import date, timedelta
from src.core.config import load_config
from src.core.db import MemoryBackend, now_utc
from src.core.telegram import TelegramClient
from src.core.alerting import RunStats, setup_logging
from src.jobs import weekly_recap as wr


class MockSource:
    def __init__(self, data): self.data = data
    def history(self, ticker): return self.data.get(ticker, [])


def daily(start_price, start_date, days, drift, spike_day=None, spike_pct=0.0):
    # build a simple daily series of (date, close, high). high sits a little above
    # close; spike_day injects a one-day intraday high to exercise peak tracking.
    out = []
    p = start_price
    for i in range(days):
        close = round(p, 2)
        high = close * 1.01
        if spike_day is not None and i == spike_day:
            high = close * (1 + spike_pct)
        out.append((start_date + timedelta(days=i), close, round(high, 2)))
        p *= (1 + drift)
    return out


def main():
    setup_logging(False)
    cfg = load_config()
    db = MemoryBackend()
    today = now_utc().date()

    # 3 alerts of varying age: 40d (has 1W+1M), 10d (has 1W), 3d (maturing)
    db.record_alert_performance("sec:cluster:NVAX:3:aa", "sec", "cluster", "NVAX", today - timedelta(days=40))
    db.record_alert_performance("sec:single:x1", "sec", "single_buy", "MTDR", today - timedelta(days=10))
    db.record_alert_performance("sec:single:x2", "sec", "single_buy", "BZUN", today - timedelta(days=3))

    src = MockSource({
        "NVAX": daily(10.0, today - timedelta(days=45), 60, 0.01),    # rising ~+1%/day
        "MTDR": daily(50.0, today - timedelta(days=15), 30, -0.005),  # falling
        "BZUN": daily(20.0, today - timedelta(days=8), 20, 0.002, spike_day=7, spike_pct=0.15),
    })

    stats = RunStats("recap-test")
    rows = db.get_performance_rows((today - timedelta(days=90)).isoformat())
    results = wr.price_and_score(db, src, today, rows, stats)
    html, xtext = wr.build_recap(results, today, 90, 40)

    print("================= RECAP (paid channel) =================")
    tg = TelegramClient("", cfg["delivery"]["disclaimer"], dry_run=True)
    tg.send("paid", html)
    print("\n================= COPY-FOR-X block =================")
    print(xtext)
    print("\nstats:", stats.summary())
    print("persisted (ticker, since-not-stored; ret_peak, ret_1w, ret_1m):",
          [(r["ticker"], r.get("ret_peak"), r.get("ret_1w"), r.get("ret_1m")) for r in db.performance.values()])


if __name__ == "__main__":
    main()
