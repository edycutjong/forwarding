"""The command line a judge runs: exit codes, the receipt file, and what gets printed."""

import json

import forwarding
import pytest
from conftest import (
    HERO_ADD,
    HERO_REMOVE,
    MAKER,
    THROTTLED,
    UNI,
    FakeClient,
    envelope,
    ep,
    lc,
    row,
    scenario,
)


@pytest.fixture
def routed(monkeypatch):
    """Point forwarding.Client at a routed fake for the duration of one main() call."""

    def install(routes):
        made = []

        def factory(**kw):
            kw.pop("spacing", None)
            c = FakeClient(routes, **kw)
            made.append(c)
            return c

        monkeypatch.setattr(forwarding, "Client", factory)
        return made

    return install


def test_investigate_prints_the_verdict_and_exits_zero(routed, capsys):
    routed(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    code = forwarding.main(["investigate", "--platform", "ethereum", "--address", UNI])
    out = capsys.readouterr().out
    assert code == 0
    assert "forwarding address — keyless" in out
    assert "● LP REMOVED  −$2,921,711" in out
    assert "◆ MIGRATION · severity amber" in out
    assert "99.9% recovered — $2,918,988 of $2,921,711" in out
    assert "2,918,987.61 ÷ 2,921,711.20 = 0.9991" in out
    assert "0 credits" in out and "keyless" in out
    assert "GET /v1/dex/liquidity-change/list" in out  # the trace is printed


def test_the_json_flag_writes_the_receipt_with_rows_calls_and_responses(routed, tmp_path):
    routed(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    out = tmp_path / "run.json"
    code = forwarding.main(["--json", str(out), "investigate", "--address", UNI])
    assert code == 0
    r = json.loads(out.read_text())
    assert r["verdict"]["kind"] == "MIGRATION"
    assert r["verdict"]["removal"] == HERO_REMOVE
    assert r["selection_rule"].startswith("largest non-JIT removal ≥ $100,000")
    assert r["credits_used"] == 0
    assert len(r["responses"]) == len({c["sha256"] for c in r["calls"]})


def test_no_qualifying_removal_exits_three(routed, capsys):
    routed(scenario(trigger_rows=[row("add", 500_000.0)], maker_rows=[]))
    code = forwarding.main(["--quiet", "investigate", "--address", UNI])
    assert code == 3
    assert "no verdict — no non-JIT removal" in capsys.readouterr().out


def test_a_throttled_trigger_exits_seventy_five_with_advice(routed, capsys):
    routed([(ep("/v1/dex/token"), {"data": {}}), (lc(), THROTTLED)])
    code = forwarding.main(["--quiet", "investigate", "--address", UNI])
    err = capsys.readouterr().err
    assert code == 75
    assert "throttled the anonymous tier" in err
    assert "wait a minute and re-run" in err
    assert "escape hatch, never a requirement" in err


def test_quiet_suppresses_the_trace_but_not_the_verdict(routed, capsys):
    routed(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    forwarding.main(["--quiet", "investigate", "--address", UNI])
    out = capsys.readouterr().out
    assert "GET /v1/dex" not in out and "◆ MIGRATION" in out


def test_txn_and_maker_flags_reach_the_engine(routed, capsys):
    routed(scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE]))
    code = forwarding.main(
        ["investigate", "--address", UNI, "--txn", HERO_REMOVE["txn"], "--maker", MAKER]
    )
    assert code == 0 and "◆ MIGRATION" in capsys.readouterr().out


def test_an_exit_verdict_says_where_it_looked(routed, capsys):
    routed(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_REMOVE]))
    code = forwarding.main(["--quiet", "investigate", "--address", UNI])
    out = capsys.readouterr().out
    assert code == 0
    assert "◆ EXIT · severity red" in out
    assert "nothing re-added by 0xc3da…5e56 within ±6 h on ethereum" in out


def test_removals_lists_non_jit_removals_largest_first_with_share_of_pool(routed, capsys):
    jit_a, jit_r = row("add", 9e6, txn="0xjit"), row("remove", -9e6, txn="0xjit")
    small = row("remove", -120_000.0, ts=HERO_REMOVE["ts"], m="0xaaaa")
    routed(scenario(trigger_rows=[small, jit_a, jit_r, HERO_REMOVE], maker_rows=[]))
    code = forwarding.main(["removals", "--address", UNI])
    out = capsys.readouterr().out
    assert code == 0
    assert "1 JIT transactions discarded · 2 removals" in out
    lines = [ln for ln in out.splitlines() if "−$" in ln]
    assert "2,921,711" in lines[0] and "120,000" in lines[1]
    assert "68.0% of pool" in lines[0] or "67.8% of pool" in lines[0]


def test_removals_with_nothing_qualifying_exits_three(routed, capsys):
    routed(scenario(trigger_rows=[], maker_rows=[]))
    assert forwarding.main(["removals", "--address", UNI]) == 3


def test_follow_lists_a_wallets_events_oldest_first_and_marks_jit(routed, capsys):
    jit_a, jit_r = row("add", 5e5, txn="0xjit", ts=1), row("remove", -5e5, txn="0xjit", ts=1)
    routed(scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE, jit_a, jit_r]))
    code = forwarding.main(["follow", "--address", UNI, "--maker", MAKER])
    out = capsys.readouterr().out
    assert code == 0
    assert "4 events by this wallet · 1 JIT transactions marked" in out
    lines = [ln for ln in out.splitlines() if "$" in ln and "UTC" in ln]
    assert "JIT" in lines[0] and "remove" in lines[2] and "add" in lines[3]


def test_watch_one_cycle_fires_on_the_newest_removal_and_prints_the_rewrite(
    routed, capsys, tmp_path
):
    routed(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    wl = _watchlist(tmp_path)
    code = forwarding.main(["--quiet", "watch", "--cycles", "1", "--watchlist", wl])
    out = capsys.readouterr().out
    assert code == 0
    assert "● LP REMOVED $2,921,711" in out
    assert "severity rewritten to amber" in out
    assert "1 verdict(s)" in out


def _watchlist(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps([{"platform": "ethereum", "address": UNI, "symbol": "UNI"}]))
    return str(path)


def test_no_subcommand_prints_help_and_exits_two(capsys):
    assert forwarding.main([]) == 2
    assert "investigate" in capsys.readouterr().out


def test_the_default_watchlist_is_eleven_tokens_on_three_chains():
    wl = forwarding.parse_watchlist(None)
    assert len(wl) == 11
    assert {p for p, _, _ in wl} == {"ethereum", "base", "bsc"}


def test_the_mode_line_names_a_keyed_run_so_it_can_never_pass_as_keyless(
    routed, monkeypatch, capsys
):
    routed(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    monkeypatch.setenv("CMC_API_KEY", "secret-key")
    forwarding.main(["--quiet", "investigate", "--address", UNI])
    out = capsys.readouterr().out
    assert "keyed via $CMC_API_KEY (escape hatch — the default is keyless)" in out
    assert "secret-key" not in out


def test_the_trace_line_shows_endpoint_status_ms_and_row_count(capsys):
    r = {
        "endpoint": "/v1/dex/liquidity-change/list",
        "params": {"platform": "ethereum", "address": UNI, "maker": MAKER, "limit": "100"},
        "status": 200,
        "ms": 315,
    }
    forwarding.print_trace_line(r, envelope([HERO_ADD, HERO_REMOVE]))
    line = capsys.readouterr().out
    assert "GET /v1/dex/liquidity-change/list" in line
    assert "maker=0xc3da47…1a5e56" in line and "limit" not in line
    assert " 200 " in line and "315 ms" in line and "2 rows" in line


def test_fmt_elapsed_reads_like_a_human_wrote_it():
    assert forwarding.fmt_elapsed(252) == "4 min 12 s"
    assert forwarding.fmt_elapsed(-90) == "−1 min 30 s"
    assert forwarding.fmt_elapsed(59) == "59 s"
    assert forwarding.fmt_elapsed(7 * 3600 + 120) == "7 h 02 min"
    assert forwarding.fmt_elapsed(None) == "—"
