"""Shared fixtures: real row shapes, and a fake client that routes by endpoint.

Every fixture row below has the exact field set the live endpoint returns (verified 2026-09-18
and 2026-09-19): `ts`, `h`, `lgid`, `txId` as strings, `tu`/`a0`/`a1` signed, `en`/`eid`
present on labeled venues and absent on unlabeled ones. The fake client subclasses the real
one so its receipts (`calls`) are produced by the same `_record()` a live run uses.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import forwarding  # noqa: E402

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
V3 = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
V4 = "0x000000000004444c5dc75cb358380d2e3de08a90"
MAKER = "0xc3da4779d7e069a36b81d7d2bfb8ed882a1a5e56"
OTHER = "0x33e4fe9b50298de6a23d5bca9874ea37803d8339"
T0 = 1789702343000  # 2026-09-18 03:32:23 UTC — the hero removal


def row(
    tp,
    tu,
    *,
    ts=T0,
    m=MAKER,
    en="Uniswap v3 (Ethereum)",
    eid=1348,
    f=V3,
    t0a=UNI,
    t1a=USDC,
    t0s="UNI",
    t1s="USDC",
    a0=None,
    txn=None,
    lgid=None,
    h="26001712",
):
    """A liquidity-change row in the live shape. `tu` is signed by the caller, as the API does.

    The row identity is (txn, lgid); both default to a digest of the row's own fields so two
    fixture rows are distinct unless the test says otherwise.
    """
    digest = hashlib.sha256(repr((tp, tu, ts, m, f, t1a)).encode()).hexdigest()
    lgid = lgid if lgid is not None else str(int(digest[:4], 16))
    r = {
        "ts": str(ts),
        "tp": tp,
        "f": f,
        "t0a": t0a,
        "t1a": t1a,
        "t0s": t0s,
        "t1s": t1s,
        "a0": a0 if a0 is not None else (tu / 8.52),
        "a1": 0,
        "tu": tu,
        "m": m,
        "txn": txn or "0x" + digest,
        "h": h,
        "txId": lgid,
        "lgid": lgid,
    }
    if en is not None:
        r["en"] = en
        r["eid"] = eid
    return r


# The hero, verbatim from the live API (docs/proof/spike_maker.json carries the same shape).
HERO_REMOVE = row(
    "remove",
    -2921711.1957615116,
    a0=-342774.9807839334,
    txn="0xf560f13928f2e654062a38be2725efd92dc4d56c5071a103927cf92b92074793",
    lgid="185",
)
HERO_ADD = row(
    "add",
    2918987.6118409373,
    ts=T0 + 252_000,
    en="Uniswap v4 (Ethereum)",
    eid=11955,
    f=V4,
    a0=342774.9807839334,
    txn="0x0b8c0bdbada121485d7c4009ca87b22ba5c5d284afc8751277d00036f8f89fb0",
    lgid="241",
    h="26001733",
)


def pool(addr, fa, t0, t1, liq, pub_at, exn="Uniswap v3 (Ethereum)", exid=1348):
    return {
        "addr": addr,
        "v24": "0",
        "pubAt": str(pub_at),
        "t0": {"addr": t0, "sym": "UNI", "liqUsd": str(liq / 2)},
        "t1": {"addr": t1, "sym": "USDC", "liqUsd": str(liq / 2)},
        "exid": exid,
        "exn": exn,
        "liqUsd": str(liq),
        "fa": fa,
        "top": True,
    }


V3_POOL = pool(
    "0xd0fc8ba7e267f2bc56044a7715a489d851dc6d78", V3, UNI, USDC, 1387794.66, 1620273020000
)
V4_POOL = pool(
    "0x9a5c1d2f4a7a7962a63259de6fcc1afb1d0aa1abdf5d19c23d22fd78953c5167",
    V4,
    UNI,
    USDC,
    6733223.64,
    1738173491000,
    exn="Uniswap v4 (Ethereum)",
    exid=11955,
)


def envelope(rows, last_id=None, tlu=42063378.87, lpc="10"):
    return {
        "data": {"lcs": rows, "lastId": last_id, "tlu": tlu, "lpc": lpc},
        "status": {"error_code": "0", "error_message": "", "elapsed": 315, "credit_count": 1},
    }


class FakeClient(forwarding.Client):
    """Routes each GET to a canned response; receipts are recorded by the real `_record`."""

    def __init__(self, routes, **kw):
        super().__init__(spacing=0, **kw)
        self.routes = list(routes)
        self.unrouted = []

    def get(self, path, **params):
        for match, resp in self.routes:
            if match(path, params):
                body = resp(path, params) if callable(resp) else resp
                if "_err" in body:
                    self._record(path, params, body.get("_status", 429), 1, error=body["_err"])
                    return body
                raw = json.dumps(body).encode()
                self._record(path, params, 200, 1, body=body, raw=raw)
                return body
        self.unrouted.append((path, params))
        return {"_err": f"unrouted {path} {params}", "_throttled": False, "_status": 0}


ANY = object()


def lc(platform=None, address=None, maker=None, page=ANY):
    """A matcher for /v1/dex/liquidity-change/list with optional constraints.

    page=None matches only a first page (no cursor); page="C1" only the page after cursor C1.
    """

    def m(path, p):
        if path != "/v1/dex/liquidity-change/list":
            return False
        if platform and p.get("platform") != platform:
            return False
        if address and str(p.get("address", "")).lower() != address.lower():
            return False
        if maker is not None and (p.get("maker") is None) != (maker is False):
            return False
        if maker and p.get("maker") != maker:
            return False
        if page is not ANY and (p.get("lastId") or None) != page:
            return False
        return True

    return m


def ep(path, **want):
    def m(p, params):
        return p == path and all(str(params.get(k)) == str(v) for k, v in want.items())

    return m


THROTTLED = {
    "_err": "HTTP 429 (error 1022): You've reached the limit for anonymous access",
    "_throttled": True,
    "_status": 429,
}


def scenario(
    *,
    trigger_rows,
    maker_rows,
    pools=(V3_POOL, V4_POOL),
    siblings=(),
    registry=None,
    sibling_rows=None,
    liquidity=None,
    token=None,
    extra=(),
):
    """Routes for one investigation on UNI/ethereum.

    siblings: [(plt_name, address, liq)] returned by the symbol search;
    registry: addresses the info registry knows (default: every sibling);
    sibling_rows: {(slug, address): [rows]} for the maker on other chains.
    """
    tk = token or {"n": "Uniswap", "sym": "UNI", "liqUsd": "42551754.9"}
    search_addr = {"data": {"tks": [{"plt": "Ethereum", "addr": UNI, "cid": 7083, "s": "UNI"}]}}
    sib_rows = [{"plt": "Ethereum", "addr": UNI, "cid": 7083, "s": "UNI", "liq": 41867612.0}] + [
        {"plt": plt, "addr": addr, "cid": 7083, "s": "UNI", "liq": liq}
        for plt, addr, liq in siblings
    ]
    reg = registry if registry is not None else [a for _, a, _ in siblings]
    info = {
        "data": {
            "7083": {
                "contract_address": [{"contract_address": UNI}]
                + [{"contract_address": a} for a in reg]
            }
        }
    }
    routes = list(extra) + [
        (ep("/v1/dex/token"), {"data": tk}),
        (lc(platform="ethereum", address=UNI, maker=MAKER), envelope(maker_rows)),
        (lc(platform="ethereum", address=UNI, maker=False), envelope(trigger_rows)),
        (ep("/v1/dex/token/pools", platform="ethereum"), {"data": list(pools)}),
        (ep("/v1/dex/search", q=UNI), search_addr),
        (ep("/v1/dex/search", q="UNI"), {"data": {"tks": sib_rows}}),
        (ep("/v2/cryptocurrency/info", id=7083), info),
    ]
    for (slug, addr), rows in (sibling_rows or {}).items():
        body = rows if isinstance(rows, dict) else envelope(rows)
        routes.append((lc(platform=slug, address=addr), body))
    routes.append((lc(), envelope([])))  # any other chain: the wallet did nothing there

    def quotes(path, p):
        liq = (liquidity or {}).get(p.get("contract_address"), 1234567.0)
        return {
            "data": [{"quote": [{"liquidity": liq}], "contract_address": p["contract_address"]}]
        }

    routes.append((ep("/v4/dex/pairs/quotes/latest"), quotes))
    return routes


@pytest.fixture
def hero_routes():
    return scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    """Every test runs with every credential unset — the judged path is keyless."""
    for var in forwarding.KEY_VARS:
        monkeypatch.delenv(var, raising=False)
