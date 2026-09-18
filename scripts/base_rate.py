#!/usr/bin/env python3
"""How often is "LP removed ≥ $100k" the same wallet putting it back? Measured, not assumed.

    python3 scripts/base_rate.py                      # docs/proof/base_rate.json
    python3 scripts/base_rate.py --min-usd 50000 --pages 2

Method (stated beside the number wherever it is shown):
    1. sweep      liquidity-change/list?minVolume=MIN_USD, PAGES pages per watchlist token
    2. filter     JIT transactions discarded; non-JIT removals >= MIN_USD are the population
    3. follow     for each removal, the maker's own rows on that token (maker= filter, cursor
                  walked to the window start) — SAME CHAIN ONLY; one call per distinct wallet,
                  reused across that wallet's removals
    4. adjudicate the published rule (forwarding.adjudicate) on the +-6 h window
    5. split      REBALANCE / MIGRATION / CONSOLIDATION / PARTIAL / EXIT / INCOMPLETE

"EXIT" here means "nothing re-added on the same chain within +-6 h, every follow 200". The
cross-chain follow the product runs is not run here (it would multiply the call count by five
on a per-IP anonymous tier), so the EXIT count is an upper bound on true exits and is labelled
that way. A throttled follow lands in INCOMPLETE, never in EXIT (invariant I6).

Resumable: the output file is rewritten after every follow; re-running skips rows already
classified, so a throttle costs a minute, not the run.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forwarding import (  # noqa: E402
    MIN_USD,
    WATCHLIST,
    Client,
    adjudicate,
    fmt_utc,
    jit_txns,
    pair,
    pool_id,
    pools_of,
    ts_ms,
    usd,
    venue,
    walk,
    window_rows,
)

OUT = Path(__file__).resolve().parents[1] / "docs" / "proof" / "base_rate.json"
KINDS = ["REBALANCE", "MIGRATION", "CONSOLIDATION", "PARTIAL", "EXIT", "INCOMPLETE"]


def load_existing(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-usd", type=float, default=MIN_USD)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--json", default=str(OUT))
    ap.add_argument("--fresh", action="store_true", help="ignore a previous partial run")
    a = ap.parse_args()
    out_path = Path(a.json)
    started = time.time()

    prev = None if a.fresh else load_existing(out_path)
    done = {}
    if prev and prev.get("min_usd") == a.min_usd and prev.get("pages") == a.pages:
        done = {(r["platform"], r["txn"], r["lgid"]): r for r in prev.get("rows", [])}
        if done:
            print(f"resuming — {len(done)} removal(s) already classified in {out_path.name}")

    c = Client()
    print(
        f"base rate — keyless · {len(WATCHLIST)} tokens · ≥ ${a.min_usd:,.0f} · "
        f"{a.pages} page(s) each · same-chain maker follow · ±6 h\n"
    )
    population, sweep_meta = [], []
    for platform, address, sym in WATCHLIST:
        rows, meta = walk(
            c, {"platform": platform, "address": address, "minVolume": int(a.min_usd)}, a.pages
        )
        jit = jit_txns(rows)
        removes = [
            r
            for r in rows
            if r.get("tp") == "remove" and r.get("txn") not in jit and usd(r) >= a.min_usd
        ]
        sweep_meta.append(
            {
                "sym": sym,
                "platform": platform,
                "rows": len(rows),
                "pages": meta["pages"],
                "error": meta["error"],
                "jit_txns": len(jit),
                "removals": len(removes),
            }
        )
        print(
            f"  {sym:6s} {platform:9s} {len(rows):3d} rows · {len(jit):2d} JIT · "
            f"{len(removes):2d} removals ≥ ${a.min_usd:,.0f}"
            + (f" · {meta['error']}" if meta["error"] else "")
        )
        for r in removes:
            population.append((platform, address, sym, r))
    print(f"\n  population: {len(population)} removals\n")

    pools_cache, maker_cache = {}, {}
    results = list(done.values())
    for i, (platform, address, sym, r) in enumerate(population, 1):
        key = (platform, r.get("txn"), str(r.get("lgid")))
        if key in done:
            continue
        if (platform, address) not in pools_cache:
            pools_cache[(platform, address)], _ = pools_of(c, platform, address)
        pools = pools_cache[(platform, address)]
        mk = (platform, address, r.get("m"))
        if mk not in maker_cache:
            rows, meta = walk(
                c, {"platform": platform, "address": address, "maker": r.get("m")}, 10
            )
            maker_cache[mk] = (rows, meta)
        rows, meta = maker_cache[mk]
        inside, wmeta = window_rows(rows, meta, t0_ms=ts_ms(r))
        complete = meta["error"] is None
        adds = [{"platform": platform, "row": x} for x in inside]
        keyed = {(platform, k): v for k, v in pools.items()}
        v = adjudicate(r, adds, source_platform=platform, complete=complete, pools=keyed)
        p = pools.get(pool_id(r))
        share = usd(r) / (usd(r) + p["liqUsd"]) if p else None
        row_out = {
            "sym": sym,
            "platform": platform,
            "txn": r.get("txn"),
            "lgid": str(r.get("lgid")),
            "utc": fmt_utc(ts_ms(r)),
            "venue": venue(r),
            "pair": pair(r),
            "maker": r.get("m"),
            "removed_usd": usd(r),
            "share_of_pool": share,
            "kind": v["kind"],
            "recovered_share": v["recovered_share"],
            "same_share": v["same_share"],
            "other_share": v["other_share"],
            "destination": (
                f"{v['destination']['venue']} · {v['destination']['pair']}"
                if v["destination"]
                else None
            ),
            "elapsed_s": v["elapsed_s"],
            "follow_rows": len(rows),
            "follow_reached_window_start": wmeta["reached_window_start"],
            "follow_error": meta["error"],
        }
        results.append(row_out)
        done[key] = row_out
        print(
            f"  {i:3d}/{len(population)} {sym:6s} −${usd(r):>10,.0f} {venue(r):<26} "
            f"{v['kind']:<13} {v['recovered_share'] * 100:6.1f}%"
            + (f"  → {row_out['destination']}" if row_out["destination"] else "")
        )
        _write(out_path, a, started, results, sweep_meta, c, partial=i < len(population))
    _write(out_path, a, started, results, sweep_meta, c, partial=False)
    split = {k: sum(1 for r in results if r["kind"] == k) for k in KINDS}
    n = len(results)
    back = split["REBALANCE"] + split["MIGRATION"] + split["CONSOLIDATION"] + split["PARTIAL"]
    print(f"\n  n = {n} removals ≥ ${a.min_usd:,.0f}")
    for k in KINDS:
        print(f"  {k:<13} {split[k]:4d}  {split[k] / n * 100 if n else 0:5.1f}%")
    print(
        f"\n  {back / n * 100 if n else 0:.0f}% of large removals were the same wallet putting "
        "liquidity back within 6 h (same chain)"
    )
    print(f"  {len(c.calls)} calls · {c.credits} credits · {time.time() - started:.0f} s · keyless")


def _write(path, a, started, results, sweep_meta, c, partial):
    n = len(results)
    split = {k: sum(1 for r in results if r["kind"] == k) for k in KINDS}
    back = split["REBALANCE"] + split["MIGRATION"] + split["CONSOLIDATION"] + split["PARTIAL"]
    payload = {
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "partial": partial,
        "wall_clock_s": round(time.time() - started, 1),
        "auth": "none — CoinMarketCap keyless /public-api surface",
        "credits_used": c.credits,
        "calls_made": len(c.calls),
        "min_usd": a.min_usd,
        "pages": a.pages,
        "window_h": 6,
        "method": (
            "same-chain maker follow (liquidity-change/list?maker=, cursor walked to the window "
            "start), JIT excluded, published thresholds; cross-chain NOT followed here, so EXIT "
            "is an upper bound"
        ),
        "watchlist": [list(t) for t in WATCHLIST],
        "sweep": sweep_meta,
        "n": n,
        "split": split,
        "put_back_share": back / n if n else None,
        "rows": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
