# Stonks 📈

A starter **crypto trading bot + web dashboard** for
[Alpaca](https://alpaca.markets/) **paper trading**, designed to run on an
Oracle Cloud (or any) Ubuntu VM and be viewed remotely from another network.

- **Bot** — polls crypto prices, runs your strategy, places paper orders, logs
  everything to SQLite. Ships with a clean *strategy skeleton* so the whole
  pipeline works end-to-end; you just fill in the buy/sell rules.
- **Dashboard** — a FastAPI web page showing account equity, positions, P/L,
  recent signals & trades, and an equity chart. Behind an nginx password login.
- **Deploy** — `systemd` services (auto-restart, survive reboots) + nginx
  reverse proxy + a one-shot VM setup script.

> ⚠️ Trades **paper money** by default (`ALPACA_PAPER=true`). Nothing touches
> real funds unless you deliberately switch to live keys.

---

## Architecture

```
        Alpaca API  (paper account + crypto market data)
              ▲                         ▲
              │ orders / account        │ price bars
              │                         │
        ┌─────┴──────┐            ┌─────┴───────────┐
        │  src/bot   │  writes    │ src/dashboard   │  reads
        │ (strategy) ├──────────► │  (FastAPI)      │ ◄─── you (browser)
        └────────────┘  SQLite    └─────────────────┘      via nginx + login
                        data/stonks.db
```

The bot and dashboard are independent processes that communicate only through
the SQLite database, so either can restart without affecting the other.

## Project layout

```
Stonks/
├── src/
│   ├── config.py            # all settings, read from .env
│   ├── database.py          # SQLite storage (shared bot ↔ dashboard)
│   ├── alpaca_client.py     # wraps the Alpaca SDK (account, data, orders)
│   ├── strategy.py          # >>> YOUR TRADING LOGIC GOES HERE <<<
│   ├── bot.py               # the main loop:  python -m src.bot
│   └── dashboard/
│       ├── app.py           # FastAPI app + JSON API
│       └── templates/index.html
├── deploy/
│   ├── setup_vm.sh          # one-shot VM provisioning
│   ├── stonks-bot.service   # systemd unit for the bot
│   ├── stonks-dashboard.service
│   └── nginx-stonks.conf    # reverse proxy + Basic Auth login
├── docs/ORACLE_SETUP.md     # full step-by-step Oracle Cloud guide
├── .env.example             # copy to .env and fill in
└── requirements.txt
```

## Quick start (on the Oracle VM)

Full walkthrough — including creating the VM and opening the Oracle Cloud
firewall — is in **[docs/ORACLE_SETUP.md](docs/ORACLE_SETUP.md)**.

This is a **public** repo, so the VM can pull it directly with no GitHub login.
Once SSH'd into the VM:

```bash
git clone https://github.com/ddtechapp2026/claude.git Stonks && cd Stonks
cp .env.example .env && nano .env      # add your Alpaca PAPER keys
./deploy/setup_vm.sh                    # installs & starts everything
```

Prefer a single file? Grab just the self-contained installer, which recreates
the whole project without cloning:

```bash
curl -fsSL https://raw.githubusercontent.com/ddtechapp2026/claude/main/install_stonks.sh -o install_stonks.sh
bash install_stonks.sh && cd ~/Stonks
```

Then open port 80 in the **Oracle Cloud console** (VCN Security List ingress —
this is the step everyone forgets; see docs step 5) and browse to
`http://YOUR_VM_PUBLIC_IP/`.

## Run it locally first (recommended)

Before touching the VM, you can run the whole thing on your own machine:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # add your Alpaca PAPER keys

# terminal 1 — the bot
python -m src.bot

# terminal 2 — the dashboard
uvicorn src.dashboard.app:app --reload --port 8000
# open http://localhost:8000
```

## Writing your strategy

Open `src/strategy.py` and implement the `decide()` function. It receives a
list of recent close prices and whether you currently hold a position, and
returns `Decision(BUY|SELL|HOLD, "reason")`:

```python
def decide(closes, *, has_position):
    fast = _sma(closes, 10)
    slow = _sma(closes, 30)
    if fast is None or slow is None:
        return Decision(HOLD, "warming up")
    if fast > slow and not has_position:
        return Decision(BUY,  f"fast {fast:.0f} > slow {slow:.0f}")
    if fast < slow and has_position:
        return Decision(SELL, f"fast {fast:.0f} < slow {slow:.0f}")
    return Decision(HOLD, "no cross")
```

That exact example is included, commented out, in the file. The bot enforces
the safety contract: a `BUY` is ignored if you already hold the symbol, and a
`SELL` is ignored if you don't — so you can return signals freely.

Tune behaviour in `.env`: `TRADE_SYMBOL`, `BAR_TIMEFRAME`, `LOOKBACK_HOURS`,
`ORDER_NOTIONAL_USD`, `POLL_INTERVAL_SECONDS`, and the `DRY_RUN` safety switch.

## Configuration reference

| Variable | Meaning | Default |
|---|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca **paper** API keys | — |
| `ALPACA_PAPER` | `true` = paper trading (keep this) | `true` |
| `TRADE_SYMBOL` | Crypto pair, Alpaca format | `BTC/USD` |
| `POLL_INTERVAL_SECONDS` | Seconds between decision cycles | `60` |
| `ORDER_NOTIONAL_USD` | Dollar size of each buy | `100` |
| `BAR_TIMEFRAME` | `1Min`/`5Min`/`15Min`/`1Hour`/`1Day` | `15Min` |
| `LOOKBACK_HOURS` | History pulled for indicators | `48` |
| `DRY_RUN` | Log trades but don't send them | `false` |
| `DATABASE_PATH` | SQLite file | `data/stonks.db` |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Dashboard bind address | `127.0.0.1:8000` |

## Security notes

- **Never commit `.env`** — it holds your keys and is git-ignored.
- The dashboard binds to `127.0.0.1` only; nginx is the sole exposed service
  and requires a password.
- Plain HTTP transmits that password reversibly. Add HTTPS (Let's Encrypt via
  certbot, or a Cloudflare Tunnel) — see docs step 8.
- Restrict the Oracle ingress rule to your remote computer's IP if it's stable,
  instead of `0.0.0.0/0`.
- Keep `ALPACA_PAPER=true` until you fully trust your strategy. Going live
  trades real money and is entirely at your own risk. This is not financial
  advice.
