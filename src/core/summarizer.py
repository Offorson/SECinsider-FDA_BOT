"""Swappable summary/rendering layer.

Three backends, selected by ``config.summarizer.backend``:

* ``template`` (DEFAULT) — pure deterministic string formatting. No API, no
  network, never fails. This is also the universal fallback.
* ``gemini``  — Google Gemini free tier. Used to paraphrase facts and to extract
  structured catalyst fields from press-release text. The prompt FORBIDS any
  buy/sell/advice language. ANY failure (network, quota, parse) silently falls
  back to the template — an alert is never dropped because the AI failed.
* ``claude``  — stub for later; currently delegates to the template.

The feeds hand this module structured *facts* dicts; the renderers turn them into
mobile-readable Telegram HTML. Dynamic text is HTML-escaped here; structural tags
(<b>, <a>) are literal. The Telegram layer does NOT re-escape.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.core.telegram import escape_html

log = logging.getLogger("summarizer")

# A hard instruction reused by every Gemini call. Compliance-critical.
_FACTUAL_GUARD = (
    "You are a compliance-bound financial DATA reporter. Report ONLY the facts "
    "given. NEVER recommend, suggest, or imply buying, selling, or holding any "
    "security. No opinions, no price targets, no sentiment, no words like 'bullish', "
    "'opportunity', 'should', or 'buy'. Plain factual statements only."
)

# Plain-English explanation of each catalyst type, shown in press alerts so a
# reader who doesn't know the jargon understands what the headline implies.
# Factual descriptions of the regulatory step only — no outlook, no advice.
_CATALYST_GLOSS = {
    "PDUFA": ("The FDA has accepted or is reviewing this drug application; an "
              "approve-or-decline decision will come on a target (PDUFA) date. "
              "This is not an approval yet."),
    "AdCom": ("An FDA advisory committee will publicly review this drug and vote. "
              "The vote advises the FDA but is non-binding."),
    "Approval": "The FDA has approved this drug.",
    "CRL": ("The FDA declined to approve this drug in its current form "
            "(Complete Response Letter)."),
    "Readout": "Clinical-trial results have been reported.",
}


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_shares(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


class Summarizer:
    def __init__(self, backend: str = "template", gemini_api_key: str = "",
                 gemini_model: str = "gemini-1.5-flash") -> None:
        self.backend = (backend or "template").lower()
        self.gemini_api_key = gemini_api_key or ""
        self.gemini_model = gemini_model
        self._gemini = None
        if self.backend == "gemini" and not self.gemini_api_key:
            log.warning("summarizer.backend=gemini but GEMINI_API_KEY is empty; "
                        "using template fallback for every call.")

    # ----- public render API (feeds call these) ----------------------------
    def render_cluster(self, facts: dict) -> str:
        return self._maybe_gemini(self._tmpl_cluster, self._gemini_cluster, facts)

    def render_single_buy(self, facts: dict) -> str:
        return self._maybe_gemini(self._tmpl_single, self._gemini_single, facts)

    def render_press(self, facts: dict) -> str:
        # News body: template is already factual + safe; Gemini only paraphrases.
        return self._maybe_gemini(self._tmpl_press, self._gemini_press, facts)

    def extract_catalyst_fields(self, title: str, body: str) -> Optional[dict]:
        """Return {ticker, drug, catalyst_type, date, date_precision} or None.

        Template/claude backends can't parse free text -> return None so the feed
        flags the row needs_review. Gemini attempts structured extraction; any
        failure also returns None (safe: needs_review).
        """
        if self.backend != "gemini" or not self.gemini_api_key:
            return None
        try:
            prompt = (
                f"{_FACTUAL_GUARD}\n\nExtract catalyst fields from this biotech "
                "press release as STRICT JSON with keys: ticker (US exchange "
                "symbol or null), drug (name or null), catalyst_type (one of "
                "PDUFA, AdCom, Approval, CRL, Readout, or null), date (ISO "
                "YYYY-MM-DD or YYYY-MM or null), date_precision (day|month|quarter|"
                "null). Output ONLY the JSON object.\n\n"
                f"TITLE: {title}\n\nBODY: {body[:4000]}"
            )
            text = self._gemini_generate(prompt)
            data = json.loads(_strip_code_fence(text))
            if isinstance(data, dict):
                return data
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("gemini extract failed, needs_review: %s", exc)
            return None

    # ----- dispatch helper --------------------------------------------------
    def _maybe_gemini(self, tmpl_fn, gemini_fn, facts: dict) -> str:
        if self.backend == "gemini" and self.gemini_api_key:
            try:
                out = gemini_fn(facts)
                if out and out.strip():
                    return out
            except Exception as exc:  # noqa: BLE001
                log.debug("gemini render failed, using template: %s", exc)
        # template + claude(stub) + any gemini failure
        return tmpl_fn(facts)

    # ----- TEMPLATE renderers (canonical + fallback) ------------------------
    def _tmpl_cluster(self, f: dict) -> str:
        ticker = escape_html(str(f.get("ticker", "")))
        issuer = escape_html(str(f.get("issuer_name") or ""))
        count = f.get("member_count", 0)
        window = f.get("window_days", "?")
        if f.get("is_upgrade"):
            lines = [f"⬆️ <b>Cluster Upgrade · ${ticker}</b>"]
        else:
            lines = [f"🟢 <b>Insider Cluster Buy · ${ticker}</b>"]
        if issuer:
            lines.append(f"<i>{issuer}</i>")
        lines.append("")
        if f.get("is_upgrade"):
            lines.append(f"Now <b>{count} insiders</b> (was {f.get('prev_count', '?')}) "
                         f"buying within {window} days")
        else:
            lines.append(f"<b>{count} insiders</b> bought in the last {window} days")
        summary = (f"Combined <b>{_fmt_money(f.get('combined_value'))}</b> · "
                   f"avg {_fmt_price(f.get('avg_price'))}/sh")
        ft = f.get("first_time_count", 0)
        if ft:
            summary += f" · {ft} first-time"
        lines.append(summary)
        lines.append("")
        for b in f.get("buys", []):
            tag = " 🆕" if b.get("first_time") else ""
            lines.append(
                "• <b>" + escape_html(str(b.get("name", "Unknown"))) + "</b> (" +
                escape_html(str(b.get("roles", "insider"))) + ") — " +
                _fmt_shares(b.get("shares")) + " sh @ " + _fmt_price(b.get("price")) +
                " = <b>" + _fmt_money(b.get("total_value")) + "</b>" + tag)
        if f.get("catalyst_note"):
            lines.append("")
            lines.append(escape_html(f["catalyst_note"]))
        if f.get("source_url"):
            url = escape_html(f["source_url"])
            lines.append("")
            lines.append(f'🔗 <a href="{url}">View on EDGAR</a>')
        return "\n".join(lines)

    def _tmpl_single(self, f: dict) -> str:
        ticker = escape_html(str(f.get("ticker", "")))
        issuer = escape_html(str(f.get("issuer_name") or ""))
        lines = [f"🔵 <b>Large Insider Buy · ${ticker}</b>"]
        if issuer:
            lines.append(f"<i>{issuer}</i>")
        lines.append("")
        name = escape_html(str(f.get("insider_name", "Unknown")))
        roles = escape_html(str(f.get("roles", "insider")))
        lines.append(f"<b>{name}</b> ({roles})")
        lines.append(
            _fmt_shares(f.get("shares")) + " sh @ " + _fmt_price(f.get("price")) +
            " = <b>" + _fmt_money(f.get("total_value")) + "</b>")
        if f.get("catalyst_note"):
            lines.append("")
            lines.append(escape_html(f["catalyst_note"]))
        if f.get("source_url"):
            url = escape_html(f["source_url"])
            lines.append("")
            lines.append(f'🔗 <a href="{url}">View on EDGAR</a>')
        return "\n".join(lines)

    def _tmpl_press(self, f: dict) -> str:
        title = escape_html(str(f.get("title", "")))
        ticker = str(f.get("ticker") or "").strip()
        head = "📰 <b>FDA / Catalyst News</b>"
        if ticker:
            head = f"📰 <b>${escape_html(ticker)}</b> · FDA / Catalyst News"
        lines = [head, "", title]
        note = _CATALYST_GLOSS.get(str(f.get("catalyst_type") or ""))
        if note:
            lines.append("")
            lines.append("<i>What this means:</i> " + escape_html(note))
        if f.get("catalyst_note"):
            lines.append("")
            lines.append(escape_html(f["catalyst_note"]))
        if f.get("link"):
            url = escape_html(f["link"])
            lines.append("")
            lines.append(f'🔗 <a href="{url}">Read the release</a>')
        return "\n".join(lines)

    # ----- GEMINI renderers (paraphrase only; structure stays template) -----
    def _gemini_cluster(self, f: dict) -> str:
        base = self._tmpl_cluster(f)
        return self._gemini_paraphrase(base)

    def _gemini_single(self, f: dict) -> str:
        base = self._tmpl_single(f)
        return self._gemini_paraphrase(base)

    def _gemini_press(self, f: dict) -> str:
        base = self._tmpl_press(f)
        return self._gemini_paraphrase(base)

    def _gemini_paraphrase(self, html_body: str) -> str:
        prompt = (
            f"{_FACTUAL_GUARD}\n\nRewrite the following alert to be concise and "
            "mobile-readable. KEEP every number, name, ticker, and the <a> link "
            "EXACTLY. Keep simple HTML tags (<b>, <a href>). Do not add any "
            "commentary or recommendation. Return only the rewritten alert.\n\n"
            f"{html_body}"
        )
        out = self._gemini_generate(prompt)
        return out or html_body

    # ----- low-level Gemini call -------------------------------------------
    def _gemini_generate(self, prompt: str) -> str:
        if self._gemini is None:
            import google.generativeai as genai  # lazy import
            genai.configure(api_key=self.gemini_api_key)
            self._gemini = genai.GenerativeModel(self.gemini_model)
        resp = self._gemini.generate_content(prompt)
        return (resp.text or "").strip()


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t[: -3]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    return t.strip()
