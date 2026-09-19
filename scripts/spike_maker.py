#!/usr/bin/env python3
"""Day-1 spike: is `m` on a liquidity-change row the LP's own wallet, or a position manager?

The whole product is a join on `m` across pools. If `m` were the NonfungiblePositionManager
(or any router), every Uniswap v3 event would share one maker and the join would degenerate
into "everything matches everything". This script asks the live API and writes what it saw
to docs/proof/spike_maker.json — verbatim responses, no key, nothing edited.

    python3 scripts/spike_maker.py

Three questions, each answered by a real call:
  1. On one token's most recent liquidity events, how many distinct makers are there, and do
     the Uniswap v3 rows share one maker (position manager) or many (EOAs)?
  2. Does `maker=<addr>` filter keyless, and does every row it returns carry that maker?
  3. Which platform slugs does liquidity-change/list accept for the same asset's other-chain
     contracts (the cross-chain follow needs them)?
"""

import collections
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://pro-api.coinmarketcap.com/public-api"
OUT = Path(__file__).resolve().parents[1] / "docs" / "proof" / "spike_maker.json"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
# UNI's contracts on other EVM chains, from /v1/dex/search?q=UNI (cid 7083) on 2026-09-18.
# The question is which `platform` slug each one answers to.
UNI_ELSEWHERE = [
    ("bsc", "0xbf5140a22578168fd562dccf235e5d43a02ce9b1"),
    ("arbitrum", "0xfa7f8980b0f1e64a2062791cc3b0871572f1f7f0"),
    ("polygon", "0xb33eaad8d922b1083446dc23f610c2567fb5180f"),
    ("unichain", "0x8f187aa05619a017077f5308904739877ce9ea21"),
]
CALLS: list[dict] = []
NPM = "0xc36442b4a4522e871399cd717abdd847ab11fe88"  # Uniswap v3 NonfungiblePositionManager


def get(path, **params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    t0 = time.time()
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                body = json.loads(raw)
                CALLS.append(
                    {
                        "endpoint": path,
                        "params": params,
                        "status": r.status,
                        "ms": int((time.time() - t0) * 1000),
                        "credit_count": (body.get("status") or {}).get("credit_count"),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                )
                return r.status, body
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:300]
            if e.code == 429 or e.code >= 500:
                wait = 15 * (2**attempt)
                print(f"   throttled {e.code}; wait {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            CALLS.append({"endpoint": path, "params": params, "status": e.code, "body": txt})
            return e.code, {"_err": txt}
    return 0, {"_err": "gave up"}


def main():
    out = {"ran_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "key_sent": False}

    print("1. makers on UNI's newest 100 liquidity events (ethereum)")
    st, body = get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI, limit=100)
    rows = (body.get("data") or {}).get("lcs") or []
    makers = collections.Counter(r.get("m") for r in rows)
    by_venue = collections.defaultdict(set)
    for r in rows:
        by_venue[r.get("en") or f"factory {r.get('f')}"].add(r.get("m"))
    v3 = [r for r in rows if "Uniswap v3" in str(r.get("en"))]
    v3_makers = {r.get("m") for r in v3}
    out["q1_maker_is_eoa"] = {
        "status": st,
        "rows": len(rows),
        "distinct_makers": len(makers),
        "uniswap_v3_rows": len(v3),
        "uniswap_v3_distinct_makers": len(v3_makers),
        "position_manager_appears_as_maker": NPM in {m.lower() for m in makers if m},
        "makers_per_venue": {k: len(v) for k, v in by_venue.items()},
        "top_makers": makers.most_common(5),
        "response": body,
    }
    print(
        f"   {len(rows)} rows · {len(makers)} distinct makers · Uniswap v3: {len(v3)} rows from "
        f"{len(v3_makers)} makers · position manager as maker: "
        f"{NPM in {m.lower() for m in makers if m}}"
    )
    time.sleep(2)

    print("2. maker= filter, keyless, on the largest remover in that page")
    removes = [r for r in rows if r.get("tp") == "remove"]
    target = max(removes, key=lambda r: abs(r.get("tu") or 0))["m"] if removes else rows[0]["m"]
    st, body = get(
        "/v1/dex/liquidity-change/list", platform="ethereum", address=UNI, maker=target, limit=100
    )
    frows = (body.get("data") or {}).get("lcs") or []
    venues = collections.Counter((r.get("en"), r.get("t0s"), r.get("t1s")) for r in frows)
    out["q2_maker_filter"] = {
        "status": st,
        "maker": target,
        "rows": len(frows),
        "all_rows_carry_that_maker": all(r.get("m") == target for r in frows),
        "pools_this_maker_touched": [list(k) + [n] for k, n in venues.most_common()],
        "response": body,
    }
    print(
        f"   maker {target}: {len(frows)} rows, all same maker: "
        f"{all(r.get('m') == target for r in frows)}, across {len(venues)} pool identities"
    )
    time.sleep(2)

    print("3. platform slugs for UNI's other-chain contracts")
    slugs = {}
    for slug, addr in UNI_ELSEWHERE:
        st, body = get("/v1/dex/liquidity-change/list", platform=slug, address=addr, limit=5)
        n = len((body.get("data") or {}).get("lcs") or []) if st == 200 else None
        slugs[slug] = {"status": st, "rows": n, "address": addr}
        print(f"   {slug:9s} status {st} rows {n}")
        time.sleep(2)
    out["q3_platform_slugs"] = slugs

    print("4. does dex/search resolve a contract address to its CMC id?")
    st, body = get("/v1/dex/search", q=UNI, limit=10)
    tks = (body.get("data") or {}).get("tks") or []
    hit = [t for t in tks if str(t.get("addr", "")).lower() == UNI]
    out["q4_search_by_address"] = {
        "status": st,
        "rows": len(tks),
        "exact_address_hit": bool(hit),
        "cid": hit[0].get("cid") if hit else None,
        "response": body,
    }
    print(
        f"   status {st}, {len(tks)} rows, exact hit: {bool(hit)}, "
        f"cid {hit[0].get('cid') if hit else None}"
    )

    out["calls"] = CALLS
    evm = re.compile(r"^0x[0-9a-f]{40}$")
    out["verdict"] = {
        "maker_is_eoa": (
            out["q1_maker_is_eoa"]["uniswap_v3_distinct_makers"] > 1
            and not out["q1_maker_is_eoa"]["position_manager_appears_as_maker"]
        ),
        "maker_filter_works_keyless": out["q2_maker_filter"]["all_rows_carry_that_maker"]
        and out["q2_maker_filter"]["rows"] > 0,
        "cross_chain_slugs_ok": [s for s, v in slugs.items() if v["status"] == 200],
        "search_resolves_address": out["q4_search_by_address"]["exact_address_hit"],
        "all_makers_look_like_evm_addresses": all(evm.match(m or "") for m in makers),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(f"\nverdict: {json.dumps(out['verdict'])}")
    print(f"wrote {OUT.relative_to(OUT.parents[2])} · {len(CALLS)} calls · no key sent")


if __name__ == "__main__":
    main()
