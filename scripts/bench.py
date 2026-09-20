#!/usr/bin/env python3
"""Reproducible benchmark: how long does following a wallet actually take?

Three things are timed separately, because they have different characters:

    investigate   one whole keyless investigation, end to end (network-bound: 8-12 calls,
                  2 s spacing on the anonymous tier, plus any backoff)
    follow        the join itself — one liquidity-change/list?maker= call (network-bound)
    adjudicate    the rule on the rows (CPU-bound, deterministic, microseconds)

    python3 scripts/bench.py --iterations 5           # live, keyless
    python3 scripts/bench.py --replay --iterations 200 # adjudicate() over the committed receipts

--replay needs no network and is deterministic, which makes it right for CI. It is NOT the
product: it measures the arithmetic over receipts seed.py captured. The live path is the judged
one, and it is timed on the hero removal by txn+maker so every iteration follows the same wallet.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forwarding import (  # noqa: E402
    Client,
    NoCandidate,
    Throttled,
    adjudicate,
    escape_hatch_var,
    follow_maker,
    investigate,
    ts_ms,
)

PROOF = Path(__file__).resolve().parents[1] / "docs" / "proof"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


def pct(values, p):
    """Nearest-rank percentile — no interpolation, so a p95 is always a real observation."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def report(label, samples, unit):
    print(
        f"  {label:26} n={len(samples):<4} p50 {pct(samples, 50):10.3f} {unit}   "
        f"p95 {pct(samples, 95):10.3f} {unit}   max {max(samples):10.3f} {unit}"
    )
    return {
        "n": len(samples),
        "p50": round(pct(samples, 50), 4),
        "p95": round(pct(samples, 95), 4),
        "max": round(max(samples), 4),
        "unit": unit,
    }


def replay_inputs(name):
    d = json.loads((PROOF / f"{name}.json").read_text())
    v = d["verdict"]
    complete = all(f["complete"] for f in v["follows"])
    pools = {}
    dest = v.get("destination")
    if dest:
        key = (dest["platform"], (dest["identity"][1], tuple(dest["identity"][2])))
        pools[key] = {"addr": dest["addr"], "pubAt_ms": dest["pubAt_ms"], "fee_tiers": 1}
    return v["removal"], v["evidence"], v["token"]["platform"], complete, pools, v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--replay", action="store_true", help="adjudicate only, from the receipts")
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()
    out = {"iterations": a.iterations, "mode": "replay" if a.replay else "live"}

    if a.replay:
        names = [
            n
            for n in ("hero", "runner_up", "exit", "rebalance", "uni_v3_v4")
            if (PROOF / f"{n}.json").exists()
        ]
        if not names:
            sys.exit("no receipts in docs/proof — run: python3 scripts/seed.py")
        print(f"replay — adjudicate() over {', '.join(names)}, no network\n")
        samples, identical, total = [], 0, 0
        for name in names:
            removal, evidence, platform, complete, pools, stored = replay_inputs(name)
            first = None
            for _ in range(a.iterations):
                t = time.perf_counter()
                r = adjudicate(
                    removal, evidence, source_platform=platform, complete=complete, pools=pools
                )
                samples.append((time.perf_counter() - t) * 1000)
                blob = json.dumps(r, sort_keys=True)
                first = first or blob
                identical += blob == first
                total += 1
            assert r["kind"] == stored["kind"], (name, r["kind"], stored["kind"])
            assert abs(r["recovered_share"] - stored["recovered_share"]) < 1e-9, name
            print(
                f"  {name:10} {r['kind']:<11} {r['recovered_share'] * 100:6.1f}%  "
                "matches the receipt"
            )
        print()
        out["adjudicate"] = report("adjudicate (pure)", samples, "ms")
        out["byte_identical"] = f"{identical}/{total}"
        out["receipts"] = names
        print(f"\n  byte-identical {identical}/{total} · no network")
    else:
        var = escape_hatch_var()
        surface = f"keyed via ${var} (escape hatch)" if var else "keyless"
        hero = json.loads((PROOF / "hero.json").read_text())["verdict"]
        txn, maker, platform = (
            hero["removal"]["txn"],
            hero["removal"]["m"],
            hero["token"]["platform"],
        )
        address = hero["token"]["address"]
        print(f"live — {surface}, {a.iterations} iterations on the hero removal\n")
        inv_s, follow_s, adj_ms, calls, throttled = [], [], [], [], 0
        for i in range(a.iterations):
            c = Client()
            t = time.perf_counter()
            try:
                v = investigate(platform, address, txn=txn, maker=maker, client=c)
            except (NoCandidate, Throttled) as e:
                print(f"  iteration {i + 1}: {type(e).__name__} — {e}")
                throttled += 1
                continue
            inv_s.append(time.perf_counter() - t)
            n_calls = len(c.calls)
            calls.append(n_calls)
            # a call that backed off and then answered is recorded once, with its attempts
            throttled += sum(max(0, x.get("attempts", 1) - 1) for x in c.calls)
            throttled += sum(1 for x in c.calls if x["status"] in (429, 500, 0))
            t = time.perf_counter()
            rows, meta = follow_maker(c, platform, address, maker, t0_ms=ts_ms(v.removal))
            follow_s.append(time.perf_counter() - t)
            t = time.perf_counter()
            adjudicate(v.removal, v.evidence, source_platform=platform)
            adj_ms.append((time.perf_counter() - t) * 1000)
            print(
                f"  iteration {i + 1}: {v.kind} {v.recovered_share * 100:.1f}% · "
                f"{n_calls} calls · {inv_s[-1]:.1f} s"
            )
        if not inv_s:
            sys.exit("every iteration failed — the anonymous tier is rate-limiting")
        print()
        out["investigate"] = report("investigate (end to end)", inv_s, "s")
        out["follow"] = report("follow_maker (1 call)", follow_s, "s")
        out["adjudicate"] = report("adjudicate (pure)", adj_ms, "ms")
        out["calls_per_investigation"] = int(statistics.median(calls))
        out["throttle_events"] = throttled
        out["credits_used"] = 0 if not var else sum(calls)
        print(
            f"\n  {out['calls_per_investigation']} calls per investigation · "
            f"{throttled} throttle event(s) · "
            f"{'0 credits — keyless' if not var else 'keyed'}"
        )

    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=2, sort_keys=True))
        print(f"  wrote {a.json}")


if __name__ == "__main__":
    main()
