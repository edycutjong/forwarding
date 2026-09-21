"""scripts/bench.py offline: the percentile arithmetic, the replay over committed receipts, and
the live loop driven by a routed fake client."""

import json
import math
import runpy
import sys

import bench
import forwarding
import pytest
from conftest import (
    HERO_ADD,
    HERO_REMOVE,
    MAKER,
    THROTTLED,
    UNI,
    FakeClient,
    ep,
    lc,
    scenario,
)

HERO_VERDICT = {
    "kind": "MIGRATION",
    "recovered_share": 0.999,
    "removal": HERO_REMOVE,
    "evidence": [],
    "follows": [{"complete": True}],
    "destination": None,
    "token": {"platform": "ethereum", "address": UNI},
}


@pytest.fixture
def proof(tmp_path, monkeypatch):
    """A receipts folder holding only a hero whose removal the routed fake can serve."""
    (tmp_path / "hero.json").write_text(json.dumps({"verdict": HERO_VERDICT}))
    monkeypatch.setattr(bench, "PROOF", tmp_path)
    return tmp_path


@pytest.fixture
def routed(monkeypatch):
    """Hand bench.Client() one routed fake per iteration, in the order the routes are given."""

    def install(*route_sets):
        queue, made = list(route_sets), []

        def factory(**kw):
            routes = queue.pop(0) if len(queue) > 1 else queue[0]
            c = FakeClient(routes, **kw)
            made.append(c)
            return c

        monkeypatch.setattr(bench, "Client", factory)
        return made

    return install


@pytest.fixture
def argv(monkeypatch):
    def set_args(*args):
        monkeypatch.setattr(sys, "argv", ["bench.py", *args])

    return set_args


class BackedOffClient(FakeClient):
    """Answers the token header after three attempts, the way the real backoff records it."""

    def get(self, path, **params):
        if path == "/v1/dex/token":
            body = {"data": {"n": "Uniswap", "sym": "UNI", "liqUsd": "1"}}
            self._record(path, params, 200, 1, body=body, raw=json.dumps(body).encode(), attempts=3)
            return body
        return super().get(path, **params)


def test_percentile_of_nothing_is_nan():
    assert math.isnan(bench.pct([], 50))


def test_percentile_is_nearest_rank_so_every_value_is_a_real_observation():
    values = [5, 1, 4, 2, 3]
    assert bench.pct(values, 50) == 3
    assert bench.pct(values, 95) == 5
    assert bench.pct(values, 0) == 1
    assert bench.pct([7.5], 95) == 7.5


def test_report_prints_one_line_and_returns_the_rounded_summary(capsys):
    summary = bench.report("adjudicate (pure)", [0.12345, 0.5, 0.25], "ms")
    out = capsys.readouterr().out
    assert "adjudicate (pure)" in out and "n=3" in out and "p50" in out and "max" in out
    assert summary == {"n": 3, "p50": 0.25, "p95": 0.5, "max": 0.5, "unit": "ms"}


def test_replay_inputs_rebuilds_the_destination_pool_from_a_migration_receipt():
    removal, evidence, platform, complete, pools, verdict = bench.replay_inputs("hero")
    assert removal == verdict["removal"] and evidence == verdict["evidence"]
    assert platform == verdict["token"]["platform"] and complete is True
    dest = verdict["destination"]
    (key, pool), *rest = pools.items()
    assert not rest
    assert key == (dest["platform"], (dest["identity"][1], tuple(dest["identity"][2])))
    assert pool == {"addr": dest["addr"], "pubAt_ms": dest["pubAt_ms"], "fee_tiers": 1}


def test_replay_inputs_has_no_pools_when_the_receipt_had_no_destination():
    *_, pools, verdict = bench.replay_inputs("runner_up")
    assert verdict["destination"] is None and pools == {}


def test_replay_reproduces_every_committed_receipt_byte_for_byte(argv, tmp_path, capsys):
    out_path = tmp_path / "bench.json"
    argv("--replay", "--iterations", "3", "--json", str(out_path))
    bench.main()
    out = capsys.readouterr().out
    written = json.loads(out_path.read_text())
    assert "replay — adjudicate() over" in out and "no network" in out
    assert out.count("matches the receipt") == len(written["receipts"])
    assert written["mode"] == "replay" and written["iterations"] == 3
    total = 3 * len(written["receipts"])
    assert written["byte_identical"] == f"{total}/{total}"
    assert written["adjudicate"]["n"] == total and written["adjudicate"]["unit"] == "ms"
    assert f"wrote {out_path}" in out


def test_replay_without_receipts_says_to_run_seed(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "PROOF", tmp_path)
    argv("--replay")
    with pytest.raises(SystemExit, match="no receipts in docs/proof"):
        bench.main()


def test_live_times_each_iteration_keyless_and_writes_the_receipt(
    proof, routed, argv, tmp_path, capsys
):
    routes = scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE])
    made = routed(routes)
    out_path = tmp_path / "live.json"
    argv("--iterations", "2", "--json", str(out_path))
    bench.main()
    out = capsys.readouterr().out
    assert "live — keyless, 2 iterations on the hero removal" in out
    assert out.count("  iteration ") == 2 and "MIGRATION 99.9%" in out
    assert "0 throttle event(s) · 0 credits — keyless" in out
    written = json.loads(out_path.read_text())
    assert written["mode"] == "live" and written["throttle_events"] == 0
    assert written["credits_used"] == 0
    assert written["calls_per_investigation"] == len(made[0].calls) - 1  # the follow is timed apart
    assert written["investigate"]["n"] == written["follow"]["n"] == 2
    assert written["adjudicate"]["unit"] == "ms" and written["follow"]["unit"] == "s"


def test_live_with_a_key_reports_the_escape_hatch_and_bills_the_calls(
    proof, routed, argv, capsys, monkeypatch
):
    monkeypatch.setattr(bench, "escape_hatch_var", lambda: "CMC_API_KEY")
    made = routed(scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE]))
    argv("--iterations", "1")
    bench.main()
    out = capsys.readouterr().out
    assert "live — keyed via $CMC_API_KEY (escape hatch)" in out
    assert f"{len(made[0].calls) - 1} calls per investigation · 0 throttle event(s) · keyed" in out


def test_a_throttled_iteration_is_counted_and_skipped_not_timed(proof, routed, argv, capsys):
    throttled = [(ep("/v1/dex/token"), {"data": {}}), (lc(), THROTTLED)]
    routed(throttled, scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE]))
    argv("--iterations", "2")
    bench.main()
    out = capsys.readouterr().out
    assert "iteration 1: Throttled — HTTP 429" in out
    assert "iteration 2: MIGRATION" in out
    assert "1 throttle event(s)" in out


def test_a_missing_candidate_is_reported_by_its_exception_name(proof, routed, argv, capsys):
    other_removal = dict(HERO_REMOVE, m=MAKER, txn="0xnotthehero")
    routed(
        scenario(trigger_rows=[], maker_rows=[other_removal]),
        scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE]),
    )
    argv("--iterations", "2")
    bench.main()
    out = capsys.readouterr().out
    assert "iteration 1: NoCandidate — txn" in out and "iteration 2: MIGRATION" in out


def test_backoff_attempts_and_failed_statuses_count_as_throttle_events(
    proof, argv, capsys, monkeypatch
):
    failing_token = {"_err": "HTTP 500 upstream", "_throttled": False, "_status": 500}
    good = scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE])
    backed_off = [BackedOffClient(good)]
    failed = [FakeClient([(ep("/v1/dex/token"), failing_token), *good])]
    clients = backed_off + failed
    monkeypatch.setattr(bench, "Client", lambda **kw: clients.pop(0))
    argv("--iterations", "2")
    bench.main()
    out = capsys.readouterr().out
    assert out.count("MIGRATION") == 2
    assert "3 throttle event(s)" in out


def test_every_iteration_failing_exits_with_the_rate_limit_message(proof, routed, argv):
    routed([(ep("/v1/dex/token"), {"data": {}}), (lc(), THROTTLED)])
    argv("--iterations", "2")
    with pytest.raises(SystemExit, match="every iteration failed"):
        bench.main()


def test_the_bench_module_runs_as_a_program(argv, tmp_path, capsys):
    out_path = tmp_path / "main.json"
    argv("--replay", "--iterations", "1", "--json", str(out_path))
    runpy.run_path(bench.__file__, run_name="__main__")
    assert "byte-identical" in capsys.readouterr().out
    assert json.loads(out_path.read_text())["mode"] == "replay"


def test_the_fake_client_is_the_real_client_so_receipts_share_one_shape(proof, routed, argv):
    made = routed(scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE]))
    argv("--iterations", "1")
    bench.main()
    assert isinstance(made[0], forwarding.Client)
    assert {"endpoint", "status", "sha256", "utc"} <= set(made[0].calls[0])
