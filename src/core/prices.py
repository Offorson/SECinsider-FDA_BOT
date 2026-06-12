"""Free price data — Yahoo Finance chart API (primary) + Stooq CSV (fallback).

No API key, no paid services. Yahoo's chart endpoint is the same data yfinance
uses; reliable from CI when sent a browser User-Agent. Stooq's free CSV often
blocks cloud IPs, so it is only a fallback. History is returned as sorted
``[(date, close, high)]`` — the daily high is used for peak-return tracking.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("prices")

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _parse_yahoo_chart(data: dict) -> list[tuple[date, float, float]]:
    """Parse a Yahoo v8 chart payload into sorted [(date, close, high)]."""
    res = ((data or {}).get("chart") or {}).get("result") or []
    if not res:
        return []
    node = res[0]
    ts = node.get("timestamp") or []
    q = ((node.get("indicators") or {}).get("quote") or [{}])
    q0 = q[0] if q else {}
    closes = q0.get("close") or []
    highs = q0.get("high") or []
    out: list[tuple[date, float, float]] = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        h = highs[i] if i < len(highs) else None
        try:
            d = datetime.fromtimestamp(t, tz=timezone.utc).date()
            out.append((d, float(c), float(h) if h is not None else float(c)))
        except (TypeError, ValueError, OSError):
            continue
    out.sort()
    return out


class YahooPriceSource:
    HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")

    def __init__(self, timeout: int = 20, rng: str = "6mo") -> None:
        self.timeout = timeout
        self.rng = rng
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _BROWSER_UA, "Accept": "application/json"})

    @staticmethod
    def _symbol(ticker: str) -> str:
        return ticker.strip().upper().replace(".", "-")

    def history(self, ticker: str) -> list[tuple[date, float, float]]:
        sym = self._symbol(ticker)
        for host in self.HOSTS:
            url = f"{host}/v8/finance/chart/{sym}?range={self.rng}&interval=1d"
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    log.warning("Yahoo %s for %s on %s", r.status_code, ticker, host)
                    continue
                hist = _parse_yahoo_chart(r.json())
            except (requests.RequestException, ValueError) as exc:
                log.warning("Yahoo fetch failed for %s (%s): %s", ticker, host, exc)
                continue
            if hist:
                return hist
        return []


class StooqPriceSource:
    def __init__(self, url_template: str = "https://stooq.com/q/d/l/?s={symbol}&i=d",
                 timeout: int = 20) -> None:
        self.url_template = url_template
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _BROWSER_UA})

    @staticmethod
    def _symbol(ticker: str) -> str:
        return ticker.strip().lower().replace(".", "-") + ".us"

    def history(self, ticker: str) -> list[tuple[date, float, float]]:
        url = self.url_template.format(symbol=self._symbol(ticker))
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Stooq fetch failed for %s: %s", ticker, exc)
            return []
        text = (r.text or "").strip()
        low = text.lower()
        if not text or text[:1] == "<" or "no data" in low or "exceeded" in low:
            return []
        out: list[tuple[date, float, float]] = []
        for row in csv.DictReader(io.StringIO(text)):
            try:
                d = datetime.strptime(row["Date"], "%Y-%m-%d").date()
                c = float(row["Close"])
                hv = row.get("High")
                h = float(hv) if hv not in (None, "", "N/D") else c
            except (KeyError, ValueError, TypeError):
                continue
            out.append((d, c, h))
        out.sort()
        return out


class AutoPriceSource:
    """Yahoo first, Stooq fallback."""

    def __init__(self) -> None:
        self.yahoo = YahooPriceSource()
        self.stooq = StooqPriceSource()

    def history(self, ticker: str) -> list[tuple[date, float, float]]:
        hist = self.yahoo.history(ticker)
        if hist:
            return hist
        log.info("Yahoo had no data for %s — trying Stooq.", ticker)
        return self.stooq.history(ticker)


def make_source(cfg):
    pcfg = (cfg.get("performance", {}) or {})
    which = (pcfg.get("price_source") or "auto").lower()
    if which == "stooq":
        return StooqPriceSource(url_template=pcfg.get(
            "stooq_csv", "https://stooq.com/q/d/l/?s={symbol}&i=d"))
    if which == "yahoo":
        return YahooPriceSource()
    return AutoPriceSource()


# ----- helpers over a sorted [(date, close, high)] history -----------------
def close_asof(hist, target: date) -> Optional[float]:
    best = None
    for bar in hist:
        if bar[0] <= target:
            best = bar[1]
        else:
            break
    return best


def close_on_or_after(hist, target: date) -> Optional[float]:
    for bar in hist:
        if bar[0] >= target:
            return bar[1]
    return None


def last_close(hist) -> Optional[float]:
    return hist[-1][1] if hist else None


def peak_return(hist, since_date: date, base: Optional[float]) -> Optional[float]:
    """Max % return reached on any daily HIGH on/after since_date vs base."""
    if not base:
        return None
    best = None
    for bar in hist:
        if bar[0] < since_date:
            continue
        high = bar[2] if len(bar) > 2 else bar[1]
        if high is None:
            continue
        r = (high / base - 1.0) * 100.0
        if best is None or r > best:
            best = r
    return best


def pct_return(base: Optional[float], later: Optional[float]) -> Optional[float]:
    if not base or later is None:
        return None
    return (later / base - 1.0) * 100.0
