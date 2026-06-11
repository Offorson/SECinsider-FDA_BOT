# SEC Insider Cluster Bot + FDA Catalyst Bot

Two automated Telegram alert feeds in one codebase, running on free infrastructure:

1. **SEC Insider Cluster Bot** — watches EDGAR for Form 4 open-market purchases and
   fires when several insiders at the same company buy within a short window
   ("cluster buys"), plus standalone large buys.
2. **FDA Catalyst Bot** — keeps a calendar of biotech catalysts (PDUFA dates, AdComs,
   readouts), reminds you before each one, and alerts instantly on FDA news and
   ClinicalTrials.gov status changes.

Both share one database and one delivery layer, and publish to four Telegram
channels (SEC-free, SEC-paid, FDA-free, FDA-paid). Paid channels get alerts in real
time; the free channels get the same alerts 24 hours later.

**Cost: $0.** Everything runs on the free tiers of GitHub Actions, Supabase, Telegram,
and (optionally) Google Gemini. No servers, no Docker, no credit card.

> ⚠️ **Not investment advice.** This system reports public filing facts only. Every
> alert ends with the disclaimer footer. It never tells anyone to buy or sell.

---

## How it works (30-second tour)

```
GitHub Actions (cron)        Python jobs                 Supabase (Postgres)        Telegram
  every 10 min  ───────────►  poll_sec.py  ──────────►   filings / insider_buys  ─► SEC paid channel
  every 15 min  ───────────►  poll_fda.py  ──────────►   catalysts / press_seen  ─► FDA paid channel
  daily         ───────────►  daily_digest.py ───────►   reminders/overdue/digest
  hourly        ───────────►  release_delayed.py ────►   delayed_queue           ─► free channels (24h later)
```

* **State** lives entirely in Supabase (Actions runners are wiped after each run).
* **Idempotency**: every alert claims a unique key in `alerts_sent` before sending, so
  a job can crash and re-run with **zero duplicate alerts**. (See "Idempotency design".)
* **Summaries** go through a swappable `summarizer.py` (`template` by default — no AI;
  or `gemini`). If Gemini ever fails, it silently falls back to the template.

---

## What you'll set up (≈30–45 minutes, no DevOps experience needed)

You will create four free accounts/keys and paste some values into GitHub. Follow the
steps in order. Where the system needs a value from you, it is read from an environment
variable (a "secret") — never hard-coded.

### Prerequisites
* A **GitHub account** and a copy of this repo in your own account (fork it, or push it
  as a new repo). Make the repo **public** so Actions minutes are free.
* Python 3.11+ on your computer **only if** you want to run the local dry-run test
  (recommended but optional — you can also trigger everything from the Actions tab).

---

## Step 1 — Create the database (Supabase)

1. Go to <https://supabase.com> → sign in → **New project**. Pick any name and a strong
   database password (you won't need the password again here). Choose the free plan.
2. Wait ~2 minutes for it to provision.
3. In the left sidebar open **SQL Editor** → **New query**. Open `schema.sql` from this
   repo, copy its entire contents, paste, and click **Run**. You should see "Success".
   This creates every table, index, and unique constraint in one shot.
4. Open **Project Settings → API**. Copy two values for later:
   * **Project URL** (looks like `https://abcdefgh.supabase.co`) → this is `SUPABASE_URL`.
   * **service_role key** (under "Project API keys") → this is `SUPABASE_KEY`.
     Use the **service_role** key, not `anon`. It is secret; treat it like a password.

---

## Step 2 — Create the Telegram bot and channels

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts. It gives you a
   **bot token** like `123456789:AAH...`. That is `TELEGRAM_BOT_TOKEN`.
2. Create **four channels** (Telegram → New Channel). Suggested names:
   *SEC Alerts (Paid)*, *SEC Alerts (Free)*, *FDA Alerts (Paid)*, *FDA Alerts (Free)*.
   They can be private (Whop will manage who joins).
3. For **each** channel: open it → **Administrators** → **Add Admin** → search your
   bot's username → add it with permission to **Post Messages**.
4. Get each channel's numeric ID (private channels look like `-1001234567890`):
   * Easiest: post any message in the channel, then **forward** that message to
     **@getidsbot** (or **@JsonDumpBot**). It replies with the channel ID.
   * Or, after your bot has posted at least once, open
     `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read the
     `chat.id`.
   Record the four IDs as:
   `TELEGRAM_SEC_PAID_CHANNEL`, `TELEGRAM_SEC_FREE_CHANNEL`,
   `TELEGRAM_FDA_PAID_CHANNEL`, `TELEGRAM_FDA_FREE_CHANNEL`.

---

## Step 3 — (Optional) Get a Gemini API key

The default summarizer backend is `template` (pure formatting, no AI) and works fully
without any key. If you want AI-paraphrased alerts and automatic date extraction from
press releases:

1. Go to <https://aistudio.google.com/app/apikey> → **Create API key** (free).
2. Save it as `GEMINI_API_KEY`.
3. In `config.yaml`, set `summarizer.backend: gemini`.

If you skip this, leave `GEMINI_API_KEY` blank and `backend: template`. Auto-captured
catalysts will simply be flagged `needs_review` for you to confirm.

---

## Step 4 — Add your secrets to GitHub

In your repo on GitHub: **Settings → Secrets and variables → Actions → New repository
secret**. Add one secret per row below (names must match exactly):

| Secret name | Value | Required? |
|---|---|---|
| `SUPABASE_URL` | from Step 1 | yes |
| `SUPABASE_KEY` | service_role key from Step 1 | yes |
| `TELEGRAM_BOT_TOKEN` | from Step 2 | yes |
| `SEC_CONTACT_EMAIL` | a real email you monitor (SEC requires it) | yes |
| `SEC_APP_NAME` | e.g. `SecBiotechBot` | recommended |
| `TELEGRAM_SEC_PAID_CHANNEL` | channel ID | yes |
| `TELEGRAM_SEC_FREE_CHANNEL` | channel ID | yes |
| `TELEGRAM_FDA_PAID_CHANNEL` | channel ID | yes |
| `TELEGRAM_FDA_FREE_CHANNEL` | channel ID | yes |
| `GEMINI_API_KEY` | from Step 3 | only if backend = gemini |

`.env.example` documents the same variables for local runs (copy it to `.env` and fill
it in — `.env` is git-ignored and must never be committed).

---

## Step 5 — Warm up the history (backfill + seed)

These are one-time setup runs. You can run them locally (recommended) or from the
Actions tab once workflows are enabled.

**Locally:**

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and paste your real values

# 1) Pull ~90 days of Form 4 history so cluster windows and first-time-buyer
#    flags are accurate from day one. This makes many EDGAR requests and can take
#    a while; reduce --days or set --max-filings to bound it.
python -m src.scripts.backfill_form4 --days 90

# 2) Load your known catalyst dates. Edit catalysts_seed.csv first (a 5-row sample
#    is included showing the format), then:
python -m src.scripts.seed_catalysts catalysts_seed.csv
```

`seed_catalysts.py` validates every row, reports rejects with reasons, and never
crashes on a bad row. Re-running the same CSV is safe (duplicates are ignored).

---

## Step 6 — Dry-run test (do this before going live)

Dry-run runs the **entire** pipeline but **prints** alerts to your screen instead of
posting to Telegram. No token or even database is required — without `SUPABASE_URL` it
uses an in-memory store and bundled sample filings.

```bash
# SEC, against bundled offline fixtures (no network, no DB needed):
python -m src.jobs.poll_sec --dry-run --fixtures tests/fixtures/sec

# FDA, against bundled offline fixtures:
python -m src.jobs.poll_fda --dry-run --fixtures tests/fixtures/fda

# Validate your catalyst CSV without writing:
python -m src.scripts.seed_catalysts catalysts_seed.csv --dry-run

# Live data, still printing only (uses your real Supabase + EDGAR, posts nothing):
python -m src.jobs.poll_sec --dry-run
python -m src.jobs.poll_fda --dry-run
```

You should see formatted alerts ending with `📋 Public filing data. Not investment
advice.` When that looks right, continue.

---

## Step 7 — Go live

The four workflows in `.github/workflows/` are already scheduled:

| Workflow | Schedule (UTC cron) | What it does |
|---|---|---|
| `poll_sec.yml` | `*/10 * * * *` (every 10 min) | SEC Form 4 clusters + large buys |
| `poll_fda.yml` | `*/15 * * * *` (every 15 min) | FDA press + ClinicalTrials.gov |
| `daily_digest.yml` | `30 13 * * *` (daily) | reminders, overdue, digest |
| `release_delayed.yml` | `5 * * * *` (hourly) | 24h-delayed posts to free channels |

1. Push your repo (with secrets already set) to GitHub.
2. Open the **Actions** tab. If prompted, click **"I understand my workflows, enable
   them."**
3. Click any workflow → **Run workflow** (the `workflow_dispatch` button) to fire it
   once immediately and confirm it posts to your paid channel.
4. From then on it runs on the schedule, unattended.

> GitHub's scheduled cron is best-effort and can lag a few minutes under load — a
> 10-minute schedule may occasionally run at 12–13 minutes. That is normal and within
> the spec's "~10 minute" target.

---

## ✅ Confirm-before-launch checklist

Tick every box before enabling the live workflows:

- [ ] `schema.sql` ran successfully in Supabase (all tables exist under **Table Editor**).
- [ ] All required GitHub secrets from Step 4 are set, names spelled exactly.
- [ ] `SEC_CONTACT_EMAIL` is a **real, monitored** email (SEC may contact you; an
      invalid UA can get you rate-limited or blocked).
- [ ] The bot is an **admin with Post permission** in all four channels.
- [ ] A `--dry-run` of `poll_sec` and `poll_fda` printed correctly-formatted alerts.
- [ ] **Confirm the feed URLs in `config.yaml`** (these are marked `# CONFIRM`):
  - [ ] `sec.feed_url` — the EDGAR latest-Form-4 Atom feed returns entries.
  - [ ] `fda.press_feeds[].url` — the GlobeNewswire and PR Newswire RSS URLs are the
        correct health/biotech feeds for your needs. The included URLs are reasonable
        defaults but **must be verified** (publishers change feed paths). Open each URL
        in a browser; if it 404s or isn't biotech news, replace it.
- [ ] You ran `backfill_form4.py` once (optional but recommended for accurate flags).
- [ ] You seeded your real catalyst dates with `seed_catalysts.py`.
- [ ] `summarizer.backend` in `config.yaml` is what you want (`template` or `gemini`),
      and if `gemini`, `GEMINI_API_KEY` is set.
- [ ] You fired each workflow once via **Run workflow** and saw a real post arrive.

---

## Configuration reference (`config.yaml`)

All tunables live here; **no secrets**. Highlights:

* `sec.min_total_value` (default `25000`) — ignore qualifying buys smaller than this.
* `sec.cluster.window_days` (`14`) and `min_distinct_insiders` (`3`) — cluster rules.
* `sec.standalone.large_buy_value` (`500000`) — standalone alert threshold; plus any
  first-ever CEO/CFO buy if `alert_first_time_ceo_cfo: true`.
* `sec.role_weights` — CEO/CFO 3, President/COO 2.5, other C-suite 2, Director 1,
  10% owner 1.5 (multiple roles take the max).
* `fda.reminder_days` (`[30,14,7,1]`), `fda.cross_feed_window_days` (`90`).
* `delivery.delay_hours` (`24`) — how long before the free channel gets a copy.
* `delivery.disclaimer` — the footer appended to every message.

Strings may reference an environment variable with `${VAR}`; it is expanded at load
time. That is how channel IDs stay out of the committed repo.

---

## Idempotency design (why you'll never get duplicates)

Every outbound alert has a deterministic `dedup_key` (e.g.
`sec:cluster:NVAX:3:<hash>`). Before sending, the dispatcher:

1. **Claims** the key by inserting a `pending` row in `alerts_sent` (a DB **unique
   constraint** makes this atomic — two concurrent runs can't both claim it).
2. **Sends** to Telegram (with exponential-backoff retries).
3. On success, marks the row **`sent`**. On hard failure, **deletes** the claim so a
   later run retries — the alert is never lost.

If a run crashes between claim and send, the `pending` row would normally block
forever; instead, a claim older than `idempotency.pending_stale_minutes` (default 30)
is treated as crash-orphaned and reclaimed. Result: **no duplicates in normal
operation, and automatic recovery from crashes.** The free-channel copy uses the same
mechanism with a `:free` key suffix.

---

## Design decisions (ambiguities resolved per the spec's "choose simpler/reliable")

* **Offline dry-run backend.** When `--dry-run` is used with no `SUPABASE_URL`, the code
  uses an in-memory store and bundled fixtures so the whole pipeline is testable with no
  database or network. Live runs always require Supabase.
* **Crash-safe claim→send→confirm** idempotency with a stale-claim recovery window
  (above), chosen over plain "check-then-send" which can duplicate on crash.
* **Disclaimer enforced structurally** in the Telegram client — appended exactly once to
  every message, so no code path can forget it.
* **One Form 4 = one aggregated buy.** Multiple code-P lines in a single filing are
  summed into one record (total shares, VWAP price); `shares_owned_before` is taken from
  the earliest transaction, and `new_stake` is flagged when prior holdings were ≤ 0.
* **EDGAR access uses JSON/Atom/index files only** — the latest-Form-4 Atom feed, each
  filing's `index.json` to locate the ownership XML, and (for backfill) the daily
  `master.idx`. No HTML page is ever scraped. All requests carry the required
  `User-Agent` and are rate-limited to ≤ 9 req/s with 429/403 back-off.
* **One active cluster per ticker** (DB partial unique index). New insiders joining fire
  an "upgrade" alert; standalone large-buy alerts are suppressed for a ticker that
  already has an active cluster (it would be redundant).
* **Backfill seeds existing clusters silently** (records them as already-alerted) so your
  first live run doesn't flood you with stale clusters — you only get upgrades going
  forward.
* **Channel IDs via `${ENV}`** — not technically secret, but referenced from env so they
  stay out of the public repo.
* **Reminders only for `day`-precision dates**; month/quarter catalysts are too imprecise
  for T-1. Overdue uses the effective end-of-month / end-of-quarter for those.
* **Template-backend press capture** creates a `needs_review` catalyst with a null date
  (it can't parse free text); the Gemini backend extracts the structured date/ticker.
* **A small `src/core/alerting.py` helper** was added (beyond the files listed in the
  spec) to keep the shared dispatch + idempotency + logging in one place. All other files
  follow the spec's structure exactly.
* **Graceful degradation:** EDGAR/feed outage → log and exit 0 (no false CI failures);
  Supabase outage → exit 1; Gemini failure → silent template fallback.

---

## Troubleshooting

**A workflow run is red / failed.**
Open the run in the Actions tab and read the log. Exit code 1 almost always means
Supabase was unreachable or a required secret is missing/misspelled. Upstream outages
(EDGAR, news feeds) exit 0 on purpose, so they won't show as failures.

**No alerts are arriving, but runs are green.**
That's normal when there's simply no qualifying activity. Check the run log's summary
line (e.g. `[poll_sec] feed_entries=100 parsed=40 qualifying_buys=0`). Lower
`sec.min_total_value` temporarily, or run a `--dry-run` to see counts.

**`Telegram post failed` / `Bad Request: chat not found`.**
The bot isn't an admin of that channel, or the channel ID is wrong (private channel IDs
start with `-100`). Re-check Step 2.

**`Missing required environment secrets: ...`.**
That secret isn't set (locally in `.env`, or as a GitHub Actions secret). Names are
case-sensitive.

**SEC requests get 403/429.**
Your `SEC_CONTACT_EMAIL` is blank or fake, or you're polling too fast. The limiter keeps
you ≤ 9 req/s; make sure you didn't lower the schedule interval.

**Press feed returns nothing.**
Open the feed URL from `config.yaml` in a browser. Publishers change RSS paths; update
the URL (see the confirm-before-launch checklist).

**Gemini errors in the log.**
Harmless — the system falls back to the template automatically. Set
`summarizer.backend: template` to silence them, or check your `GEMINI_API_KEY`/quota.

---

## Repo structure

```
src/
  feeds/   sec_form4.py        EDGAR polling + Form 4 parse + scoring + clusters
           fda_catalysts.py    catalyst calendar + press + ClinicalTrials.gov
  core/    db.py               Supabase + in-memory backends (all queries)
           telegram.py         channel posting, escaping, retry, footer
           summarizer.py       template | gemini | claude backends
           alerting.py         shared idempotent dispatch + logging/stats
           config.py           config.yaml + env secrets loader
  jobs/    poll_sec.py         entrypoint — every 10 min
           poll_fda.py         entrypoint — every 15 min
           daily_digest.py     entrypoint — daily (reminders/overdue/digest)
           release_delayed.py  entrypoint — hourly (24h free-channel release)
  scripts/ seed_catalysts.py   CSV import for manual catalyst seeding
           backfill_form4.py   pull N days of Form 4 history (run once)
schema.sql            full Supabase schema (paste-and-run)
config.yaml           all tunables (no secrets)
.env.example          every secret documented
catalysts_seed.csv    5-row sample for seeding
.github/workflows/    the four scheduled workflows
tests/fixtures/       offline sample filings/feeds for dry-run
```

---

## Compliance note

This project distributes **public regulatory filing data and public press releases**.
It is **not** investment advice and is designed to never produce buy/sell/hold language
— including the Gemini prompts, which explicitly forbid it. Every message carries the
disclaimer footer. Subscription/channel access is handled externally (Whop); this code
contains no user management or bot command handling.
