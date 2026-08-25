# TradeRadar landing page

A single, self-contained static page: `index.html`. No build step, no
framework, no server. Every style, script, and asset is inline, so you can
open the file directly or host it as-is.

The two Join buttons link to the live Telegram channels:
- Insider Trading Radar: https://t.me/InsiderTradeRader
- FDA Radar Alerts: https://t.me/FDARaderAlerts

## Option 1 — Vercel (recommended for a clean shareable link)

Because the page lives in this `landing/` subfolder, point Vercel at that
folder rather than the repo root (the repo root is the Python bot, not a site).

1. Push this repo to GitHub.
2. Go to https://vercel.com, sign in with GitHub, click **Add New > Project**.
3. Import the `SECinsider-FDA_BOT` repo.
4. Set **Root Directory** to `landing`.
5. **Framework Preset**: Other. No build command, no output dir needed.
6. Click **Deploy**.

You get a link like `https://traderadar.vercel.app` to send anyone. You can
add a custom domain later under the project's Domains tab.

CLI alternative (no GitHub push needed): install the Vercel CLI, run `vercel`
from inside the `landing` folder, and follow the prompts.

## Option 2 — Fastest link, no account or repo needed

Go to https://app.netlify.com/drop and drag the `landing` folder (or just
`index.html`) onto the page. You get a public URL within seconds. Good for
sending a quick preview.

## Option 3 — GitHub Pages (free, uses the repo you already have)

GitHub Pages serves from the repo root or a `/docs` folder, not from an
arbitrary subfolder. To use it, copy `index.html` to a `docs/` folder at the
repo root, then in the repo go to **Settings > Pages** and set the source to
branch `main`, folder `/docs`. The link will look like
`https://offorson.github.io/SECinsider-FDA_BOT/`.

## Updating the page

Edit `index.html`, then redeploy. On Vercel and GitHub Pages a new push
redeploys automatically. On Netlify Drop, drop the file again.
