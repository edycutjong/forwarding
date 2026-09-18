#!/usr/bin/env python3
"""Capture the demo receipts from the live API by a PUBLISHED rule — never by hand.

    python3 scripts/seed.py            # docs/proof/{hero,exit,rebalance,jit,seed_sweep}.json
    python3 scripts/seed.py --only hero

The rule, printed on the landing page and reproduced here so a judge can re-run it:

    1. watchlist   the 11 tokens in forwarding.WATCHLIST (ethereum x8, base x2, bsc x1)
    2. sweep       liquidity-change/list?minVolume=25000&limit=100 for each — one call per token
    3. filter      drop every transaction that both adds and removes (just-in-time liquidity)
    4. hero        argmax |tu| over the remaining removals -> investigate() -> hero.json
                   WHATEVER its verdict is: the rule selects the removal, never the outcome
    5. exit        walk the remaining removals in |tu| order; the first whose full follow
                   (maker= on every EVM chain of the asset, +-6 h, every follow 200) finds
                   nothing re-added -> exit.json
    6. rebalance   the first whose follow lands >= 70% back in the same pool -> rebalance.json
    7. jit         the largest same-transaction add+remove pair discarded in step 3, handed to
                   investigate() by txn -> jit.json (the refusal, receipted)
    8. stamp       every receipt embeds every response verbatim, keyed by the sha256 the trace cites

WHAT THIS IS NOT: the demo path. `scripts/forwarding.py investigate` always runs live; nothing
on the judged path reads these files. They exist so the landing page, DEMO.md and verify.py
have dated, hash-checked evidence to render and replay.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forwarding import (  # noqa: E402
    FULL,
    WATCHLIST,
    Client,
    NoCandidate,
    Throttled,
    fmt_utc,
    follow_maker,
    investigate,
    jit_txns,
    pool_id,
    receipt,
    short,
    ts_ms,
    usd,
    venue,
    walk,
)

PROOF = Path(__file__).resolve().parents[1] / "docs" / "proof"
SWEEP_MIN_USD = 25_000
MAX_CANDIDATES = 12  # how far down the |tu| ranking the exit/rebalance search walks


def sweep(client, watchlist):
    """Step 2-3: one page per token, JIT filtered. Returns (candidates, jit_pairs, per_token)."""
    candidates, jit_pairs, per_token = [], [], []
    for platform, address, sym in watchlist:
        rows, meta = walk(
            client, {"platform": platform, "address": address, "minVolume": SWEEP_MIN_USD}, 1
        )
        if meta["error"] and not rows:
            per_token.append({"sym": sym, "platform": platform, "error": meta["error"]})
            print(f"  {sym:6s} {platform:9s} error — {meta['error']}")
            continue
        jit = jit_txns(rows)
        removes = [r for r in rows if r.get("tp") == "remove" and r.get("txn") not in jit]
        by_txn = {}
        for r in rows:
            if r.get("txn") in jit:
                by_txn.setdefault(r["txn"], []).append(r)
        for txn, pair_rows in by_txn.items():
            jit_pairs.append(
                {
                    "platform": platform,
                    "address": address,
                    "sym": sym,
                    "txn": txn,
                    "usd": max(usd(r) for r in pair_rows),
                    "rows": pair_rows,
                }
            )
        for r in removes:
            candidates.append({"platform": platform, "address": address, "sym": sym, "row": r})
        span_h = (max(ts_ms(r) for r in rows) - min(ts_ms(r) for r in rows)) / 3.6e6 if rows else 0
        per_token.append(
            {
                "sym": sym,
                "platform": platform,
                "address": address,
                "rows": len(rows),
                "span_h": round(span_h, 1),
                "jit_txns": len(jit),
                "removals": len(removes),
                "largest_removal_usd": max((usd(r) for r in removes), default=0),
            }
        )
        print(
            f"  {sym:6s} {platform:9s} {len(rows):3d} rows · {span_h:6.1f} h · "
            f"{len(jit):2d} JIT txns · {len(removes):2d} removals · largest "
            f"${max((usd(r) for r in removes), default=0):,.0f}"
        )
    candidates.sort(key=lambda c: usd(c["row"]), reverse=True)
    jit_pairs.sort(key=lambda j: j["usd"], reverse=True)
    return candidates, jit_pairs, per_token


def capture(c_, client, rule, name):
    """Run the full investigation on one candidate and write its receipt."""
    started = time.time()
    r = c_["row"]
    print(
        f"\n{name}: {c_['sym']} −${usd(r):,.0f} {venue(r)} {r.get('t0s')}/{r.get('t1s')} "
        f"{fmt_utc(ts_ms(r))} maker {short(r.get('m'))}"
    )
    try:
        v = investigate(c_["platform"], c_["address"], txn=r["txn"], maker=r["m"], client=client)
    except NoCandidate as e:
        print(f"  refused: {e}")
        return None
    except Throttled as e:
        print(f"  throttled: {e}")
        return None
    out = receipt(
        v,
        client,
        started=started,
        selection_rule=rule,
        extra={"sym": c_["sym"], "receipt": name, "min_usd_sweep": SWEEP_MIN_USD},
    )
    print(f"  → {v.headline()}")
    return out, v


def quick_class(client, c_):
    """A one-call same-chain follow to see roughly what a candidate is before spending a full
    investigation on it. Never written to disk — the receipts come from investigate()."""
    r = c_["row"]
    rows, meta = follow_maker(client, c_["platform"], c_["address"], r["m"], t0_ms=ts_ms(r))
    if meta["error"]:
        return "unknown", 0.0
    same = sum(usd(x) for x in rows if x.get("tp") == "add" and pool_id(x) == pool_id(r))
    other = sum(usd(x) for x in rows if x.get("tp") == "add" and pool_id(x) != pool_id(r))
    removed = usd(r)
    if same / removed >= FULL:
        return "rebalance", same / removed
    if other / removed >= FULL:
        return "migration", other / removed
    if (same + other) / removed >= 0.10:
        return "partial", (same + other) / removed
    return "nothing-same-chain", 0.0


def write(name, payload):
    PROOF.mkdir(parents=True, exist_ok=True)
    path = PROOF / f"{name}.json"
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))
    print(f"  wrote {path.relative_to(PROOF.parents[1])} ({path.stat().st_size:,} B)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["hero", "exit", "rebalance", "jit"], nargs="*")
    ap.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES)
    a = ap.parse_args()
    wanted = set(a.only or ["hero", "exit", "rebalance", "jit"])
    started = time.time()

    print(f"seed — keyless · {len(WATCHLIST)} tokens · minVolume={SWEEP_MIN_USD:,}\n")
    sweeper = Client(keep_bodies=True)
    candidates, jit_pairs, per_token = sweep(sweeper, WATCHLIST)
    write(
        "seed_sweep",
        {
            "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "rule": "one page of 100 rows per token at minVolume=25000; JIT transactions "
            "(add and remove in one txn) discarded; removals ranked by |tu|",
            "watchlist": [list(t) for t in WATCHLIST],
            "per_token": per_token,
            "candidates": [
                {
                    "sym": c["sym"],
                    "platform": c["platform"],
                    "usd": usd(c["row"]),
                    "utc": fmt_utc(ts_ms(c["row"])),
                    "venue": venue(c["row"]),
                    "pair": f"{c['row'].get('t0s')}/{c['row'].get('t1s')}",
                    "maker": c["row"].get("m"),
                    "txn": c["row"].get("txn"),
                }
                for c in candidates
            ],
            "jit_pairs": [
                {
                    "sym": j["sym"],
                    "platform": j["platform"],
                    "usd": j["usd"],
                    "txn": j["txn"],
                    "rows": j["rows"],
                }
                for j in jit_pairs[:10]
            ],
            "calls": sweeper.calls,
            "responses": sweeper.bodies,
        },
    )
    if not candidates:
        sys.exit("the sweep found no non-JIT removal — nothing to seed")

    hero_rule = (
        f"argmax |tu| over non-JIT removals >= ${SWEEP_MIN_USD:,} across the "
        f"{len(WATCHLIST)}-token watchlist, last 100 rows per token, at capture time"
    )
    if "hero" in wanted:
        got = capture(candidates[0], Client(keep_bodies=True), hero_rule, "hero")
        if got:
            write("hero", got[0])

    if "exit" in wanted or "rebalance" in wanted:
        need = {k for k in ("exit", "rebalance") if k in wanted}
        for c_ in candidates[1 : 1 + a.max_candidates]:
            if not need:
                break
            probe = Client()
            kind, share = quick_class(probe, c_)
            print(
                f"  probe {c_['sym']:6s} −${usd(c_['row']):>10,.0f} {venue(c_['row']):<26} "
                f"→ {kind} ({share * 100:.0f}%)"
            )
            if kind == "nothing-same-chain" and "exit" in need:
                got = capture(
                    c_,
                    Client(keep_bodies=True),
                    "first removal in |tu| order after the hero whose full follow (every EVM "
                    "chain of the asset, +-6 h, every follow 200) found nothing re-added",
                    "exit",
                )
                if got and got[1].kind == "EXIT":
                    write("exit", got[0])
                    need.discard("exit")
                elif got:
                    print(f"  (cross-chain changed it to {got[1].kind}; keep looking)")
            elif kind == "rebalance" and "rebalance" in need:
                got = capture(
                    c_,
                    Client(keep_bodies=True),
                    "first removal in |tu| order after the hero whose follow landed >= 70% "
                    "back in the same pool identity",
                    "rebalance",
                )
                if got and got[1].kind == "REBALANCE":
                    write("rebalance", got[0])
                    need.discard("rebalance")
        for k in need:
            print(
                f"\n  no {k.upper()} found among the top {a.max_candidates} candidates — "
                "say so on the page rather than manufacture one"
            )

    if "jit" in wanted and jit_pairs:
        j = jit_pairs[0]
        started_j = time.time()
        client = Client(keep_bodies=True)
        print(
            f"\njit: {j['sym']} ${j['usd']:,.0f} txn {short(j['txn'], 6)} "
            f"({len(j['rows'])} rows in one transaction)"
        )
        refused = None
        try:
            investigate(j["platform"], j["address"], txn=j["txn"], client=client)
        except NoCandidate as e:
            refused = str(e)
            print(f"  → refused: {e}")
        except Throttled as e:
            print(f"  throttled: {e}")
        write(
            "jit",
            {
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_j)),
                "wall_clock_s": round(time.time() - started_j, 2),
                "auth": "none — CoinMarketCap keyless /public-api surface",
                "credits_used": client.credits,
                "calls_made": len(client.calls),
                "selection_rule": "the largest same-transaction add+remove pair discarded by "
                "the sweep, handed to investigate() by txn",
                "sym": j["sym"],
                "platform": j["platform"],
                "address": j["address"],
                "txn": j["txn"],
                "usd": j["usd"],
                "rows": j["rows"],
                "refused": refused,
                "calls": client.calls,
                "responses": client.bodies,
            },
        )
    print(f"\ndone in {time.time() - started:.0f} s · 0 credits · keyless")


if __name__ == "__main__":
    main()
