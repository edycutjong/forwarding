# The rule — Forwarding Address, formally

One page, so a judge can disagree with a threshold instead of with a black box. Every constant
below is a named module constant in `scripts/forwarding.py`; every invariant has a test.

## Inputs

All from CoinMarketCap's keyless `/public-api`, verified live 2026-09-18 and 2026-09-19:

| Field | Endpoint | Meaning |
|---|---|---|
| `m` | `/v1/dex/liquidity-change/list` | maker — the transaction sender's own wallet (never a position manager: 32 distinct makers on 71 Uniswap v3 rows, `docs/proof/spike_maker.json`) |
| `tp`, `tu` | same | side (`add`/`remove`) and signed USD value |
| `f`, `t0a`, `t1a` | same | factory address and the pair's token addresses — the pool identity |
| `en` | same | venue name; **absent on unlabeled venues**, which is why identity uses `f` |
| `ts`, `txn`, `lgid` | same | time (ms, as a string), transaction hash, log index — `(txn, lgid)` is the row key |
| `maker=` | same, as a query parameter | server-side filter: one wallet's events across every pool of the token, one call |
| `addr`, `liqUsd`, `pubAt`, `fa`, `t0.addr`, `t1.addr` | `/v1/dex/token/pools` | pool address, depth now, creation time, identity |
| `cid`, `plt`, `addr`, `liq` | `/v1/dex/search` | asset id, and the same asset's contracts on other chains |
| `contract_address[]` | `/v2/cryptocurrency/info` | the canonical registry the search rows are checked against |
| `quote[0].liquidity` | `/v4/dex/pairs/quotes/latest` | a pool's depth now, by pool address |

## Definitions

```
identity(row)   = (row.f, {row.t0a, row.t1a})            # venue factory + unordered pair — NOT a pool address
identity(pool)  = (pool.fa, {pool.t0.addr, pool.t1.addr})
JIT(txn)        = the transaction contains both an add and a remove
plausible(row)  = |row.tu| <= SANE_USD                      # 1e10: above it, a price-feed artefact
usd(row)        = |row.tu|
```

## Constants

| Name | Value | Role |
|---|---|---|
| `MIN_USD` | 100,000 | a removal below this is not an alert |
| `MIN_SHARE` | 0.10 | a removal below 10% of its pool is skipped (share = usd / (usd + pool.liqUsd now)); a pool absent from the token's pool list has an unknown share and is kept, with a note |
| `W_BACK_H`, `W_FWD_H` | 6, 6 | the window around the removal the wallet is followed in |
| `FULL` | 0.70 | REBALANCE / MIGRATION threshold |
| `PARTIAL_MIN` | 0.10 | below this, nothing meaningful came back |
| `TRIGGER_PAGES` | 3 | 300 rows ≥ `MIN_USD` is the trigger's depth |
| `FOLLOW_PAGES` | 10 | the most pages one wallet's follow will walk |
| `SIBLING_MIN_LIQ` | 10,000 | another chain is followed only if the asset has ≥ $10k of DEX liquidity there |
| `SANE_USD` | 1e10 | no liquidity event has ever been $10B; above it the row is refused |

## State machine

```
NONE ──(remove row, |tu| ≥ MIN_USD, plausible)──► CANDIDATE
CANDIDATE ──(txn also adds)──────────────────────► JIT            refused: "not an event"
CANDIDATE ──(share < MIN_SHARE)──────────────────► SKIPPED        next candidate; reason recorded
CANDIDATE ──(follow the maker: same chain, then every EVM sibling chain)──► FOLLOWED
FOLLOWED ──(same/removed ≥ FULL and same ≥ other)► REBALANCE      grey
FOLLOWED ──(other/removed ≥ FULL)────────────────► MIGRATION      amber   ──(dest.pubAt > removal.ts)──► CONSOLIDATION  amber
FOLLOWED ──(PARTIAL_MIN ≤ (same+other)/removed)──► PARTIAL        amber-red
FOLLOWED ──(nothing, every follow complete)──────► EXIT           red
FOLLOWED ──(nothing, a follow failed/throttled)──► INCOMPLETE     red, "re-run"
```

where, over the adds by the same maker inside `[ts − W_BACK_H, ts + W_FWD_H]` with JIT and
implausible rows excluded:

```
same  = Σ usd(add)  for identity(add) == identity(removal) on the same chain
other = Σ usd(add)  for every other identity, on this chain or any followed sibling chain
recovered_share = same/removed (REBALANCE) · other/removed (MIGRATION, CONSOLIDATION, EXIT, INCOMPLETE) · (same+other)/removed (PARTIAL)
destination     = the identity with the largest Σ usd among `other`, named from the pool list
elapsed_s       = ts(first add into the destination) − ts(removal)   (signed; REBALANCE: first add back)
```

Cross-chain: the sibling set is every `/v1/dex/search` row with the same `cid` as the source
token whose address is an EVM address, is present in `/v2/cryptocurrency/info`'s
`contract_address[]`, and has ≥ `SIBLING_MIN_LIQ` of DEX liquidity. Solana rows are refused
(a Solana maker cannot be this wallet). Skipped chains are listed in `refused[]`.

## Invariants (each has a test; `scripts/verify.py` re-checks them on every committed receipt)

| | Statement | Where it is tested |
|---|---|---|
| **I1** | The model never produces a number: every float in a `Verdict` is a sum or a ratio of `tu` fields on API rows | `test_property.py`, `verify.py` |
| **I2** | `recovered_usd ≤ Σ adds by the same maker in the window`; a JIT transaction's rows never count; only `tp == "add"` counts | `test_adjudicate.py`, `test_property.py` |
| **I3** | A MIGRATION / CONSOLIDATION names a destination identity ≠ the removal's identity | `test_adjudicate.py`, `test_property.py` |
| **I4** | Two runs on the same rows are byte-identical | `bench.py --replay` (1000/1000), `verify.py` |
| **I5** | An EXIT is stated with the window and the chains it searched — never as "gone" | `test_investigate.py`, `verify.py` |
| **I6** | An EXIT requires every planned follow to have returned 200; a throttled or failed follow yields INCOMPLETE, so the anonymous tier can never manufacture an Exit | `test_investigate.py`, `test_property.py`, `verify.py` |

## What the rule does not claim

- **Pool identity is venue + pair, not pool address.** Two fee tiers of one pair on one venue
  are one identity; the card says so when the pool list shows more than one.
- **The window is ±6 h**, enforced by walking the cursor (`startTime` is plan-gated keyless).
  Adds before the removal count too, so a wallet that cycles more than once inside the window
  can show a share above 100% — the verdict counts and discloses the other removals.
- **A wallet is not an entity.** Liquidity moved through a second wallet is not followed.
- **Same-chain only on the hosted page** (`/api/lookup`) and in the base rate; the CLI and the
  MCP tool follow every sibling chain.
