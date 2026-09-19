#!/usr/bin/env python3
"""Replay every committed receipt and refuse to ship one that does not hold up.

    python3 scripts/verify.py            # exit 1 on any failure
    python3 scripts/verify.py --json

For each of docs/proof/{hero,runner_up,exit,rebalance,uni_v3_v4}.json this
    1. re-runs adjudicate() on the receipt's own rows and asserts the stored verdict and the
       recovered share to 9 decimal places;
    2. checks the invariants in docs/SPEC.md, I1-I6, against the receipt;
    3. checks the receipt's chain of custody: every call in the trace has its response stored
       under the sha256 the trace cites, and every evidence row appears VERBATIM inside the
       maker-follow response it was taken from — a row cannot have been typed in.
It then checks docs/proof/jit.json is a refusal, docs/proof/base_rate.json adds up, and that
site/index.html, site/judge.html, site/pitch/index.html and JUDGE.md are what the receipts
render (render_site.py --check).

This is a CI gate, not the demo. `make demo` is the live keyless run.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forwarding import KINDS, adjudicate, jit_txns, pool_id, usd  # noqa: E402

BUILD = Path(__file__).resolve().parents[1]
PROOF = BUILD / "docs" / "proof"
RECEIPTS = ("hero", "runner_up", "exit", "rebalance", "uni_v3_v4")


def check(cond, label, failures, detail=""):
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label if not detail else f"{label}: {detail}")


def rows_in_body(body):
    """Every liquidity-change row inside one stored response."""
    data = (body or {}).get("data") or {}
    return data.get("lcs") or [] if isinstance(data, dict) else []


def verify_receipt(name, failures):
    d = json.loads((PROOF / f"{name}.json").read_text())
    v = d["verdict"]
    removal, evidence, platform = v["removal"], v["evidence"], v["token"]["platform"]
    complete = all(f["complete"] for f in v["follows"])
    pools = {}
    if v.get("destination"):
        dest = v["destination"]
        key = (dest["platform"], (dest["identity"][1], tuple(dest["identity"][2])))
        pools[key] = {"addr": dest["addr"], "pubAt_ms": dest["pubAt_ms"], "fee_tiers": 1}
    print(
        f"\n{name}.json — captured {d['captured_utc']}, {d['calls_made']} calls, "
        f"{d['credits_used']} credits"
    )

    a = adjudicate(removal, evidence, source_platform=platform, complete=complete, pools=pools)
    check(
        a["kind"] == v["kind"], f"replay reproduces the verdict ({v['kind']})", failures, a["kind"]
    )
    check(
        abs(a["recovered_share"] - v["recovered_share"]) < 1e-9,
        f"replay reproduces recovered share ({v['recovered_share']:.6f})",
        failures,
        f"{a['recovered_share']:.9f}",
    )
    check(v["kind"] in KINDS, "verdict class is one of the published six", failures)

    # I1 — every number is arithmetic on rows
    adds = [e["row"] for e in evidence if e["row"].get("tp") == "add"]
    same = sum(
        usd(r)
        for e, r in zip(evidence, [e["row"] for e in evidence], strict=True)
        if r.get("tp") == "add" and (e["platform"], pool_id(r)) == (platform, pool_id(removal))
    )
    other = sum(usd(r) for r in adds) - same
    expected = {"REBALANCE": same, "PARTIAL": same + other}.get(v["kind"], other)
    check(
        abs(v["recovered_usd"] - expected) < 1e-6,
        "I1 recovered_usd is the sum of the counted adds' tu",
        failures,
    )
    check(
        abs(v["removed_usd"] - usd(removal)) < 1e-9,
        "I1 removed_usd is |tu| of the removal row",
        failures,
    )

    # I2 — never more than the wallet added, JIT never counted, only this maker
    check(
        v["recovered_usd"] <= sum(usd(r) for r in adds) + 1e-6,
        "I2 recovered ≤ Σ adds by the wallet in the window",
        failures,
    )
    check(
        all(e["row"].get("m") == removal.get("m") for e in evidence),
        "I2 every evidence row carries the removal's maker",
        failures,
    )
    check(
        not jit_txns([e["row"] for e in evidence]),
        "I2 no JIT transaction among the evidence rows",
        failures,
    )

    # I3 — a migration names a destination that is not the source
    if v["kind"] in ("MIGRATION", "CONSOLIDATION"):
        dest = v["destination"]
        check(dest is not None, "I3 a migration names a destination pool", failures)
        src = [platform, pool_id(removal)[0], list(pool_id(removal)[1])]
        check(dest["identity"] != src, "I3 the destination is not the source pool", failures)
        check(
            dest.get("liquidity_now_usd"), "I3 the destination's depth was confirmed live", failures
        )

    # I4 — deterministic
    again = adjudicate(removal, evidence, source_platform=platform, complete=complete, pools=pools)
    check(
        json.dumps(a, sort_keys=True) == json.dumps(again, sort_keys=True),
        "I4 two replays are byte-identical",
        failures,
    )

    # I5 / I6 — an EXIT states where it looked, and every follow completed
    if v["kind"] == "EXIT":
        check(
            len(v["follows"]) >= 1 and all("platform" in f for f in v["follows"]),
            "I5 the EXIT states the chains it searched",
            failures,
        )
        check(
            v["window_h"]["back"] > 0 and v["window_h"]["fwd"] > 0,
            "I5 the EXIT states its window",
            failures,
        )
        check(
            all(f["complete"] for f in v["follows"]),
            "I6 every planned follow completed (200)",
            failures,
        )
        check(
            all(f["reached_window_start"] for f in v["follows"]),
            "I6 every follow reached the window start",
            failures,
        )
    if not complete:
        check(v["kind"] != "EXIT", "I6 an incomplete follow is never an EXIT", failures)

    # chain of custody
    shas = {c["sha256"] for c in d["calls"] if c.get("sha256")}
    check(
        shas <= set(d["responses"]),
        "every traced call's response is stored under its hash",
        failures,
        f"{len(shas - set(d['responses']))} missing",
    )
    check(
        all(c["status"] == 200 for c in d["calls"]),
        "every traced call returned 200",
        failures,
        str([c["status"] for c in d["calls"] if c["status"] != 200]),
    )
    check(
        d["credits_used"] == 0 and d["auth"].startswith("none"),
        "keyless: 0 credits, no key",
        failures,
    )
    stored_rows = [
        json.dumps(r, sort_keys=True)
        for body in d["responses"].values()
        for r in rows_in_body(body)
    ]
    verbatim = all(json.dumps(e["row"], sort_keys=True) in stored_rows for e in evidence)
    check(verbatim, "every evidence row is verbatim inside a stored API response", failures)
    check(
        json.dumps(removal, sort_keys=True) in stored_rows,
        "the removal row is verbatim inside a stored API response",
        failures,
    )
    endpoints = {c["endpoint"] for c in d["calls"]}
    check(
        "/v1/dex/liquidity-change/list" in endpoints and "/v1/dex/token/pools" in endpoints,
        "the join and the pool list were both called",
        failures,
    )
    return d


def verify_jit(failures):
    p = PROOF / "jit.json"
    if not p.exists():
        check(False, "jit.json exists", failures)
        return
    d = json.loads(p.read_text())
    print(f"\njit.json — captured {d['captured_utc']}, {d['calls_made']} calls")
    check(
        d["refused"] and "not an event" in d["refused"],
        "the JIT pair was refused, not adjudicated",
        failures,
    )
    sides = {r.get("tp") for r in d["rows"]}
    check(
        sides == {"add", "remove"} and len({r["txn"] for r in d["rows"]}) == 1,
        "the pair adds and removes in one transaction",
        failures,
    )
    check(d["credits_used"] == 0, "keyless: 0 credits", failures)


def verify_base_rate(failures):
    p = PROOF / "base_rate.json"
    if not p.exists():
        print("\nbase_rate.json — not captured yet (run: make base-rate)")
        return
    d = json.loads(p.read_text())
    print(f"\nbase_rate.json — captured {d['captured_utc']}, n = {d['n']}, {d['calls_made']} calls")
    split = d["split"]
    check(sum(split.values()) == d["n"] == len(d["rows"]), "the split adds up to n", failures)
    check(not d.get("partial"), "the run completed (not partial)", failures)
    back = sum(split[k] for k in ("REBALANCE", "MIGRATION", "CONSOLIDATION", "PARTIAL"))
    check(
        abs(d["put_back_share"] - back / d["n"]) < 1e-9,
        "put_back_share is the four classes over n",
        failures,
    )
    check(
        all(r["kind"] != "EXIT" or r["follow_error"] is None for r in d["rows"]),
        "I6 no EXIT row had a failed follow",
        failures,
    )
    check(
        all(r["removed_usd"] >= d["min_usd"] for r in d["rows"]), "every row is ≥ min_usd", failures
    )
    check(d["credits_used"] == 0, "keyless: 0 credits", failures)


def verify_site(failures):
    print("\nsite — render_site.py --check")
    r = subprocess.run(
        [sys.executable, str(BUILD / "scripts" / "render_site.py"), "--check"],
        capture_output=True,
        text=True,
    )
    check(
        r.returncode == 0,
        "site/index.html, site/judge.html, site/pitch/index.html and JUDGE.md are what the "
        "receipts render",
        failures,
        (r.stdout + r.stderr).strip()[:200],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-site", action="store_true", help="skip the rendered-page check")
    a = ap.parse_args()
    failures = []
    print("verify — replaying docs/proof/*.json through adjudicate(), no network")
    for name in RECEIPTS:
        if (PROOF / f"{name}.json").exists():
            verify_receipt(name, failures)
        else:
            check(False, f"{name}.json exists", failures)
    verify_jit(failures)
    verify_base_rate(failures)
    if not a.no_site and (BUILD / "scripts" / "render_site.py").exists():
        verify_site(failures)
    if a.json:
        print(json.dumps({"ok": not failures, "failures": failures}, indent=2))
    print(f"\n{'all checks passed' if not failures else f'{len(failures)} check(s) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
