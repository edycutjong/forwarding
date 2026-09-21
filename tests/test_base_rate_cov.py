"""The base-rate script: the sweep, the same-chain follow, the split, and the resumable file."""

import json
import runpy

import base_rate
import forwarding
import pytest
from conftest import (
    HERO_ADD,
    HERO_REMOVE,
    MAKER,
    OTHER,
    T0,
    THROTTLED,
    UNI,
    V3_POOL,
    V4_POOL,
    FakeClient,
    envelope,
    ep,
    lc,
    row,
)

LINK = "0x514910771af9ca656af840dff83e8264ecf986ca"
UNKNOWN_FACTORY = "0x00000000000000000000000000000000deadbeef"
SECOND_REMOVE = row("remove", -250_000.0, ts=T0 + 3_600_000, lgid="300")
OTHER_REMOVE = row("remove", -400_000.0, m=OTHER, f=UNKNOWN_FACTORY, lgid="301")
JIT_TXN = "0x" + "ab" * 32
SWEEP_ROWS = [
    HERO_REMOVE,
    SECOND_REMOVE,
    OTHER_REMOVE,
    row("add", 900_000.0, lgid="302"),
    row("remove", -150_000.0, lgid="303", txn=JIT_TXN),
    row("add", 150_000.0, lgid="304", txn=JIT_TXN),
    row("remove", -99_999.0, lgid="305"),
    row("remove", -float(forwarding.SANE_USD) * 2, lgid="306"),
]


def routes(sweep_rows=SWEEP_ROWS):
    return [
        (lc(platform="ethereum", address=UNI, maker=MAKER), envelope([HERO_ADD, HERO_REMOVE])),
        (lc(platform="ethereum", address=UNI, maker=OTHER), envelope([])),
        (lc(platform="ethereum", address=UNI, maker=False), envelope(sweep_rows)),
        (lc(platform="ethereum", address=LINK), THROTTLED),
        (ep("/v1/dex/token/pools", platform="ethereum"), {"data": [V3_POOL, V4_POOL]}),
    ]


@pytest.fixture
def two_tokens(monkeypatch):
    """A two-token watchlist routed to a fake client; LINK's sweep is throttled."""
    made = []

    def install(rows=SWEEP_ROWS):
        def factory(**kw):
            kw.pop("spacing", None)
            c = FakeClient(routes(rows), **kw)
            made.append(c)
            return c

        watchlist = [("ethereum", UNI, "UNI"), ("ethereum", LINK, "LINK")]
        for mod in (base_rate, forwarding):
            monkeypatch.setattr(mod, "Client", factory)
            monkeypatch.setattr(mod, "WATCHLIST", watchlist)
        return made

    return install


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["base_rate", *argv])
    base_rate.main()


def test_load_existing_returns_the_file_none_for_garbage_and_none_when_absent(tmp_path):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text('{"n": 1}')
    bad.write_text("{not json")
    assert base_rate.load_existing(good) == {"n": 1}
    assert base_rate.load_existing(bad) is None
    assert base_rate.load_existing(tmp_path / "missing.json") is None


def test_a_fresh_run_sweeps_filters_follows_and_writes_the_split(
    two_tokens, tmp_path, monkeypatch, capsys
):
    made = two_tokens()
    out = tmp_path / "proof" / "base_rate.json"
    run(monkeypatch, "--fresh", "--json", str(out))
    printed = capsys.readouterr().out
    r = json.loads(out.read_text())

    assert "UNI    ethereum    8 rows ·  1 JIT ·  3 removals ≥ $100,000" in printed
    assert "LINK   ethereum    0 rows ·  0 JIT ·  0 removals ≥ $100,000 · HTTP 429" in printed
    assert "population: 3 removals" in printed
    assert "→ Uniswap v4 (Ethereum) · UNI/USDC" in printed
    assert "n = 3 removals ≥ $100,000" in printed
    assert "MIGRATION        2   66.7%" in printed and "EXIT             1   33.3%" in printed
    assert "calls · 0 credits" in printed and "keyless" in printed

    assert r["partial"] is False and r["n"] == 3 and r["credits_used"] == 0
    assert r["auth"].startswith("none")
    assert r["watchlist"] == [["ethereum", UNI, "UNI"], ["ethereum", LINK, "LINK"]]
    assert [s["sym"] for s in r["sweep"]] == ["UNI", "LINK"]
    assert r["sweep"][0]["jit_txns"] == 1 and r["sweep"][0]["implausible_rows"] == 1
    assert r["sweep"][1]["error"].startswith("HTTP 429") and r["sweep"][1]["pages"] == 0
    by_lgid = {x["lgid"]: x for x in r["rows"]}
    hero = by_lgid["185"]
    assert hero["kind"] == "MIGRATION"
    assert hero["destination"] == "Uniswap v4 (Ethereum) · UNI/USDC"
    assert hero["share_of_pool"] == pytest.approx(
        hero["removed_usd"] / (hero["removed_usd"] + 1387794.66)
    )
    assert hero["follow_error"] is None and hero["follow_reached_window_start"] is True
    other = by_lgid["301"]
    assert other["kind"] == "EXIT" and other["destination"] is None
    assert other["share_of_pool"] is None and other["maker"] == OTHER
    assert r["split"]["MIGRATION"] == 2 and r["split"]["EXIT"] == 1
    assert r["put_back_share"] == pytest.approx(2 / 3)

    maker_follows = [c for c in made[0].calls if c["params"].get("maker")]
    assert len(maker_follows) == 2  # one per distinct wallet, reused across its removals
    assert len([c for c in made[0].calls if c["endpoint"] == "/v1/dex/token/pools"]) == 1


def test_a_partial_file_from_the_same_parameters_is_resumed_not_recomputed(
    two_tokens, tmp_path, monkeypatch, capsys
):
    made = two_tokens()
    out = tmp_path / "base_rate.json"
    run(monkeypatch, "--json", str(out))
    first = json.loads(out.read_text())
    keep = [x for x in first["rows"] if x["lgid"] == "185"]
    out.write_text(json.dumps(dict(first, partial=True, rows=keep)))
    capsys.readouterr()

    run(monkeypatch, "--json", str(out))
    printed = capsys.readouterr().out
    r = json.loads(out.read_text())
    assert "resuming — 1 removal(s) already classified in base_rate.json" in printed
    assert "  1/3 UNI" not in printed and "  2/3 UNI" in printed
    assert r["n"] == 3 and r["partial"] is False
    assert [c["params"].get("maker") for c in made[1].calls if c["params"].get("maker")] == [
        MAKER,
        OTHER,
    ]


def test_a_previous_file_with_other_parameters_or_no_rows_is_not_resumed(
    two_tokens, tmp_path, monkeypatch, capsys
):
    two_tokens()
    out = tmp_path / "base_rate.json"
    run(monkeypatch, "--json", str(out))
    first = json.loads(out.read_text())
    capsys.readouterr()

    out.write_text(json.dumps(dict(first, rows=[])))
    run(monkeypatch, "--json", str(out))
    assert "resuming" not in capsys.readouterr().out

    out.write_text(json.dumps(first))
    run(monkeypatch, "--json", str(out), "--min-usd", "50000")
    printed = capsys.readouterr().out
    assert "resuming" not in printed and "n = 4 removals ≥ $50,000" in printed

    out.write_text("{broken")
    run(monkeypatch, "--json", str(out), "--pages", "2")
    assert "resuming" not in capsys.readouterr().out
    assert json.loads(out.read_text())["pages"] == 2


def test_an_empty_population_writes_a_zero_split_with_no_put_back_share(
    two_tokens, tmp_path, monkeypatch, capsys
):
    two_tokens(rows=[row("add", 500_000.0)])
    out = tmp_path / "base_rate.json"
    run(monkeypatch, "--fresh", "--json", str(out))
    printed = capsys.readouterr().out
    r = json.loads(out.read_text())
    assert "population: 0 removals" in printed
    assert "n = 0 removals" in printed and "REBALANCE        0    0.0%" in printed
    assert "0% of large removals were the same wallet" in printed
    assert r["n"] == 0 and r["put_back_share"] is None and r["rows"] == []


def test_the_script_runs_as_a_program(two_tokens, tmp_path, monkeypatch, capsys):
    two_tokens()
    out = tmp_path / "base_rate.json"
    monkeypatch.setattr("sys.argv", ["base_rate", "--fresh", "--json", str(out)])
    runpy.run_path(base_rate.__file__, run_name="__main__")
    assert "population: 3 removals" in capsys.readouterr().out
    assert json.loads(out.read_text())["n"] == 3
