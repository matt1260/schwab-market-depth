# Scalper Terminal — Setup Walkthrough

A multi-strategy options scalper that trades on **real-time Schwab Level-2 market data**
and executes on **Alpaca paper trading**. It runs several strategies side by side and
compares their P&L: **APEX**, **MA-cross X**, **Bull Pivot**, an **AI supervisor**, and
your own **manual** trades — plus scanners for premium-selling (0DTE credit spreads
ranked by probability of expiring OTM, and a 30–45 DTE IV-rank spread scanner).

> **Everything is PAPER (fake money) by default.** Real-money "live" mode is optional,
> requires separate live keys, and must be turned on deliberately in the app. You can do
> the entire setup and testing without ever risking real money.

This guide assumes no prior setup. Follow it top to bottom.

---

## Step 0 — Check your tools

You need **Python 3.11+** and **Node.js 18+**. Check what you have:

```bash
python3 --version     # need 3.11 or higher
node --version        # need v18 or higher
```

If either is missing or too old:
- **Python** → https://www.python.org/downloads/ (macOS/Windows installer)
- **Node** → https://nodejs.org (download the "LTS" version)

---

## Step 1 — Get your API keys

You need two free accounts (a third is optional). Do this first — the Schwab one can
take a day or two to approve.

### a) Schwab developer app  *(market data — required)*
1. Go to https://developer.schwab.com and sign in with a Schwab brokerage login.
2. Create an app. For the product, choose the **market data / accounts** APIs.
3. Set the app's **Callback URL** to exactly:  `https://127.0.0.1`
4. When approved (status "Ready For Use"), copy the **App Key** and **App Secret**.

### b) Alpaca paper account  *(order execution — required)*
1. Go to https://app.alpaca.markets and sign up (free).
2. Toggle to **Paper Trading** (top-left switch).
3. Under **API Keys**, generate a key — copy the **Key** and **Secret**.

### c) Anthropic API key  *(optional — only for the AI strategy)*
- https://console.anthropic.com → API Keys → create one. Skip this if you don't want
  the AI strategy; everything else works without it.

Keep these keys handy for Step 3.

---

## Step 2 — Install the program

Open a terminal in the unzipped `scalper-terminal` folder (the one with `api.py` in it),
then:

```bash
# 1) Create + activate a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate            # Windows (PowerShell): .venv\Scripts\Activate.ps1

# 2) Install the backend dependencies
pip install -r requirements.txt

# 3) Install the frontend dependencies
cd frontend
npm install
cd ..
```

**Checkpoint:** `pip install` finishes without red errors, and `npm install` creates a
`frontend/node_modules` folder. (First `npm install` can take a minute or two.)

---

## Step 3 — Add your keys

Copy the template and fill in your own keys from Step 1:

```bash
cp .env.example .env
```

Open the new `.env` file in any text editor and paste your keys in after the `=` signs:

```
SCHWAB_APP_KEY=your_schwab_app_key
SCHWAB_APP_SECRET=your_schwab_app_secret
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_API_SECRET=your_alpaca_paper_secret
ALPACA_PAPER=true
# leave the live + anthropic lines blank unless you have them
```

> Never share your `.env` — it contains your private keys. It stays only on your machine.

---

## Step 4 — Authorize Schwab (browser login)

Schwab uses a login that must be refreshed **once now, then about every 7 days**. Run:

```bash
source .venv/bin/activate     # if not already active
python reauth.py
```

What happens:
1. A **browser window opens** to Schwab — log in and approve access.
2. You'll be redirected to a `https://127.0.0.1/?code=…` page. **It will look like an
   error page ("can't reach this site") — that's expected and fine.**
3. **Copy the entire URL** from the browser's address bar.
4. **Paste it back into the terminal** at the `paste the address bar url here:` prompt
   and press Enter. Do this **quickly** — the code expires in ~30 seconds.

**Checkpoint:** the script prints `Re-auth OK` with a live `$VIX` price. If it says the
code was invalid, just run `python reauth.py` again and paste faster.

---

## Step 5 — Run it

You need **two terminals** running at once — the backend and the frontend.

**Terminal 1 — backend (data + trading engine):**
```bash
source .venv/bin/activate
python api.py
```
Leave it running. It listens on port **8080**.

**Terminal 2 — frontend (the web UI):**
```bash
cd frontend
npm run dev
```
Leave it running too. It serves on port **8081**.

**Now open your browser to:**  http://localhost:8081/scalper

---

## Step 6 — What you should see

At the top, a **status rail**: `● Feed Live` (green dot), `Paper`, entry state, and the
day's P&L. Below it, tabs: **Scalper · Spreads · Scanner**.

On the **Scalper** tab:
- **Control toolbar** — an `Engine On/Off` switch, Autonomous/Confirm mode, and the
  paper/live toggle.
- **Trading cockpits** (one per symbol, SPY & QQQ) — a live chart with **Buy Call /
  Buy Put** buttons and a **Close** button right beside it. These are *your* manual
  trades, tracked as `MANUAL`.
- **Who's trading** — pick `AI decides`, `Run all (A/B)` (compare every strategy), or a
  single strategy. Each has an ⓘ icon explaining it.
- **Risk & timing** — size per trade, max concurrent, kill switch, wait-after-open.
- **Trades table** — every trade, labeled by strategy, with per-strategy P&L and filters.

On the **Scanner** tab: the **0DTE credit-spread scanner** (ranked by probability of
expiring OTM) and the **30–45 DTE IV-rank** spread scanner.

### To take your first trade
1. Make sure the top rail shows `Feed Live` and it's **market hours** (see below).
2. In a cockpit, click **Buy Call** or **Buy Put**. It buys a near-the-money option at
   the current price (paper) and shows it in the cockpit with a **Close** button.
3. Click **Close** to exit, or let the auto exit-logic manage it.

### To run the strategy comparison
Set **Who's trading → Run all (A/B)** and flip the **Engine** on. The strategies will
trade on their own during market hours; watch the trades table to compare them.

---

## Important notes

- **Market hours matter.** Live quotes and 0DTE option chains only update during U.S.
  market hours (**9:30 AM – 4:00 PM ET, weekdays**). Outside that the feed shows the last
  close and nothing trades — that's normal, not a bug.
- **Paper vs live.** The rail shows `Paper` by default. Going live needs Alpaca *live*
  keys in `.env` **and** an explicit confirmation in the UI; it never auto-arms and
  resets to paper on restart. Start on paper.
- **The 7-day re-auth.** When the feed goes down and the rail shows `Feed Down`, your
  Schwab login likely expired — stop the backend, run `python reauth.py` again, restart.
- **Auto-restart helper (optional).** `feed_watchdog.sh` restarts the backend if the
  Schwab stream stalls. Edit the `DIR` path at the top of the file to your folder, then
  run `./feed_watchdog.sh &` next to the backend.
- **Your data** lives in `data/scalper.db` (created on first run) — trade history.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Page says **"Scalper not initialized"** or rail shows **Feed Down** | Schwab token expired or not authorized — stop the backend, run `python reauth.py`, restart `python api.py`. |
| **Nothing loads / UI can't reach data** | Make sure `python api.py` (Terminal 1) is running on port 8080. |
| **`python api.py` errors on import** | Re-run `pip install -r requirements.txt` with the venv active (`source .venv/bin/activate`). |
| **AI strategy never trades** | Expected without an `ANTHROPIC_API_KEY` in `.env`. |
| **Scanner shows no 0DTE spreads** | Only populates during market hours on a day with same-day expiries (SPY/QQQ/IWM/DIA). |
| **Feed frozen / prices not moving** | Could be after-hours, or the stream stalled — restart the backend (or use the watchdog). |

---

## Quick reference

| | |
|---|---|
| Backend | `python api.py` → port **8080** |
| Frontend | `cd frontend && npm run dev` → port **8081** |
| Open in browser | **http://localhost:8081/scalper** |
| Re-authorize Schwab | `python reauth.py` (first run + every ~7 days) |
| Your keys | `.env` (copied from `.env.example`) |
