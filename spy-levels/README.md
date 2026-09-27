# SPY Levels

A small web app that maps where SPY is likely to stall or break each day:

- **Support and resistance** from dealer gamma walls, VWAP, the opening range and prior-day levels
- **Regime**: *Range day* above the gamma flip (fade the edges) or *Trend day* below it (follow breaks)
- **Setups** with entry, stop, target and reward-to-risk
- **Phone alerts** when SPY comes within $0.25 of a level or the regime changes

During market hours (Mon–Fri, 9:30 AM–4:00 PM ET) prices update every minute and the option chain every 15 minutes. Outside market hours it keeps the last session on screen and refreshes every 30 minutes.

No database and no third-party Python packages. It's one Python process that serves the page and refreshes the data in the background.

---

## 1. Try it on your Mac (2 minutes)

You need Python 3.9 or newer (`python3 --version`).

```bash
cd spy-levels
python3 -m app.server
```

Open http://localhost:8000. The first data pull takes about 5 seconds. Stop it with Ctrl+C.

To turn on the password or alerts locally:

```bash
APP_PASSWORD=choose-a-password NTFY_TOPIC=spy-levels-yourname-7f3k python3 -m app.server
```

---

## 2. Put it online

Both options below need the code in a GitHub repository first:

1. Create a new **private** repository on github.com (for example `spy-levels`).
2. Upload the contents of this folder (drag and drop on the repo page works), or from Terminal:
   ```bash
   cd spy-levels
   git init && git add . && git commit -m "SPY Levels"
   git branch -M main
   git remote add origin https://github.com/YOUR-NAME/spy-levels.git
   git push -u origin main
   ```

### Option A: Render (recommended)

1. Sign up at [render.com](https://render.com) and connect your GitHub account.
2. Click **New → Blueprint**, pick the `spy-levels` repo, and click **Apply**. Render reads `render.yaml`.
3. When asked, fill in:
   - `APP_PASSWORD`: the password for the site (anything you like; the username can be anything)
   - `NTFY_TOPIC`: your alert topic (see section 3), or leave blank for no alerts
4. Wait for the deploy to finish (2–3 minutes). Your app is at `https://spy-levels-XXXX.onrender.com`.

The blueprint uses Render's **Starter** plan (about $7/month). The free plan works too, but it goes to sleep after 15 minutes without visitors, and while it sleeps it stops refreshing and stops sending alerts. To try the free plan, change `plan: starter` to `plan: free` in `render.yaml`.

### Option B: Railway

1. Sign up at [railway.com](https://railway.com) and click **New Project → Deploy from GitHub repo**.
2. Pick the repo. Railway builds the `Dockerfile` automatically.
3. Under **Variables**, add `APP_PASSWORD` and (optional) `NTFY_TOPIC`.
4. Under **Settings → Networking**, click **Generate Domain** to get your URL.

Railway bills by usage. An app this small usually costs a few dollars a month.

### Other hosts

Anything that runs a Docker image or a Python process works (Fly.io, a VPS, a Raspberry Pi). The app listens on `$PORT` (default 8000) and has a health check at `/healthz`.

---

## 3. Phone alerts (free, optional)

Alerts use [ntfy](https://ntfy.sh), a free push service with no account needed.

1. Install the **ntfy** app (iPhone or Android).
2. Tap **+** and subscribe to a topic name that's hard to guess, for example `spy-levels-yourname-7f3k`. Anyone who knows the topic name can read it, so don't use something simple.
3. Set the same name as `NTFY_TOPIC` on your host.

You'll get:
- **SPY at [level]** when price comes within $0.25 of a level (once per level, at most every 20 minutes)
- **Regime changed** when SPY crosses the gamma flip

---

## 4. Settings

All optional. Set them as environment variables on your host.

| Variable | Default | What it does |
|---|---|---|
| `APP_PASSWORD` | off | Password-protects the whole site |
| `NTFY_TOPIC` | off | Topic for phone alerts |
| `NTFY_SERVER` | `https://ntfy.sh` | Use your own ntfy server |
| `PRICE_SECONDS` | `60` | Price refresh during market hours |
| `CHAIN_MINUTES` | `15` | Option chain refresh during market hours |
| `ALERT_DISTANCE` | `0.25` | How close (in $) counts as "at a level" |
| `STOP_DISTANCE` | `0.30` | Stop distance used in setups |
| `N_EXPIRIES` | `6` | How many upcoming expiries go into the gamma map |
| `DATA_PROVIDER` | `yahoo` | `yahoo` or `tradier` |
| `TRADIER_TOKEN` | — | Tradier API token (when `DATA_PROVIDER=tradier`) |
| `TRADIER_SANDBOX` | `0` | `1` = Tradier's free sandbox (15-minute delayed data) |

---

## 5. If the data stops updating

The page shows a red note when an update fails and keeps the last good data on screen. The app retries on its own (30 s, then 1, 2, and up to 5 minutes apart).

The usual cause is **Yahoo rate-limiting** the host's IP address. Yahoo is free and unofficial, and it sometimes blocks cloud servers. If that happens often:

1. Open a free brokerage or developer account at [Tradier](https://developer.tradier.com) and create an API token.
2. Set `DATA_PROVIDER=tradier` and `TRADIER_TOKEN=your-token` (add `TRADIER_SANDBOX=1` for a sandbox token).

The Tradier connector follows Tradier's documented API but was not tested against a live token, so check the page after switching.

Logs on Render (**Logs** tab) or Railway (**Deployments → View logs**) show each refresh, for example:
`2026-09-28 10:31:05 refreshed price 771.84 flip 769.40 Range day`

---

## How the numbers work

- **Gamma by strike**: for each option within 8% of the price (6 nearest expiries), gamma × open interest × 100 × price² × 1%, summed per strike. The sign assumes dealers are long calls and short puts. That's the common convention, not something anyone can observe directly.
- **Gamma flip**: the price where total dealer gamma crosses zero, found by recomputing total gamma at prices ±4% around spot.
- **Call walls / put walls**: the three strikes above price with the most call gamma, and the three below with the most put gamma.
- **Vol pace**: average volume of the last 5 minutes ÷ average volume of the session so far. Above about 2 means a break is more likely to hold.

Open interest updates overnight, so the walls are fixed during the day while price, VWAP, setups and alerts update every minute.

This is a research tool, not financial advice.

---

## Files

```
app/engine.py        data providers (Yahoo, Tradier) and all calculations
app/server.py        web server, refresh loop, alerts, password
app/static/index.html  the dashboard
render.yaml          Render blueprint
Dockerfile           for Railway and other Docker hosts
Procfile             for Heroku-style hosts
```

Run the calculation once without the server: `python3 -m app.engine out.json`
