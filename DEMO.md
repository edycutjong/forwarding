# Demo — a real run, with its receipt

Everything below is a transcript of an actual run against CoinMarketCap's live API on
**2026-09-18**. No fixtures, no flags, no key. Re-run it yourself in one command; the numbers may
differ, because they come from the market rather than from this file.

## Reproduce

```bash
git clone https://github.com/edycutjong/forwarding.git && cd forwarding
python3 scripts/forwarding.py investigate --platform ethereum --address 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984
```

That is the whole thing. No `pip install`, no `.env`, no signup — `forwarding.py` is stdlib-only
and every endpoint it calls is keyless. **There is no offline flag on this path, deliberately.**
The one replay mode in the repository (`scripts/bench.py --replay`, `scripts/verify.py`) replays
`adjudicate()` over receipts `seed.py` captured, and is labelled a replay everywhere it appears;
it is not the product.

**27 s is the clean-path time** (p50 of 5 live runs, [`bench_live.json`](docs/proof/bench_live.json)):
11–14 keyless calls at 2 s spacing. The endpoint is CoinMarketCap's shared anonymous tier,
rate-limited per IP. If it is throttling when you run, the script backs off (15 s, 30 s, 60 s)
and says so — the run below hit exactly one such backoff and took 50.6 s — and if the quota from
your IP is exhausted outright it stops with a message that names the two ways through: wait a
minute, or export a free key from [coinmarketcap.com/api](https://coinmarketcap.com/api) as
`CMC_API_KEY`, which moves the identical calls to the keyed endpoint. The key is an escape hatch,
never a requirement: the receipt below was taken with every CMC variable unset, and a keyed run
announces itself on its first line, its last line and in its receipt, so it can never pass as this
one. An exhausted quota exits **75** (`EX_TEMPFAIL`); "nothing qualified" exits **3**; a verdict
exits **0**.

## Receipt — live run, 2026-09-18T23:36:32Z

| | |
|---|---|
| **Wall clock** | **50.6 s**, cold start to final line — including one 15 s backoff on the first call (the receipt records it as `attempts: 2`) |
| **Token** | UNI on Ethereum, `0x1f9840a85d5af5bf1d1762f925bdaddc4201f984` |
| **Rows scanned** | 300 (3 pages × 100, `minVolume=100000`) — 150 non-JIT removals, 0 JIT transactions, 0 implausible rows |
| **The removal** | **−$21,330,275** out of Ring Exchange (Ethereum) · UNI/WBTC, 2026-09-02 03:59:11 UTC, maker `0x4f0aa5900b8292273b2f9a178d5468f8048bb9a9` |
| **The verdict** | **MIGRATION · 99.8% recovered** — +$21,287,255 into Ring Exchange (Ethereum) · UNI/WETH, **2 min 12 s** later, the same 1,962,475.5391248302 UNI |
| **API calls** | 14, all HTTP 200: token · list ×3 · pools · maker follow · search ×2 · info · 4 sibling-chain follows · quotes |
| **Credits used** | **0** — every endpoint is on the keyless `/public-api` surface |
| **Credentials** | none. Run with `CMC_API_KEY`, `COINMARKETCAP_API_KEY` and `CMC_PRO_API_KEY` explicitly unset |
| **Raw receipt** | [`docs/proof/live_run.json`](docs/proof/live_run.json) — the verdict, the rows, every call with its sha256, every response verbatim |

```
forwarding address — keyless · ethereum · 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984

  following the wallet
    GET /v1/dex/token                    platform=ethereum address=0x1f9840…01f984                  200   487 ms 
    GET /v1/dex/liquidity-change/list    platform=ethereum address=0x1f9840…01f984 minVolume=100000 200  1004 ms  100 rows
    GET /v1/dex/liquidity-change/list    platform=ethereum address=0x1f9840…01f984 minVolume=100000 lastId=AVd6RTNP…E9PQ== 200   641 ms  100 rows
    GET /v1/dex/liquidity-change/list    platform=ethereum address=0x1f9840…01f984 minVolume=100000 lastId=AVd6RTNP…YwPQ== 200   573 ms  100 rows
    GET /v1/dex/token/pools              platform=ethereum address=0x1f9840…01f984 size=20          200   516 ms  20 items
    GET /v1/dex/liquidity-change/list    platform=ethereum address=0x1f9840…01f984 maker=0x4f0aa5…8bb9a9 200   806 ms  2 rows
    GET /v1/dex/search                   q=0x1f9840…01f984                                          200   466 ms  4 tokens
    GET /v1/dex/search                   q=UNI                                                      200  1180 ms  50 tokens
    GET /v2/cryptocurrency/info          id=7083                                                    200   297 ms 
    GET /v1/dex/liquidity-change/list    platform=bsc address=0xbf5140…2ce9b1 maker=0x4f0aa5…8bb9a9 200   522 ms  0 rows
    GET /v1/dex/liquidity-change/list    platform=arbitrum address=0xfa7f89…f1f7f0 maker=0x4f0aa5…8bb9a9 200   501 ms  0 rows
    GET /v1/dex/liquidity-change/list    platform=unichain address=0x8f187a…e9ea21 maker=0x4f0aa5…8bb9a9 200   541 ms  0 rows
    GET /v1/dex/liquidity-change/list    platform=polygon address=0xb33eaa…b5180f maker=0x4f0aa5…8bb9a9 200   955 ms  0 rows
    GET /v4/dex/pairs/quotes/latest      network_slug=ethereum contract_address=0x8626be…4a7306     200   647 ms  1 items

  ● LP REMOVED  −$21,330,275   Ring Exchange (Ethereum) · UNI/WBTC   2026-09-02 03:59:11 UTC
    maker 0x4f0aa5900b8292273b2f9a178d5468f8048bb9a9 · share of pool unknown · txn 0x5081d9…dcd495

  ◆ MIGRATION · severity amber
    99.8% recovered — $21,287,255 of $21,330,275
    +$21,287,255 into Ring Exchange (Ethereum) · UNI/WETH (ethereum)   2 min 12 s later · pool now holds $102,239,395

  the rows (verbatim fields)
    remove  ts=1788321551000 tu=-21330274.564875204 m=0x4f0aa5900b8292273b2f9a178d5468f8048bb9a9 en=Ring Exchange (Ethereum) a0=-1962475.5391248302 txn=0x5081d9…dcd495
    add     ts=1788321683000 tu=21287254.934237212 m=0x4f0aa5900b8292273b2f9a178d5468f8048bb9a9 en=Ring Exchange (Ethereum) a0=1962475.5391248302 txn=0x9fafe3…71a0f3  [ethereum]
    21,287,254.93 ÷ 21,330,274.56 = 0.9980

  refused
    · Solana: 8FU95xFJ…Ceab36 is not an EVM address — a maker there cannot be this wallet
    · Gnosis: 0x4537e3…af9d74 holds $239 of DEX liquidity — below the $10,000 floor, not followed
    · HECO: 0x22c54c…5c46e6 holds $0 of DEX liquidity — below the $10,000 floor, not followed
    · Avalanche: 0x8ebaf2…ba8580 holds $214 of DEX liquidity — below the $10,000 floor, not followed

  notes
    · share of pool unknown — the pool is not in the token's pool list (20 returned), so its depth now is not known

  14 calls · 14 × 200 · 0 credits · 50.6 s · keyless
  wrote docs/proof/live_run.json
```

(The first line of the actual terminal output was the backoff notice on stderr —
`throttled (HTTP 429 (error 1022): You've reached the limit for anonymous access…) — waiting 15s
(attempt 1/3)` — which is why 50.6 s, not 27.)

### Read the two rows the way an LP would

At 03:59:11 UTC a wallet pulled **1,962,475.54 UNI and 137.96 WBTC — $21.3M — out of the
Ring Exchange UNI/WBTC pool.** An alert bot stops here, red.

**132 seconds later the same wallet put the same 1,962,475.5391248302 UNI, now paired with
4,402.69 WETH, into the Ring Exchange UNI/WETH pool.** It swapped its quote asset and re-added.
The liquidity did not leave the token; it moved one pool over. The agent rewrites the alert
from red to amber: **MIGRATION, 99.8% recovered.**

### Check it by hand

Two `tu` fields, one division: `21,287,254.934237212 ÷ 21,330,274.564875204 = 0.99798`.
Two `ts` fields, one subtraction: `1788321683000 − 1788321551000 = 132,000 ms = 2 min 12 s`.
Both rows come from the single call `GET /v1/dex/liquidity-change/list?platform=ethereum&address=0x1f98…f984&maker=0x4f0a…b9a9`,
whose full response is embedded in [`live_run.json`](docs/proof/live_run.json) under the
sha256 the trace cites. `scripts/verify.py` re-checks that the rows the verdict was computed
from appear byte-for-byte inside that stored response.

### The other outcomes, captured by the same published rule

`scripts/seed.py` sweeps the 11-token watchlist at the trigger's own depth and commits what
the rule selects — never what would look best:

| Receipt | Rule | What it found | Class |
|---|---|---|---|
| [`hero.json`](docs/proof/hero.json) | the largest non-JIT removal across the sweep | the $21.3M UNI event above, re-captured 23:16 UTC | **MIGRATION 99.8%** |
| [`runner_up.json`](docs/proof/runner_up.json) | the second-largest | LINK −$9,257,843 out of Uniswap v3 LINK/WETH, 2026-08-20 — nothing re-added on 8 chains | **EXIT** |
| [`rebalance.json`](docs/proof/rebalance.json) | the first whose follow landed ≥ 70% in the same pool | LINK −$8,506,390 out of Uniswap v4 LINK/ETH, back 48 min 48 s later | **REBALANCE 105%** |
| [`exit.json`](docs/proof/exit.json) | the first whose full follow found nothing | LINK −$5,823,200 out of Uniswap v4 LINK/ETH, 2026-08-15 — 8 chains, every follow 200 | **EXIT** |
| [`jit.json`](docs/proof/jit.json) | the largest same-transaction add+remove pair | DAI $229,834 in and out in one transaction | **refused** |
| [`uni_v3_v4.json`](docs/proof/uni_v3_v4.json) | the event the mechanism was found on, 2026-09-18 | UNI −$2,921,711 out of Uniswap v3 UNI/USDC → +$2,918,988 into Uniswap v4 UNI/USDC, 4 min 12 s, the same 342,774.9807839334 UNI | **MIGRATION 99.9%** |

### The honest branch: the base rate

Following one removal proves the mechanism. Following 335 measures it:

```
$ python3 scripts/base_rate.py
  n = 335 removals ≥ $100,000            (11 tokens · 3 chains · 352 keyless calls · 0 credits)
  REBALANCE      194   57.9%
  MIGRATION       19    5.7%
  CONSOLIDATION    0    0.0%
  PARTIAL         10    3.0%
  EXIT           112   33.4%             ← same-chain follow only: an upper bound on true exits
  INCOMPLETE       0    0.0%

  67% of large removals were the same wallet putting liquidity back within 6 h (same chain)
```

Two in three "liquidity pulled" alerts are the wrong headline. Every row, with its wallet,
class and destination, is in [`base_rate.json`](docs/proof/base_rate.json).

### The agent surface

```bash
claude mcp add forwarding -- python3 $PWD/scripts/mcp_server.py
```

then ask Claude Code where the UNI liquidity went. A real session — three tool calls, the
JIT refusal, every figure quoted from a tool result, 5 turns, 72 s — is committed verbatim in
[`docs/proof/mcp_session.md`](docs/proof/mcp_session.md).

### The autonomous loop

```bash
python3 scripts/forwarding.py watch --cycles 1          # one pass over the 11-token watchlist
python3 scripts/forwarding.py watch --interval 60       # keep polling; every new qualifying removal is adjudicated as it lands
python3 scripts/forwarding.py watch --webhook https://…  # …and POSTed as JSON
```

The first pass primes a per-token high-water mark; from then on only rows newer than the
mark fire. `--cycles 1` fires on the newest qualifying removal per token so a single pass is
a demo, not a wait.

## Benchmarks

```bash
make bench        # deterministic: adjudicate() over the committed receipts, 200 iterations each, no network
make bench-live   # the real thing: 5 keyless investigations end to end
```

| Measurement | n | p50 | p95 |
|---|---|---|---|
| Investigation, end to end, live ([`bench_live.json`](docs/proof/bench_live.json)) | 5 | **27.0 s** | 28.0 s |
| The join — one `maker=` call, live | 5 | 2.5 s | 3.0 s |
| Adjudication, replay ([`bench_replay.json`](docs/proof/bench_replay.json)) | 1,000 | **0.004 ms** | 0.006 ms |

Fetch and arithmetic are timed separately because averaging them would hide the only
interesting fact: the product's own work is microseconds, and every second a judge waits is
the anonymous tier's 2 s spacing plus network. The replay was byte-identical 1,000 of 1,000
times (invariant I4).

## Tests

`make test` runs **126** offline tests in about ten seconds; `make test-live` runs 10 more
against the real contract.

| | Count |
|---|---|
| Offline tests (`make test`) | **126** |
| Live tests against the real contract and the deployment (`make test-live`) | 10 |
| Regression tests named for the live defect they pin | 12 |
| Property-based verification of `adjudicate()` | **2,000 generated investigations, 0 failing** |

The live tests assert the **external** contract, not a return value: that `maker=` really
returns only that wallet's rows, that the committed hero reproduces from a fresh fetch to six
decimals, that `ts`/`h`/`lgid` are strings and `tu` is signed, that the four sibling-chain
slugs answer. They are designed to fail if CoinMarketCap changes the contract.

`scripts/verify.py` (`make verify`) replays every committed receipt through `adjudicate()`,
asserts the six invariants, checks that every evidence row appears verbatim inside a stored
response, and refuses to pass if `site/index.html` or `JUDGE.md` is not what the receipts render.

## Honest limitations

- **Pool identity is venue + pair, not pool address.** Rows carry no pool contract; two fee
  tiers of one pair on one venue collapse into one identity, and the removal's share of its pool
  is unknown when the pool is not in the 20 the API returns (as in the run above).
- **The window is ±6 h**, walked with the cursor because `startTime` is plan-gated keyless. A
  wallet that comes back a day later reads as an Exit for that window — the card says which
  window it searched. Adds before the removal count too, so a wallet that cycles more than once
  inside the window can show a share above 100%; the verdict counts and discloses the other
  removals.
- **A wallet is not an entity.** Liquidity moved through a second wallet is not followed.
- **The anonymous tier throttles per IP** and reports it as error 1022 or 1011, sometimes as a
  500. The tool backs off and records every retry; a throttled follow is INCOMPLETE, never an
  Exit. The hosted page's live box shares one IP across every visitor, so the page leads with
  the dated receipt and this CLI is the surface with a quota of your own.
- **The base rate follows wallets on the same chain only** — its EXIT count is an upper bound.
- **A `tu` of 10⁴² exists in the feed** (CAKE on BSC, an unlabeled venue). Rows above $10B are
  refused as price-feed artefacts and listed under `refused`; the finding is
  [FEEDBACK.md](FEEDBACK.md) #1.
