# Stonks 📈

A **10-bot crypto trading lab + web dashboard** for
[Alpaca](https://alpaca.markets/) market data, designed to run on an Oracle
Cloud (or any) Ubuntu VM and be controlled remotely from another network.

- **10 independent bots**, each with its own strategy and its own **virtual
  wallet** (own cash, position, P/L, stop-loss). They trade *simulated* money
  priced off **real live Alpaca crypto prices**, so bots never interfere with
  each other and you can compare strategies head-to-head.
- **Tabbed dashboard** — one tab per bot: equity chart, wallet stats, trade &
  signal history, and full controls. Behind an nginx password login.
- **Plain-English strategies** — describe how a bot should trade in normal words
  ("buy when RSI drops below 30, sell when up 3% or down 2%, use 40% of cash")
  and the AI converts it into safe trading rules.
- **AI Control switch (per bot)** — **ON:** the bot follows its own strategy and
  actively manages the open trade (trailing stop + auto break-even, and tunes
  position size as it learns), with your stop-loss/take-profit as **hard
  guardrails it can only tighten within, never exceed**. **OFF:** your controls
  are the literal answer — fixed stop/take-profit, no dynamic changes, no
  learning.
- **Per-bot controls** — on/off, the crypto to trade (**any** pair, or **Auto**
  to let the bot scan the whole universe and pick what fires), allocated cash,
  biggest trade, stop-loss, take-profit, and a run-until deadline.
- **Full decision journal** — every cycle records the symbol, signal, reasoning,
  and the indicator snapshot behind it, per bot. One-click **Export data**
  downloads the whole journal + trades as JSON to learn from.
- **Fees & taxes accounted for** — a per-bot P&L breakdown shows gross trading
  P/L → minus fees (charged on every buy/sell) → minus estimated taxes → net.
  Fee % and tax % are editable per bot (`FEE_PCT` / `TAX_PCT` defaults). The tax
  line is a rough estimate, not tax advice.

> ⚠️ Every bot trades a **simulated wallet** — no real orders are ever sent.
> This is a research/paper sandbox. Not financial advice.

---

## Architecture

```
     Alpaca crypto market data (real prices, no key required)
                          │
                          ▼
             ┌───────────────────────────┐
             │        src/engine         │  runs every enabled bot each cycle:
             │  virtual wallet per bot   │  mark-to-market → risk checks →
             │  risk + strategy + learn  │  strategy spec → buy/sell (sim) →
             └───────────┬───────────────┘  learn from closed trades
                         │ SQLite (data/stonks.db)
             ┌───────────┴───────────────┐        OpenRouter (AI)
             │      src/dashboard        │◄──────  plain-English → rules,
             │  tabbed FastAPI UI + API  │         trade reviews
             └───────────┬───────────────┘
                         ▼
                    you (browser)  ── via nginx + password login
```

The engine and dashboard are separate processes sharing only the SQLite DB, so
either can restart independently. The dashboard writes config; the engine reads
it fresh every cycle, so on/off and control changes take effect immediately.

## Project layout

```
├── src/
│   ├── config.py           # settings from .env
│   ├── market.py           # live crypto prices (Alpaca), short-cached
│   ├── indicators.py       # SMA / EMA / RSI / pct-change / high / low
│   ├── strategy_engine.py  # SAFE interpreter for JSON strategy specs
│   ├── llm.py              # OpenRouter: English→rules + learning (rule fallback)
│   ├── database.py         # SQLite: bots, wallets, trades, equity, reviews
│   ├── seed.py             # creates the 10 starter bots
│   ├── engine.py           # the supervisor loop:  python -m src.engine
│   └── dashboard/
│       ├── app.py          # FastAPI UI + control API
│       └── templates/index.html
├── deploy/                 # systemd units, nginx (+Basic Auth), setup_vm.sh
├── docs/ORACLE_SETUP.md    # full Oracle Cloud walkthrough
├── install_stonks.sh       # whole project in one file (no-git install)
└── .env.example
```

## Quick start (on the Oracle VM)

Full walkthrough (VM creation + Oracle firewall) is in
**[docs/ORACLE_SETUP.md](docs/ORACLE_SETUP.md)**. This repo is **public**, so the
VM pulls it with no GitHub login:

```bash
git clone https://github.com/ddtechapp2026/claude.git Stonks && cd Stonks
cp .env.example .env && nano .env      # add your OpenRouter key (Alpaca keys optional)
./deploy/setup_vm.sh                    # installs & starts everything
```

Single-file alternative (no git):

```bash
curl -fsSL https://raw.githubusercontent.com/ddtechapp2026/claude/main/install_stonks.sh -o install_stonks.sh
bash install_stonks.sh && cd ~/Stonks
```

Then open port 80 in the **Oracle Cloud console** (VCN Security List ingress —
the step everyone forgets; see docs step 5) and browse to
`http://YOUR_VM_PUBLIC_IP/`.

## Run it locally first (recommended)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add OPENROUTER_API_KEY (optional but enables AI)

# terminal 1 — the engine (seeds 10 bots on first run, all OFF)
python -m src.engine

# terminal 2 — the dashboard
uvicorn src.dashboard.app:app --reload --port 8000
# open http://localhost:8000, pick a bot tab, turn it ON
```

## Using it

1. Each bot starts **OFF**. Open its tab and hit **Turn ON**.
2. **Set the strategy in plain English** in the "How this bot trades" box and
   click *Translate & save*. With an OpenRouter key the AI writes the rules;
   without one, a built-in parser handles common phrasings.
3. **Set the controls**: allocated cash, biggest trade, stop-loss %,
   take-profit %, a run-until deadline (blank = forever), and auto-learn on/off.
4. Watch the equity curve, trades, and the **AI learning log** (what it changed
   and why) fill in.

### How strategies are represented (safe by design)

Plain English is translated into a small **JSON rule spec** — never executable
code — that the engine interprets. Available terms: `price`, `pct_from_entry`,
`sma(N)`, `ema(N)`, `rsi(N)`, `pct_change(N)`, `high(N)`, `low(N)`, compared with
`< > <= >= ==` and combined with all/any. Anything the AI returns is validated
and rejected if it references unknown terms, so a bad translation can't run
arbitrary logic.

## Configuration reference

| Variable | Meaning | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Enables AI strategy translation + reviews | — |
| `OPENROUTER_FREE_ONLY` | Only call free (`:free`) models — never spend money | `true` |
| `OPENROUTER_MODEL` | OpenRouter model slug (`:free` added if free-only) | `deepseek/deepseek-chat-v3-0324:free` |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Optional; raise data rate limits | — |
| `TRADE_SYMBOL` | Default symbol for seeded bots | `BTC/USD` |
| `POLL_INTERVAL_SECONDS` | Seconds between engine cycles | `60` |
| `BAR_TIMEFRAME` | `1Min`/`5Min`/`15Min`/`1Hour`/`1Day` | `15Min` |
| `LOOKBACK_HOURS` | History pulled for indicators | `72` |
| `AI_REVIEW_MIN_TRADES` | Closed trades between learning reviews | `5` |
| `AI_MIN_WINRATE` | Reviews tighten risk below this win-rate | `0.45` |
| `DATABASE_PATH` | SQLite file | `data/stonks.db` |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Dashboard bind address | `127.0.0.1:8000` |

## Security notes

- **Never commit `.env`** — it holds your OpenRouter key and is git-ignored.
- The dashboard binds to `127.0.0.1` only; nginx is the sole exposed service and
  requires a password. Its controls (on/off, config) are protected by that same
  login, so keep it strong.
- Plain HTTP transmits the password reversibly — add HTTPS (certbot or a
  Cloudflare Tunnel), see docs step 8.
- Everything is simulated; there is no live-trading path in this code. Adding one
  would be entirely at your own risk. Not financial advice.
