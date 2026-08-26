# WNT No-Fade Bot

Buys **NO at 26¢ or cheaper** on every market in the daily Kalshi
`KXWORLDNEWSMENTION` event as soon as it opens, then makes certain nothing
is still resting at **5:29 PM CT**, one minute before ABC World News Tonight
goes to air.

You are not predicting the news. You are the seller at the top of the daily
drift. A resting offer only transacts when the price wanders up to it, and the
daily high overshoots true probability by roughly 15¢ at every price level.

---

## What changed from the prototype

The single-file prototype was written from memory, without access to the live
API. Four things in it were wrong, and two of them mattered a lot.

**1. The order payload was the deprecated shape.** Kalshi's current order
endpoint is `POST /portfolio/events/orders`, and it quotes everything from the
YES leg. There is no "buy no" any more:

| What you mean | What you send |
|---|---|
| buy NO at 26¢ | `side: "ask"`, `price: "0.7400"` |
| 19 contracts | `count: "19"` (a **string**) |

Prices and counts are fixed-point strings, not integers. `time_in_force` and
`self_trade_prevention_type` are now required fields.

**2. `post_only` on a quiet book, take immediately on a cheap book.**
If NO is not yet 26¢, we rest a maker order at 26¢ (`post_only: true`).
If the market already opened with YES above 74¢ (NO already 26¢ or cheaper),
we turn `post_only` off for that one order and buy immediately. We never
pay more than 26¢ for NO. Maker vs taker fee does not matter here — missing
the market does.

**3. `expiration_time` exists.** Every order carries a server-side expiry set
to 5:29 PM CT. Kalshi kills the order itself. This is the strongest possible
version of the cancel, because it survives this app dying, your host dying,
and your internet dying.

**4. The reference repo has no Kalshi auth to reuse.** `abcnws-live-bot`
makes only unauthenticated GETs with a User-Agent header. The RSA-PSS signing
here is written from scratch against the current docs. What *was* worth
reusing: the `st.secrets` → env config pattern, the `@st.cache_resource`
thread bootstrap, the Telegram helper, and the keep-alive workflow.

**And the answer to "where are we saving the data":** the old app wrote
`mentions_seen.json` to local disk, and Streamlit Cloud wipes that disk on
every restart and every push. Nothing survived. This app uses Postgres.

---

## Architecture

```
GitHub Actions                     Streamlit Cloud                  Kalshi
--------------                     ---------------                  ------
keepalive     --- every 10min ---> streamlit_app.py
                                     |
                                     +-- strategy thread -----------> place / cancel
                                     +-- depth thread --------------> orderbooks
                                     +-- telegram listener            (read only)
                                     |
hard-cancel   --------------------- | ------------------------------> CANCEL ALL
  (17:29 CT, independent)           |                                 (safety net)
                                    v
nightly-settle --------------> Postgres (Supabase)
```

Four independent things have to fail before an order is left resting into the
broadcast:

1. the server-side expiry on the order itself
2. the in-app scheduler at 5:29
3. the GitHub Actions cancel at 5:29
4. you, on your phone, sending `/cancelnow`

---

## Setup

### 1. Kalshi API key

Kalshi → Account & security → API Keys → Create Key. Save the key ID and the
private key file; the private key is shown once and never again.

### 2. Database (Supabase free tier is fine)

Create a project, then Settings → Database → Connection string. **Use the
connection pooler string (port 6543), not the direct one** — direct Postgres
connections are IPv6-only and Streamlit Cloud cannot reach them.

Tables are created automatically on first boot. `sql/schema.sql` is there if
you would rather create them by hand.

### 3. Telegram

Make a **new** bot with @BotFather rather than reusing the listener bot's
token. Two apps polling `getUpdates` on the same token will steal each other's
messages. The chat ID can be the same.

### 4. Secrets

Locally, copy `.env.example` to `.env`. On Streamlit Cloud, put the same keys
in the app's Secrets box as TOML. The private key has to go in as **PEM text**,
because there is no filesystem to upload a file to:

```toml
KALSHI_KEY_ID = "your-key-id"
KALSHI_PRIVATE_KEY_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEow...
-----END RSA PRIVATE KEY-----"""
DATABASE_URL = "postgresql://postgres.xxx:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
TELEGRAM_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
DRY_RUN = "true"
```

For GitHub Actions, add the same values as repository **Secrets**, and set
`APP_URL` as a repository **Variable** pointing at your Streamlit app.

### 5. Run it

```bash
pip install -r requirements.txt
python -m scripts.verify_api      # read-only pre-flight
streamlit run streamlit_app.py
```

---

## Go-live checklist

Work down this list. Do not skip ahead.

- [ ] **`python -m scripts.verify_api` passes.** Auth works, balance reads,
      the series fee structure prints, today's event is visible.
- [ ] **Smoke test on the demo exchange.** Set `USE_DEMO=true`, then
      `python -m scripts.smoke_order --contracts 1`. This places a real order
      with fake money. `DRY_RUN` never posts anything, so it is the only way
      to catch a payload error before real money is involved.
- [ ] **Smoke test on production with one contract.** `USE_DEMO=false`,
      `python -m scripts.smoke_order --contracts 1 --no-price 5`. Five cents
      at risk. Confirm the order comes back as `outcome_side: no`, that the
      expiry field is set, and that the cancel really removes it.
- [ ] **Three full days of `DRY_RUN=true`.** You are checking that the event
      is detected within a minute, that the market list looks right, and that
      Telegram fires.
- [ ] **Trigger the hard-cancel workflow by hand** (Actions → Hard cancel →
      Run workflow, immediate = true). Confirm it reports back on Telegram.
      Do this while you have a test order resting so you see it actually work.
- [ ] **Confirm depth rows are landing** in the dashboard's Depth tab.
- [ ] **Fund the account.** See the collateral note below.
- [ ] Flip `DRY_RUN=false`. Watch the first live day from start to finish.
- [ ] The day after your first live order, call Kalshi's *Upgrade Account API
      Usage Level* endpoint. It is free once one of your last 100 orders came
      from the API, and it triples your write budget.

---

## The collateral number people get wrong

On Kalshi a **resting** buy order holds its full cost as collateral, not just
the fills. So your bankroll has to cover every order you put up, not the
position you expect to end with.

| Size per market | Contracts | Per market | 25 markets resting |
|---|---|---|---|
| $5 | 19 | $4.94 | **$123.50** |
| $20 | 76 | $19.76 | **$494.00** |
| $50 | 192 | $49.92 | **$1,248.00** |

The backtest's "peak collateral ~$54/day" describes filled positions. The
number that has to be in the account is the right-hand column. $250–350 is
right for the $5 rung; the $20 rung needs about $600 and the $50 rung about
$1,400. `MAX_DAILY_COLLATERAL` is the hard stop, and the bot trims markets
rather than exceed it.

---

## Fees

You were right that a resting order costs nothing while it rests, and that
cancelling costs nothing. Both are in Kalshi's published schedule.

The part worth being precise about: Kalshi *does* charge a maker fee on some
series, collected when the resting order eventually executes. Where it
applies the formula is `ceil(0.0175 × contracts × P × (1−P))`, which at 26¢
and 19 contracts is about **$0.064, rounded up to 7¢ per fill**. At roughly
7 fills a night that is about 50¢ a day against a $16 backtest mean — call it
3% of the edge, not 40%.

The 40% figure was about **taker** fees, which are four times the multiplier.
`post_only: true` makes a taker fill structurally impossible, so that risk is
closed rather than estimated.

Don't settle this from a PDF. Every fill records its actual fee, and the
dashboard counts how many fills paid one. If that counter is zero after two
weeks, the answer is zero.

---

## The two numbers to watch

**Fill rate** — backtest says ~50% at 26¢. If live comes in near 30%, every
dollar projection scales down by the same ratio. Rejected orders are excluded
from the denominator, because an order that never rested was never part of
the experiment.

**P(NO | filled)** — backtest says ~41%. This is the capacity meter and it is
the one that will catch you out. Going too big does **not** look like "I can't
get filled." It looks like "I'm filling fine and losing more often," because
the size that finds you is the size that knows something. The dashboard
returns `FREEZE` if this drops 8 points below baseline, even when the fill
rate looks healthy.

---

## Telegram commands

| Command | What it does |
|---|---|
| `/status` | what the bot thinks is happening |
| `/today` | today's orders and fill rate |
| `/cancelnow` | cancel every resting order immediately |
| `/pause` | stop placing new orders; survives a restart |
| `/resume` | undo `/pause` |
| `/balance` | Kalshi cash |
| `/stats` | running fill rate, P(NO\|filled), P/L, scaling verdict |

`/pause` does not touch orders that are already resting. Use `/cancelnow` for
those.

---

## Known limits of this setup

**Streamlit Community Cloud is not a great host for a money-moving bot.** It
sleeps, it restarts on every push, and its disk is ephemeral. This app is
built to survive that — deterministic order IDs so a restart cannot double a
position, a stateless cancel that reads from the exchange rather than memory,
recovery of an unfinished day from the database, and two cancel mechanisms
outside the app entirely. It is safe, not elegant. Once the strategy is
earning, move the worker to Render or Fly and leave Streamlit as the
dashboard.

**GitHub Actions cron is not punctual.** Scheduled runs are regularly 10–15
minutes late. That is why the cancel job starts early and the *script* sleeps
to the exact second rather than trusting cron to fire at 5:29.

**Depth collection is read-only.** If it breaks you lose research data, never
money.
