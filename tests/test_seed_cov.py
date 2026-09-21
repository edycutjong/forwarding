"""scripts/seed.py — the published capture rule, run offline against routed responses."""

import json
import runpy
import sys
from pathlib import Path

import forwarding
import pytest
import seed
from conftest import (
    HERO_REMOVE,
    MAKER,
    OTHER,
    T0,
    THROTTLED,
    UNI,
    WETH,
    FakeClient,
    envelope,
    lc,
    row,
)

AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
CAKE = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"
WATCH = [("ethereum", UNI, "UNI"), ("base", AERO, "AERO"), ("bsc", CAKE, "CAKE")]
SEED_PY = Path(seed.__file__)


@pytest.fixture
def proof(monkeypatch, tmp_path):
    path = tmp_path / "docs" / "proof"
    monkeypatch.setattr(seed, "PROOF", path)
    return path


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(seed, "WATCHLIST", WATCH)

    def install(routes):
        made = []

        def factory(**kw):
            kw.pop("spacing", None)
            c = FakeClient(routes, **kw)
            made.append(c)
            return c

        monkeypatch.setattr(seed, "Client", factory)
        return made

    return install


@pytest.fixture
def engine(monkeypatch):
    """Replace investigate() with a lookup by txn: a Verdict to return or an exception to raise."""

    def install(plan):
        calls = []

        def fake(platform, address, *, txn=None, maker=None, client=None):
            calls.append((platform, address, txn, maker))
            got = plan[txn]
            if isinstance(got, Exception):
                raise got
            return got

        monkeypatch.setattr(seed, "investigate", fake)
        return calls

    return install


@pytest.fixture
def argv(monkeypatch):
    def install(*args):
        monkeypatch.setattr(sys, "argv", ["seed.py", *args])

    return install


def verdict(kind, removal, follows=1):
    return forwarding.Verdict(
        kind=kind,
        severity=forwarding.SEVERITY[kind],
        recovered_share=0.0,
        removed_usd=forwarding.usd(removal),
        recovered_usd=0.0,
        token={"sym": "UNI"},
        removal=removal,
        evidence=[],
        destination={"venue": "Uniswap v4 (Ethereum)", "pair": "UNI/USDC"},
        elapsed_s=300,
        removal_share_of_pool=None,
        source_pool=None,
        follows=[{"platform": "ethereum"}] * follows,
    )


def sweep_routes(**rows_by_address):
    return [
        (lc(address=addr, maker=False), envelope(rows)) for addr, rows in rows_by_address.items()
    ]


def candidate(r, platform="ethereum", address=UNI, sym="UNI"):
    return {"platform": platform, "address": address, "sym": sym, "row": r}


SMALL = row("remove", -120_000.0, ts=T0 + 3_600_000, m=OTHER)
JIT_ADD, JIT_REMOVE = row("add", 9e6, txn="0xjit"), row("remove", -9e6, txn="0xjit")
ABSURD = row("remove", -2e10, m=OTHER)
STRAY_ADD = row("add", 400_000.0, m=OTHER)


def test_sweep_ranks_non_jit_removals_by_size_and_keeps_the_jit_pairs_aside(capsys):
    client = FakeClient(
        sweep_routes(
            **{UNI: [SMALL, JIT_ADD, STRAY_ADD, JIT_REMOVE, HERO_REMOVE, ABSURD], AERO: []}
        )
    )
    candidates, jit_pairs, per_token = seed.sweep(client, WATCH[:2])
    assert [c["row"] for c in candidates] == [HERO_REMOVE, SMALL]
    assert candidates[0]["sym"] == "UNI" and candidates[0]["platform"] == "ethereum"
    assert len(jit_pairs) == 1
    assert jit_pairs[0]["txn"] == "0xjit" and jit_pairs[0]["usd"] == 9e6
    assert jit_pairs[0]["rows"] == [JIT_ADD, JIT_REMOVE]
    uni, aero = per_token
    assert uni["rows"] == 6 and uni["jit_txns"] == 1 and uni["implausible_rows"] == 1
    assert uni["removals"] == 2 and uni["largest_removal_usd"] == forwarding.usd(HERO_REMOVE)
    assert uni["span_h"] == 1.0
    assert aero == {
        "sym": "AERO",
        "platform": "base",
        "address": AERO,
        "rows": 0,
        "span_h": 0,
        "jit_txns": 0,
        "implausible_rows": 0,
        "removals": 0,
        "largest_removal_usd": 0,
    }
    out = capsys.readouterr().out
    assert (
        "UNI    ethereum    6 rows ·    1.0 h ·  1 JIT txns ·  1 implausible ·   2 removals" in out
    )
    assert "AERO   base        0 rows" in out


def test_sweep_records_a_throttled_token_and_moves_on(capsys):
    client = FakeClient([(lc(address=UNI), THROTTLED), *sweep_routes(**{CAKE: [SMALL]})])
    candidates, jit_pairs, per_token = seed.sweep(client, [WATCH[0], WATCH[2]])
    assert [c["sym"] for c in candidates] == ["CAKE"]
    assert per_token[0] == {"sym": "UNI", "platform": "ethereum", "error": THROTTLED["_err"]}
    assert per_token[1]["removals"] == 1
    assert "UNI    ethereum  error — HTTP 429" in capsys.readouterr().out


def test_capture_returns_the_receipt_and_the_verdict_with_the_seed_extras(engine, capsys):
    engine({HERO_REMOVE["txn"]: verdict("EXIT", HERO_REMOVE)})
    client = FakeClient([], keep_bodies=True)
    out, v = seed.capture(candidate(HERO_REMOVE), client, "the rule", "hero")
    assert v.kind == "EXIT"
    assert out["receipt"] == "hero" and out["sym"] == "UNI"
    assert out["min_usd_sweep"] == 100_000 and out["selection_rule"] == "the rule"
    assert out["verdict"]["removal"] == HERO_REMOVE
    printed = capsys.readouterr().out
    assert "hero: UNI −$2,921,711 Uniswap v3 (Ethereum) UNI/USDC" in printed
    assert "→ EXIT · nothing re-added" in printed


def test_capture_returns_none_when_the_engine_refuses(engine, capsys):
    engine({HERO_REMOVE["txn"]: forwarding.NoCandidate("below the pool share floor")})
    assert seed.capture(candidate(HERO_REMOVE), FakeClient([]), "rule", "hero") is None
    assert "refused: below the pool share floor" in capsys.readouterr().out


def test_capture_returns_none_when_the_engine_is_throttled(engine, capsys):
    engine({HERO_REMOVE["txn"]: forwarding.Throttled("HTTP 429")})
    assert seed.capture(candidate(HERO_REMOVE), FakeClient([]), "rule", "hero") is None
    assert "throttled: HTTP 429" in capsys.readouterr().out


def follow_client(adds):
    return FakeClient([(lc(address=UNI, maker=MAKER), envelope(adds))])


def test_quick_class_calls_a_same_pool_re_add_a_rebalance():
    later = T0 + 60_000
    kind, share = seed.quick_class(
        follow_client([row("add", 2.4e6, ts=later)]), candidate(HERO_REMOVE)
    )
    assert kind == "rebalance" and share == pytest.approx(2.4e6 / 2921711.1957615116)


def test_quick_class_calls_a_different_pool_re_add_a_migration():
    later = T0 + 60_000
    adds = [row("add", 2.4e6, ts=later, t1a=WETH, t1s="WETH")]
    kind, share = seed.quick_class(follow_client(adds), candidate(HERO_REMOVE))
    assert kind == "migration" and share == pytest.approx(2.4e6 / 2921711.1957615116)


def test_quick_class_calls_a_small_re_add_across_pools_partial():
    later = T0 + 60_000
    adds = [row("add", 200_000.0, ts=later), row("add", 200_000.0, ts=later, t1a=WETH)]
    kind, share = seed.quick_class(follow_client(adds), candidate(HERO_REMOVE))
    assert kind == "partial" and share == pytest.approx(400_000 / 2921711.1957615116)


def test_quick_class_sees_nothing_when_the_wallet_only_removed():
    kind, share = seed.quick_class(follow_client([HERO_REMOVE]), candidate(HERO_REMOVE))
    assert (kind, share) == ("nothing-same-chain", 0.0)


def test_quick_class_is_unknown_when_the_follow_is_throttled():
    client = FakeClient([(lc(address=UNI, maker=MAKER), THROTTLED)])
    assert seed.quick_class(client, candidate(HERO_REMOVE)) == ("unknown", 0.0)


def test_write_creates_the_proof_folder_and_prints_the_size(proof, capsys):
    seed.write("hero", {"b": 1, "a": [2]})
    assert json.loads((proof / "hero.json").read_text()) == {"a": [2], "b": 1}
    assert (proof / "hero.json").read_text().startswith('{\n "a"')
    assert "wrote docs/proof/hero.json (" in capsys.readouterr().out


RUNNER = row("remove", -2_000_000.0, ts=T0 + 1_000, m=OTHER)
THIRD = row("remove", -1_500_000.0, ts=T0 + 2_000, m="0x3333")
FOURTH = row("remove", -1_200_000.0, ts=T0 + 3_000, m="0x4444")
FIFTH = row("remove", -1_000_000.0, ts=T0 + 4_000, m="0x5555")
SIXTH = row("remove", -900_000.0, ts=T0 + 5_000, m="0x6666")


def probe(maker, adds):
    return (lc(address=UNI, maker=maker), envelope(adds))


def test_main_seeds_every_receipt_from_one_sweep(proof, routed, engine, argv, capsys):
    rows = [SMALL, JIT_ADD, JIT_REMOVE, HERO_REMOVE, RUNNER, THIRD, FOURTH, FIFTH]
    clients = routed(
        [
            *sweep_routes(**{UNI: rows, AERO: [], CAKE: []}),
            probe("0x3333", [THIRD]),
            probe("0x4444", [THIRD, row("add", 1.1e6, ts=T0 + 3_500, m="0x4444")]),
        ]
    )
    calls = engine(
        {
            HERO_REMOVE["txn"]: verdict("MIGRATION", HERO_REMOVE),
            RUNNER["txn"]: verdict("PARTIAL", RUNNER),
            THIRD["txn"]: verdict("EXIT", THIRD),
            FOURTH["txn"]: verdict("REBALANCE", FOURTH),
            "0xjit": forwarding.NoCandidate("the transaction both adds and removes"),
        }
    )
    argv()
    seed.main()
    written = sorted(p.name for p in proof.iterdir())
    assert written == [
        "exit.json",
        "hero.json",
        "jit.json",
        "rebalance.json",
        "runner_up.json",
        "seed_sweep.json",
    ]
    assert [c[2] for c in calls] == [
        HERO_REMOVE["txn"],
        RUNNER["txn"],
        THIRD["txn"],
        FOURTH["txn"],
        "0xjit",
    ]
    assert calls[-1][3] is None and calls[0][3] == MAKER

    sweep = json.loads((proof / "seed_sweep.json").read_text())
    assert [c["usd"] for c in sweep["candidates"]] == [
        2921711.1957615116,
        2e6,
        1.5e6,
        1.2e6,
        1e6,
        120_000.0,
    ]
    assert sweep["candidates"][0]["maker"] == MAKER and sweep["candidates"][0]["venue"].startswith(
        "Uniswap"
    )
    assert sweep["watchlist"] == [list(t) for t in WATCH]
    assert sweep["jit_pairs"][0]["txn"] == "0xjit" and len(sweep["jit_pairs"][0]["rows"]) == 2
    assert sweep["rule"].startswith("3 pages of 100 rows per token at minVolume=100000")
    assert len(sweep["calls"]) == 3
    assert len(sweep["responses"]) == len({c["sha256"] for c in sweep["calls"]}) == 2
    assert [t["sym"] for t in sweep["per_token"]] == ["UNI", "AERO", "CAKE"]

    hero = json.loads((proof / "hero.json").read_text())
    assert hero["receipt"] == "hero" and hero["verdict"]["kind"] == "MIGRATION"
    assert hero["selection_rule"].startswith("argmax |tu| over non-JIT removals >= $100,000")
    runner_up = json.loads((proof / "runner_up.json").read_text())
    assert runner_up["verdict"]["removal"] == RUNNER
    assert runner_up["selection_rule"] == "the second-largest non-JIT removal in the same sweep"
    exit_ = json.loads((proof / "exit.json").read_text())
    assert exit_["verdict"]["kind"] == "EXIT" and exit_["verdict"]["removal"] == THIRD
    rebalance = json.loads((proof / "rebalance.json").read_text())
    assert rebalance["verdict"]["kind"] == "REBALANCE" and rebalance["verdict"]["removal"] == FOURTH

    jit = json.loads((proof / "jit.json").read_text())
    assert jit["refused"] == "the transaction both adds and removes"
    assert jit["txn"] == "0xjit" and jit["usd"] == 9e6 and jit["rows"] == [JIT_ADD, JIT_REMOVE]
    assert jit["credits_used"] == 0 and jit["calls_made"] == 0
    assert jit["auth"] == "none — CoinMarketCap keyless /public-api surface"

    assert len(clients) == 8  # sweeper, hero, runner-up, two probes, two captures, jit
    out = capsys.readouterr().out
    assert "seed — keyless · 3 tokens · minVolume=100,000" in out
    assert "probe UNI    −$ 1,500,000" in out and "→ nothing-same-chain (0%)" in out
    assert "→ rebalance (92%)" in out
    assert "jit: UNI $9,000,000 txn 0xjit (2 rows in one transaction)" in out
    assert "→ refused: the transaction both adds and removes" in out
    assert "no EXIT found" not in out and "no REBALANCE found" not in out
    assert "credits · keyless" in out


def test_main_with_only_hero_and_runner_up_writes_nothing_when_both_captures_fail(
    proof, routed, engine, argv, capsys
):
    routed(sweep_routes(**{UNI: [HERO_REMOVE, RUNNER], AERO: [], CAKE: []}))
    calls = engine(
        {
            HERO_REMOVE["txn"]: forwarding.Throttled("HTTP 429"),
            RUNNER["txn"]: forwarding.NoCandidate("below the pool share floor"),
        }
    )
    argv("--only", "hero", "runner_up")
    seed.main()
    assert [p.name for p in proof.iterdir()] == ["seed_sweep.json"]
    assert [c[2] for c in calls] == [HERO_REMOVE["txn"], RUNNER["txn"]]
    out = capsys.readouterr().out
    assert "throttled: HTTP 429" in out and "refused: below the pool share floor" in out
    assert "probe" not in out and "jit:" not in out


def test_main_with_only_runner_up_and_a_single_candidate_captures_nothing(
    proof, routed, engine, argv, capsys
):
    routed(sweep_routes(**{UNI: [HERO_REMOVE], AERO: [], CAKE: []}))
    calls = engine({})
    argv("--only", "runner_up")
    seed.main()
    assert [p.name for p in proof.iterdir()] == ["seed_sweep.json"]
    assert calls == [] and "runner_up:" not in capsys.readouterr().out


def test_main_exits_when_the_sweep_finds_no_non_jit_removal(proof, routed, engine, argv):
    routed(sweep_routes(**{UNI: [JIT_ADD, JIT_REMOVE, STRAY_ADD], AERO: [], CAKE: []}))
    calls = engine({})
    argv()
    with pytest.raises(SystemExit) as e:
        seed.main()
    assert str(e.value) == "the sweep found no non-JIT removal — nothing to seed"
    assert [p.name for p in proof.iterdir()] == ["seed_sweep.json"]
    assert calls == []


def test_main_keeps_looking_past_changed_and_throttled_probes_then_gives_up(
    proof, routed, engine, argv, capsys
):
    rows = [HERO_REMOVE, RUNNER, THIRD, FOURTH, FIFTH, SIXTH]
    same_pool_add = row("add", 950_000.0, ts=T0 + 4_500, m="0x5555")
    routed(
        [
            *sweep_routes(**{UNI: rows, AERO: [], CAKE: []}),
            probe("0x3333", [THIRD]),
            probe("0x4444", [FOURTH]),
            probe("0x5555", [FIFTH, same_pool_add]),
            probe("0x6666", [SIXTH, row("add", 200_000.0, ts=T0 + 5_500, m="0x6666")]),
        ]
    )
    calls = engine(
        {
            THIRD["txn"]: verdict("PARTIAL", THIRD),
            FOURTH["txn"]: forwarding.Throttled("HTTP 429"),
            FIFTH["txn"]: verdict("EXIT", FIFTH, follows=2),
        }
    )
    argv("--only", "exit", "rebalance", "--max-candidates", "4")
    seed.main()
    assert [p.name for p in proof.iterdir()] == ["seed_sweep.json"]
    assert [c[2] for c in calls] == [THIRD["txn"], FOURTH["txn"], FIFTH["txn"]]
    out = capsys.readouterr().out
    assert "(cross-chain changed it to PARTIAL; keep looking)" in out
    assert "throttled: HTTP 429" in out
    assert "→ rebalance (95%)" in out and "→ partial (22%)" in out
    assert "no EXIT found among the top 4 candidates" in out
    assert "no REBALANCE found among the top 4 candidates" in out


def test_main_with_only_exit_stops_probing_once_the_exit_is_captured(
    proof, routed, engine, argv, capsys
):
    routed(
        [
            *sweep_routes(**{UNI: [HERO_REMOVE, RUNNER, THIRD, FOURTH], AERO: [], CAKE: []}),
            probe("0x3333", [THIRD]),
        ]
    )
    calls = engine({THIRD["txn"]: verdict("EXIT", THIRD)})
    argv("--only", "exit")
    seed.main()
    assert sorted(p.name for p in proof.iterdir()) == ["exit.json", "seed_sweep.json"]
    assert [c[2] for c in calls] == [THIRD["txn"]]
    out = capsys.readouterr().out
    assert out.count("probe ") == 1 and "no EXIT found" not in out


def test_main_with_only_rebalance_skips_a_candidate_whose_full_follow_disagrees(
    proof, routed, engine, argv, capsys
):
    add = row("add", 1.1e6, ts=T0 + 2_500, m="0x3333")
    routed(
        [
            *sweep_routes(**{UNI: [HERO_REMOVE, RUNNER, THIRD], AERO: [], CAKE: []}),
            probe("0x3333", [THIRD, add]),
        ]
    )
    engine({THIRD["txn"]: verdict("MIGRATION", THIRD)})
    argv("--only", "rebalance")
    seed.main()
    assert [p.name for p in proof.iterdir()] == ["seed_sweep.json"]
    assert "no REBALANCE found among the top 12 candidates" in capsys.readouterr().out


def test_main_with_only_jit_records_a_throttled_refusal_as_no_refusal(
    proof, routed, engine, argv, capsys
):
    routed(sweep_routes(**{UNI: [HERO_REMOVE, JIT_ADD, JIT_REMOVE], AERO: [], CAKE: []}))
    engine({"0xjit": forwarding.Throttled("HTTP 429")})
    argv("--only", "jit")
    seed.main()
    jit = json.loads((proof / "jit.json").read_text())
    assert jit["refused"] is None and jit["sym"] == "UNI" and jit["platform"] == "ethereum"
    assert "throttled: HTTP 429" in capsys.readouterr().out


def test_main_with_only_jit_records_an_engine_that_did_not_refuse(proof, routed, engine, argv):
    routed(sweep_routes(**{UNI: [HERO_REMOVE, JIT_ADD, JIT_REMOVE], AERO: [], CAKE: []}))
    engine({"0xjit": verdict("PARTIAL", JIT_REMOVE)})
    argv("--only", "jit")
    seed.main()
    jit = json.loads((proof / "jit.json").read_text())
    assert jit["refused"] is None and jit["address"] == UNI
    assert jit["selection_rule"].startswith("the largest same-transaction add+remove pair")


def test_the_script_entry_point_runs_main(argv):
    argv("--only", "bogus")
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(SEED_PY), run_name="__main__")
    assert e.value.code == 2
