"""Free price data — Stooq daily CSV endpoint. No API key, no paid services.

Stooq serves daily OHLC history as CSV at
``https://stooq.com/q/d/l/?s=<symbol>&i=d`` (symbol = lowercase US ticker + ".us",
e.g. nvda.us). We pull the full history once per ticker and compute alert-date
close, +1W/+1M/+3M closes, and the latest close from it.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Optional

import requests

log = logging.getLogger("prices")


class StooqPriceSource:
    def __init__(self, url_template: str = "https://stooq.com/q/d/l/?s={symbol}&i=d",
                 timeout: int = 20, user_agent: str = "SecBiotechBot") -> None:
        self.url_template = url_template
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent}
        self.session = requests.Session()

    @staticmethod
    def _symbol(ticker: str) -> str:
        return ticker.strip().lower().replace(".", "-") + ".us"

    def history(self, ticker: str) -> list[tuple[date, float]]:
        """Return sorted [(date, close)] for a US ticker, or [] if unavailable."""
        url = self.url_template.format(symbol=self._symbol(ticker))
        try:
            r = self.session.get(url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Stooq fetch failed for %s: %s", ticker, exc)
            return []
        text = (r.text or "").strip()
        if not text or text[:1] == "<" or "no data" in text.lower():
            return []
        out: list[tuple[date, float]] = []
        for row in csv.DictReader(io.StringIO(text)):
            try:
                d = datetime.strptime(row["Date"], "%Y-%m-%d").date()
                c = float(row["Close"])
            except (KeyError, ValueError, TypeError):
                continue
            out.append((d, c))
        out.sort()
        return out


# ----- helpers over a sorted [(date, close)] history -----------------------
def close_asof(hist: list[tuple[date, float]], target: date) -> Optional[float]:
    """Last close on/before target (handles non-trading alert dates)."""
    best = None
    for d, c in hist:
        if d <= target:
            best = c
        else:
            break
    return best


def close_on_or_after(hist: list[tuple[date, float]], target: date) -> Optional[float]:
    """First close on/after target (the window close)."""
    for d, c in hist:
        if d >= target:
            return c
    return None


def last_close(hist: list[tuple[date, float]]) -> Optional[float]:
    return hist[-1][1] if hist else None


def pct_return(base: Optional[float], later: Optional[float]) -> Optional[float]:
    if not base or later is None:
        return None
    return (later / base - 1.0) * 100.0
