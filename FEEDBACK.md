# Feedback on the CoinMarketCap API

Written for the CMC product team, who asked for exactly this. Every item below was hit while
building [Forwarding Address](README.md) against the live API between 2026-09-18 and
2026-09-19 — nothing is speculative, each finding carries the date it was observed and the
receipt that produced it, and every call was keyless.

The headline: **`/v1/dex/liquidity-change/list` with `maker=` is the best-kept secret in the
DEX catalogue.** It is a server-side wallet join across every pool of a token, it works with no
key, and it is what this whole project is built on. Eleven of the twelve items below are about
making it easier to trust.

---

## 1. A liquidity-change row can carry a USD value of 10⁴² — and nothing marks it

**Observed 2026-09-19 · CAKE on BSC · `/v1/dex/liquidity-change/list` · severity: high**

Thirteen rows for the real CAKE contract (`0x0e09fabb…ce82`) on an unlabeled venue (factory
`0xe56c0a29…fa97`, pair reported as `Cake/ETH`) carry `tu: -1e+42` and `a0: -1e+42` — a
removal of a trillion trillion trillion CAKE, worth a trillion trillion trillion dollars. They
are returned like any other row, pass `minVolume`, and in a sweep of 11 tokens they out-ranked
every real removal on the watchlist ([`docs/proof/seed_sweep.json`](docs/proof/seed_sweep.json),
2026-09-18T23:16Z run; the rows are in the CAKE response bodies).

**Why it matters:** a consumer ranking removals by `tu` — the obvious thing to do with this
endpoint — gets a scam pool's overflow at the top of every list. We added a `SANE_USD` bound
(a single event above $10B is refused as an artefact); the API could **cap `tu` at the token's
own liquidity, drop rows whose token amount exceeds the supply, or flag the venue** the way
`en`'s absence already hints at it.

---

## 2. `startTime` is plan-gated on the keyless surface, `endTime` is not

**Observed 2026-09-18 · `/v1/dex/liquidity-change/list` · severity: high for keyless users**

`startTime=<ms>` returns **HTTP 403, error 1013** — *"Your API Key subscription plan doesn't
support historical data access for Dex API"* — while `endTime` alone returns 200. A time
window therefore cannot be requested; it has to be **walked with the `lastId` cursor and cut
client-side on `ts`**, which is what `follow_maker()` does. That costs pages instead of one
call, and on a busy wallet it is the difference between one request and ten.

**Why it matters:** a ±6 h window is the natural shape of "did this wallet come back?", and
`endTime` proves the server can filter on time. **Allowing a `startTime` that is within, say,
7 days of now on the anonymous tier** would keep historical access gated while making the
common case one call.

---

## 3. The anonymous tier reports its rate limit two ways, with no `Retry-After`

**Observed 2026-09-18/19 · every endpoint · severity: high**

Under load the same condition came back as

```
HTTP 429  error_code 1022  "You've reached the limit for anonymous access…"
HTTP 429  error_code 1011  "You've hit an IP rate limit."
```

(both seen repeatedly across the receipted runs of 2026-09-19 — a call that backed off and
then answered carries an `attempts` field in `docs/proof/*.json`), and a sibling project on
the same tier recorded it as **HTTP 500 "The system is busy"** two weeks earlier. No
response carries `Retry-After` or any `X-RateLimit-*` header, and a successful keyless
response reports `credit_count: 1` — a charge against an account that does not exist.

**Why it matters:** a keyless client cannot budget for a limit it cannot see, and cannot tell
an exhausted quota from an outage. We back off 15/30/60 s on any 429 or 5xx and record the
retries in the receipt (`attempts`), but **one status code, one error code, a `Retry-After`,
and a published anonymous quota** would make that guesswork unnecessary.

---

## 4. `/v1/dex/token-liquidity/query` returns 400 on every parameter set we could think of

**Observed 2026-09-18 · severity: medium**

Eight variants — `interval` as `1h`, `h1`, `1H`, `hour`, `1d`, `d1`, with and without `limit`,
`to` (seconds and milliseconds), `needLatest` — all return **HTTP 400 "Parameter error"**
keyless. The documentation page does not say which values it wants. We could not use the
endpoint at all and confirm pool depth through `/v4/dex/pairs/quotes/latest` instead.

**Why it matters:** this is the one endpoint that would give a *history* of a token's
liquidity — exactly the context a removal alert needs. **Document the accepted `interval`
values on the endpoint page, or return them in the 400 body.**

---

## 5. `en` and `eid` are absent on unlabeled venues — and the row does not say so

**Observed 2026-09-18 · PEPE on Ethereum · severity: medium**

75 of 100 PEPE rows carried no `en` and no `eid`; only the factory `f` identified the venue.
A consumer keying on `en` silently merges every unlabeled DEX into one bucket (or crashes on
the missing key). We fall back to `f` for identity and print "unlabeled DEX (factory 0x…)".

**Why it matters:** `f` is always present and is the better identity key anyway. **Document
that `en`/`eid` are optional, and consider always emitting `eid: 0` / `en: null` so the shape
is stable.**

---

## 6. Rows carry the venue and the pair, never the pool address

**Observed 2026-09-18 · severity: medium**

A liquidity-change row identifies its pool as `(f, t0a, t1a)`. On Uniswap v3 the same pair
exists at three fee tiers — UNI has three v3 UNI/WETH pools — and the row cannot tell them
apart, while `/v1/dex/token/pools` returns per-pool `addr`. We state this as a limit rather
than guess. **Adding the pool address (`pa`) to the row** would make the two endpoints join
exactly and turn "same pool identity" into "same pool".

---

## 7. Every numeric-looking field is a string except the ones that are not

**Observed 2026-09-18 · severity: low, but it bites every first integration**

On `liquidity-change/list`: `ts`, `h`, `lgid`, `txId` are **strings**; `tu`, `a0`, `a1` are
floats; `eid` is an int. On `token/pools`: `liqUsd` and `pubAt` are strings. On `search`:
`cid` is an int but `pltId` is an int and `pt` is a string. The documentation shows `ts` as an
integer. We `int()`/`float()` at the edge, once, in `ts_ms()` and `usd()`.

**Why it matters:** `sorted(rows, key=lambda r: r["ts"])` on strings sorts lexically and is
wrong the moment a timestamp gains a digit. **Pick one representation per type and document it.**

---

## 8. `tu` and `a0` are signed on removals, and nothing says so

**Observed 2026-09-18 · severity: low**

`remove` rows carry negative `tu`, `a0`, `a1`; `minVolume` matches on the magnitude. This is
sensible but undocumented, and `type=0`/`type=1` (add/remove) is an undocumented mapping we
found by trying it. **State the sign convention and the `type` values on the endpoint page.**

---

## 9. Just-in-time liquidity is not flagged

**Observed 2026-09-18 · PEPE, AAVE, DAI, CAKE · severity: medium**

85 of 100 PEPE rows, 28 AAVE, 27 DAI and 31 CAKE transactions in 100 rows were
same-transaction add+remove pairs — a searcher supplying liquidity for one swap and pulling it
in the same block. Every one of them is a "removal" to a naive consumer, and a $369,569 JIT
"removal" is exactly the kind of row an alert bot fires on. We detect them by grouping on
`txn`; the API already has both rows and could mark them. **A `jit: true` flag, or a
`type=2` filter that excludes them, would remove the single largest source of false alarms
this endpoint produces.**

---

## 10. `/v1/dex/search` by address returns forks with `cid` missing

**Observed 2026-09-19 · `q=0x1f9840…f984` (UNI) · severity: low**

The address search returned four rows: Ethereum UNI (`cid: 7083`), and the same address on
PulseChain and EthereumPoW with **no `cid` key at all**. We take the row whose platform matches
and require a `cid`; a consumer that takes the first row with a `cid` is fine, one that takes
`rows[0]` is fine today, and one that `KeyError`s on the fork rows is not. **Emit `cid: null`
rather than omitting the key, or leave forks out of an exact-address search.**

---

## 11. `token/pools` is capped at 20 and the cap is silent

**Observed 2026-09-19 · UNI on Ethereum · severity: low**

`size=20` returns 20 pools; the removal that turned out to be the largest on UNI in the last
300 rows (a $21.3M Ring Exchange UNI/WBTC position) came from a pool that is **not in the
list** at all, so its share of pool cannot be computed. **Document the cap, and accept a
`cursor` or a larger `size`.**

---

## 12. Keyless responses report `credit_count: 1`

**Observed 2026-09-18/19 · every keyless call · severity: cosmetic, but it misleads**

Every keyless envelope says `credit_count: 1`. Nothing is billed — there is no account — but a
developer trying to work out whether the surface is free reads it as "metered". We print
"credit_count 1 · 0 billed" in the trace to explain it. **Report 0, or omit the field, on the
anonymous tier.**

---

## What worked, so this is not only a list of complaints

- **`maker=` on `liquidity-change/list`** is the reason this project exists: one keyless call
  returned every event by one wallet across three Uniswap versions, cursor-exhausted, with the
  wallet's own address in `m` on every row.
- **`/v2/cryptocurrency/info` keyless** with a full `contract_address[]` registry (13 chains
  for UNI) is the right shape for a cross-chain join and it is free.
- **`/v4/dex/pairs/quotes/latest` by pool address** answered depth-now for every pool we asked
  about, and its `network_slug` accepts the same slugs `liquidity-change/list` does.
- The platform slugs `ethereum`, `base`, `bsc`, `arbitrum`, `polygon`, `unichain`, `optimism`,
  `gnosis`, `avalanche` and `solana` all answered `liquidity-change/list` on the first try —
  the display name of `/v1/dex/platform/list`, lower-cased, is the slug, which is undocumented
  but consistent.
