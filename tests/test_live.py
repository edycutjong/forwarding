"""Live tests — the external contract, against the real CoinMarketCap API, keyless.

    pytest -m live

These assert the OUTSIDE WORLD, not a return value (LESSONS R11): that the endpoint really
filters by maker, that the committed hero reproduces from a fresh fetch, that the cross-chain
slugs answer. They are designed to fail if CoinMarketCap changes the contract. Each one is a
handful of calls on the anonymous tier; run them one file at a time, not in a loop.
"""

import json
import time
from pathlib import Path

import forwarding
import pytest
from forwarding import Client, investigate, walk

PROOF = Path(__file__).resolve().parents[1] / "docs" / "proof"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
HERO_MAKER = "0xc3da4779d7e069a36b81d7d2bfb8ed882a1a5e56"
HERO_TXN = "0xf560f13928f2e654062a38be2725efd92dc4d56c5071a103927cf92b92074793"

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _breathe():
    """The anonymous tier is per IP; give it a moment between tests."""
    yield
    time.sleep(2)


def test_live_maker_filter_returns_only_that_wallet_and_no_key_is_sent(monkeypatch):
    for var in forwarding.KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    c = Client()
    rows, meta = walk(c, {"platform": "ethereum", "address": UNI, "maker": HERO_MAKER}, 1)
    assert meta["error"] is None, meta["error"]
    assert rows, "the hero wallet has liquidity events on UNI"
    assert all(r.get("m") == HERO_MAKER for r in rows)
    assert c.credits == 0
    assert c.calls[0]["endpoint"] == "/v1/dex/liquidity-change/list"
    assert c.calls[0]["params"]["maker"] == HERO_MAKER


def test_live_hero_reproduces_the_committed_recovered_share_to_six_decimals():
    """The killer number, re-derived from a fresh fetch of the same rows."""
    stored = json.loads((PROOF / "hero.json").read_text())["verdict"]
    r = stored["removal"]
    v = investigate(
        stored["token"]["platform"],
        stored["token"]["address"],
        txn=r["txn"],
        maker=r["m"],
        cross_chain=False,
    )
    assert v.kind == stored["kind"]
    assert abs(v.recovered_share - stored["recovered_share"]) < 1e-6
    assert v.removal["tu"] == r["tu"]
    assert v.elapsed_s == stored["elapsed_s"]
    if stored["destination"]:
        assert v.destination["venue"] == stored["destination"]["venue"]


def test_live_the_uni_v3_to_v4_migration_still_reproduces():
    """The event the mechanism was found on (2026-09-18): 99.9% in 4 min 12 s, v3 → v4."""
    v = investigate("ethereum", UNI, txn=HERO_TXN, maker=HERO_MAKER, cross_chain=False)
    assert v.kind == "MIGRATION"
    assert abs(v.recovered_share - 2918987.6118409373 / 2921711.1957615116) < 1e-6
    assert v.elapsed_s == 252
    assert v.destination["venue"] == "Uniswap v4 (Ethereum)"


def test_live_rows_have_the_shape_the_engine_reads():
    """ts/h/lgid as strings, tu signed, m an EVM address, f always present."""
    c = Client()
    rows, meta = walk(c, {"platform": "ethereum", "address": UNI, "minVolume": 100000}, 1)
    assert meta["error"] is None and rows
    r = rows[0]
    for k in ("ts", "h", "lgid"):
        assert isinstance(r[k], str) and r[k].isdigit(), k
    assert r["tp"] in ("add", "remove")
    assert (float(r["tu"]) < 0) == (r["tp"] == "remove")
    assert forwarding.EVM_ADDR.match(r["m"])
    assert r.get("f") and r.get("t0a") and r.get("t1a")
    assert all(forwarding.usd(x) >= 100000 for x in rows)  # minVolume matches on |tu|


@pytest.mark.parametrize(
    "slug,address",
    [
        ("bsc", "0xbf5140a22578168fd562dccf235e5d43a02ce9b1"),
        ("arbitrum", "0xfa7f8980b0f1e64a2062791cc3b0871572f1f7f0"),
        ("polygon", "0xb33eaad8d922b1083446dc23f610c2567fb5180f"),
        ("unichain", "0x8f187aa05619a017077f5308904739877ce9ea21"),
    ],
)
def test_live_cross_chain_platform_slugs_answer(slug, address):
    c = Client()
    d = c.get("/v1/dex/liquidity-change/list", platform=slug, address=address, limit=5)
    assert "_err" not in d, d
    assert "lcs" in (d.get("data") or {})
