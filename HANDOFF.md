# HANDOFF — WNT No-Fade Bot implementation

**Paste this whole file into the new chat as your first message, along with
the repo.**

---

## Your job

Get this repo running. Nothing else.

The strategy research is finished and closed. The infrastructure is written,
compiles, and passes an end-to-end simulation. Your role is deployment,
debugging, and the go-live checklist — not design.

**Treat the decisions in this document as settled.** They came out of a long
research process and a verification pass against Kalshi's live API
documentation. If you think one of them is wrong, do not quietly change it:
say so explicitly, explain the reasoning, and let Jovan decide. Changing a
setting because it "looks off" is the failure mode to avoid here.

If something in this document or the code turns out to be factually wrong
about the Kalshi API — a rejected payload, an endpoint that 404s, a field
that comes back differently — **fix it, tell Jovan clearly what was wrong,
and keep going.** That is expected. What is not expected is redesigning the
strategy, adding features, or "improving" the safety mechanisms.

**Send Jovan back to the architecture chat only if** something cannot be
solved within this repo's structure — for example, if Streamlit Cloud turns
out to be unworkable and the whole deployment target needs to change, or if
the fill data after two weeks contradicts the strategy's premise.

---

## Communication style

Jovan wants **plain, simple language**. Short sentences, everyday words,
concrete analogies. No jargon; if a technical term is unavoidable, define it
right there. This applies to technical and quantitative explanations too — he
wants to understand the mechanism, not just receive an answer.

He pushes back fast and specifically when something is wrong or overstated.
Take that seriously rather than folding; if you were right, show why.

---

## Do not touch these

| Setting | Value | Why |
|---|---|---|
| Limit price | 26¢ NO | Middle of a wide 15–38¢ plateau. Not a fitted spike. |
| Cancel time | 17:29 CT | Once a word is said, bots take YES to 99¢ in milliseconds. Any resting NO fills at a guaranteed loss. |
| Orders on **every** market | yes | This is a trap, not a forecast. You don't know which market drifts up to you, so you must already be resting in all of them. |
| `post_only` | true on a quiet book; off when NO is already ≤26¢ | Jovan override 2026-08-26: buy at 26¢ NO or cheaper as soon as the event opens. Fee / maker / taker does not matter. Never pay more than 26¢. |
| Server-side expiry | on | The order dies at 5:29 even if everything else fails. |
| Deterministic `client_order_id` | yes | The only thing stopping a restart from doubling the position. |

Two theories were **tested and rejected**. Do not build features around them:

- *"Most words aren't said, so buy NO everywhere."* True (62% resolve NO) but
  already priced in. A separate strategy targeting cheap-YES markets was built
  and tested; the edge was entirely lookahead bias and vanished when markets
  were classified by price at decision time.
- *"The crowd is overconfident about big news."* Partly true, not the driver.
  A hype-specific bias would concentrate the edge at high YES prices. It is
  flat across all price levels.

Out of scope for this app: the promo-listener, the live-audio monitor, other
Kalshi series. Separate edges, separate mechanisms, separate repos.

---

## What the architecture chat found (context you need)

**The reference repo `jovanpost/abcnws-live-bot` has no Kalshi auth.** The
earlier handoff said to reuse it. It doesn't exist — that app only makes
unauthenticated GETs with a User-Agent header. The RSA-PSS signing in
`wnt/kalshi.py` was written from scratch against current docs. What was
reused: the `st.secrets` → env config pattern, the `@st.cache_resource`
thread bootstrap, the Telegram approach, and the keep-alive workflow.

**The prototype's order payload was the deprecated shape.** Current API:

```
POST /portfolio/events/orders
{
  "ticker": "...", "client_order_id": "...",
  "side": "ask",              # ask == long NO. NOT "no".
  "count": "19",              # string, not int
  "price": "0.7400",          # YES leg! 26c NO == 74c YES
  "time_in_force": "good_till_canceled",
  "self_trade_prevention_type": "taker_at_cross",
  "post_only": true,
  "expiration_time": 1756247340
}
```

Everything is quoted from the YES leg. `ORDER_API=v1` in config falls back to
the old `{action, side, no_price}` shape if the new one misbehaves.

**Batch cancel is `DELETE /portfolio/events/orders/batched`** with
`{"orders": [{"order_id": "..."}]}`. Costs 2 tokens per order versus 10 for
a create.

**Base URL is `https://external-api.kalshi.com/trade-api/v2`.** The
prototype's `api.elections.kalshi.com` also works but is the secondary alias.
Demo exchange: `https://external-api.demo.kalshi.co` — set `USE_DEMO=true`.

**Rate limits (Basic tier): 200 read tokens/sec, 100 write.** An order costs
10, so ~10 orders/sec. Placing 25 takes about 2.5 seconds with the built-in
150ms spacing. Not a constraint. Upgrade to Advanced (free) after the first
API order.

**Fees.** Jovan's information was right and slightly incomplete. Resting
costs nothing, cancelling costs nothing. But Kalshi charges a maker fee on
some series when the resting order executes:
`ceil(0.0175 × contracts × P × (1−P))` ≈ **7¢ per fill** at these settings —
about 3% of the edge, not 40%. The 40% figure was about *taker* fees, and
`post_only` closes that. `scripts/verify_api.py` queries the series' actual
fee fields, and every fill records the fee it paid.

---

## Answers to Jovan's questions

**"Limit should be 25 markets per day."** Already set. Typical event is ~15
markets, so 25 gives headroom for an unusually large day without the count cap
biting. Note that `MAX_DAILY_COLLATERAL` ($150) also binds at 30 markets, so
25 is the effective limit either way.

**"Not sure where we are saving the data."** Nowhere, previously — that was a
real gap. The old app wrote JSON to Streamlit Cloud's local disk, which is
wiped on every restart and every git push. This app writes Postgres
(`DATABASE_URL`, Supabase free tier). If `DATABASE_URL` is missing it falls
back to local sqlite and the dashboard shows a red error, because that
fallback silently loses data on Streamlit Cloud.

Depth snapshots are ~25 markets × 1/minute × ~5 hours ≈ 7,500 rows a
broadcast day, roughly 5 MB a month. Supabase's 500 MB free tier holds well
over a year.

---

## Order of work

Do these in order. Each one gates the next.

1. **New GitHub repo**, push this code.
2. **Supabase project.** Use the **pooler** connection string (port 6543);
   the direct one is IPv6-only and Streamlit Cloud can't reach it.
3. **Kalshi API key.** Private key goes into secrets as PEM *text*
   (`KALSHI_PRIVATE_KEY_PEM`), not a file path — Streamlit Cloud has no
   filesystem you can upload to.
4. **New Telegram bot** via @BotFather. Do **not** reuse the listener bot's
   token; two apps polling `getUpdates` on one token steal each other's
   messages. Same chat ID is fine.
5. `python -m scripts.verify_api` locally until it passes clean.
6. **Demo smoke test:** `USE_DEMO=true`, then
   `python -m scripts.smoke_order --contracts 1`. Real order, fake money.
   This is the step that catches payload errors — `DRY_RUN` never posts, so
   it structurally cannot.
7. **Production smoke test:** one contract at 5¢. Five cents at risk. Verify
   the order reads back as `outcome_side: no` and that the cancel works.
8. **Deploy to Streamlit Cloud** with `DRY_RUN=true`. Main file is
   `streamlit_app.py`.
9. **Add GitHub secrets and the three workflows.** Then trigger
   `hard-cancel` manually with a test order resting, and watch it die.
10. **Three dry-run days.** Check event detection latency, market list, and
    Telegram.
11. **Fund $250–350**, flip `DRY_RUN=false`, watch the first day end to end.

---

## Known rough edges to expect

- **Streamlit Cloud restarts** on every push and sleeps without traffic. The
  code handles this (deterministic IDs, stateless cancel, day recovery from
  the database), but don't push code during market hours.
- **GitHub Actions cron runs late**, routinely 10–15 minutes. That's why the
  cancel job starts early and the script sleeps to the exact second. Do not
  "simplify" this by moving the timing into cron.
- **The two hard-cancel schedules (22:00 and 23:00 UTC) are intentional.**
  17:29 CT is 22:29 UTC in summer and 23:29 UTC in winter. One job does the
  precise cancel, the other sweeps. Don't delete one.
- If the v2 order payload gets rejected, set `ORDER_API=v1` and re-run the
  smoke test before touching anything else.

---

## What has been tested, and what hasn't

**Tested** (fake exchange, simulated full day):
price conversion both directions · contract sizing · ticker date parsing ·
cancel deadline in both CST and CDT · deterministic order IDs · orderbook
depth metrics · storage round-trip · P/L arithmetic including fees ·
placement with a `post_only` rejection · **restart mid-day does not double
the position** · fill polling is idempotent · **cancel fires after a restart
that wiped in-memory state** · scaling verdict returns FREEZE when
P(NO\|filled) drops while fill rate looks fine.

**Not tested, because it needs the real exchange:**
the actual order payload being accepted · the exact field names Kalshi
returns on orders and fills · whether `expiration_time` is honoured · the
live orderbook JSON shape (the parser handles both known shapes) · Supabase
connectivity from Streamlit Cloud.

Those five are exactly what steps 5–7 above are for. Do them properly.

---

## First message back to Jovan

Confirm you've read this, list the four things you need from him (GitHub repo
name, Supabase URL, Kalshi key ID, Telegram token), and start at step 1. Don't
re-explain the strategy to him — he wrote it.
