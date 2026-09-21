"""The CI gate: every committed receipt replays, and a doctored one is refused."""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import verify

BUILD = Path(__file__).resolve().parents[1]
REAL_PROOF = BUILD / "docs" / "proof"


@pytest.fixture
def proof(monkeypatch, tmp_path):
    """An empty proof folder the script reads instead of docs/proof; returns a writer."""
    folder = tmp_path / "proof"
    folder.mkdir()
    monkeypatch.setattr(verify, "PROOF", folder)

    def put(name, doc):
        (folder / f"{name}.json").write_text(json.dumps(doc))
        return doc

    return put


@pytest.fixture
def receipt():
    """A deep copy of a committed receipt, safe to doctor."""

    def load(name):
        return copy.deepcopy(json.loads((REAL_PROOF / f"{name}.json").read_text()))

    return load


@pytest.fixture
def no_subprocess(monkeypatch):
    """Nothing the gate does may spawn a process."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(verify.subprocess, "run", run)
    return calls


def test_a_passing_check_prints_ok_and_records_nothing(capsys):
    failures = []
    verify.check(True, "fine", failures, "ignored detail")
    assert failures == []
    assert capsys.readouterr().out == "  ok   fine\n"


def test_a_failing_check_records_the_label_and_the_detail(capsys):
    failures = []
    verify.check(False, "bare", failures)
    verify.check(False, "with detail", failures, "why")
    assert failures == ["bare", "with detail: why"]
    assert capsys.readouterr().out == "  FAIL bare\n  FAIL with detail — why\n"


def test_rows_in_body_reads_lcs_and_tolerates_every_other_shape():
    assert verify.rows_in_body(None) == []
    assert verify.rows_in_body({"data": [1, 2]}) == []
    assert verify.rows_in_body({"data": {"tks": []}}) == []
    assert verify.rows_in_body({"data": {"lcs": [{"tp": "add"}]}}) == [{"tp": "add"}]


@pytest.mark.parametrize("name", verify.RECEIPTS)
def test_every_committed_receipt_replays_without_a_failure(name):
    failures = []
    d = verify.verify_receipt(name, failures)
    assert failures == []
    assert d["credits_used"] == 0


def test_the_committed_jit_refusal_and_base_rate_hold_up():
    failures = []
    verify.verify_jit(failures)
    verify.verify_base_rate(failures)
    assert failures == []


def test_a_receipt_whose_stored_verdict_disagrees_with_the_replay_fails(proof, receipt):
    d = receipt("hero")
    d["verdict"]["kind"] = "REBALANCE"
    d["verdict"]["recovered_share"] = 0.0
    proof("hero", d)
    failures = []
    verify.verify_receipt("hero", failures)
    assert "replay reproduces the verdict (REBALANCE): MIGRATION" in failures
    assert any(f.startswith("replay reproduces recovered share (0.000000): ") for f in failures)
    assert "I1 recovered_usd is the sum of the counted adds' tu" in failures


def test_an_incomplete_follow_on_an_exit_breaks_i6(proof, receipt):
    d = receipt("exit")
    d["verdict"]["follows"][0]["complete"] = False
    d["verdict"]["follows"][1]["reached_window_start"] = False
    proof("exit", d)
    failures = []
    verify.verify_receipt("exit", failures)
    assert "I6 an incomplete follow is never an EXIT" in failures
    assert "I6 every planned follow completed (200)" in failures
    assert "I6 every follow reached the window start" in failures


def test_a_migration_whose_destination_is_the_source_breaks_i3(proof, receipt):
    d = receipt("hero")
    v = d["verdict"]
    src = v["removal"]
    v["destination"]["identity"] = [
        v["token"]["platform"],
        src["f"],
        sorted([src["t0a"], src["t1a"]]),
    ]
    v["destination"]["liquidity_now_usd"] = 0
    proof("hero", d)
    failures = []
    verify.verify_receipt("hero", failures)
    assert "I3 the destination is not the source pool" in failures
    assert "I3 the destination's depth was confirmed live" in failures


def test_a_typed_in_evidence_row_and_a_lost_response_break_the_chain_of_custody(proof, receipt):
    d = receipt("hero")
    d["verdict"]["evidence"][-1]["row"]["tu"] += 1.0
    d["verdict"]["evidence"][-1]["row"]["m"] = "0xsomeoneelse"
    d["verdict"]["removal"]["h"] = "0"
    sha = d["calls"][0]["sha256"]
    d["responses"].pop(sha)
    d["responses"]["deadbeef"] = None
    d["calls"][1]["status"] = 429
    d["calls"] = [c for c in d["calls"] if c["endpoint"] != "/v1/dex/token/pools"]
    d["auth"] = "key — CMC_PRO_API_KEY"
    proof("hero", d)
    failures = []
    verify.verify_receipt("hero", failures)
    assert "every traced call's response is stored under its hash: 1 missing" in failures
    assert "every traced call returned 200: [429]" in failures
    assert "keyless: 0 credits, no key" in failures
    assert "every evidence row is verbatim inside a stored API response" in failures
    assert "the removal row is verbatim inside a stored API response" in failures
    assert "the join and the pool list were both called" in failures
    assert "I2 every evidence row carries the removal's maker" in failures


def test_a_jit_pair_that_was_adjudicated_instead_of_refused_fails(proof, receipt):
    d = receipt("jit")
    d["refused"] = ""
    d["rows"] = [d["rows"][0]]
    d["credits_used"] = 1
    proof("jit", d)
    failures = []
    verify.verify_jit(failures)
    assert failures == [
        "the JIT pair was refused, not adjudicated",
        "the pair adds and removes in one transaction",
        "keyless: 0 credits",
    ]


def test_a_missing_jit_receipt_is_itself_a_failure(proof):
    failures = []
    verify.verify_jit(failures)
    assert failures == ["jit.json exists"]


def test_a_base_rate_that_does_not_add_up_fails_every_arithmetic_check(proof, receipt):
    d = receipt("base_rate")
    d["n"] += 1
    d["partial"] = True
    d["put_back_share"] = 0.0
    d["rows"][0]["kind"] = "EXIT"
    d["rows"][0]["follow_error"] = "HTTP 429"
    d["rows"][1]["removed_usd"] = d["min_usd"] - 1
    d["credits_used"] = 3
    proof("base_rate", d)
    failures = []
    verify.verify_base_rate(failures)
    assert failures == [
        "the split adds up to n",
        "the run completed (not partial)",
        "put_back_share is the four classes over n",
        "I6 no EXIT row had a failed follow",
        "every row is ≥ min_usd",
        "keyless: 0 credits",
    ]


def test_a_missing_base_rate_is_reported_but_not_a_failure(proof, capsys):
    failures = []
    verify.verify_base_rate(failures)
    assert failures == []
    assert "not captured yet" in capsys.readouterr().out


def test_verify_site_runs_render_site_check_and_reports_its_output(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="index.html differs\n", stderr="")

    monkeypatch.setattr(verify.subprocess, "run", run)
    failures = []
    verify.verify_site(failures)
    assert seen[0][0] == sys.executable and seen[0][1:] == [
        str(verify.BUILD / "scripts" / "render_site.py"),
        "--check",
    ]
    assert len(failures) == 1 and failures[0].endswith("receipts render: index.html differs")


def test_main_passes_on_the_committed_receipts_and_checks_the_site(
    monkeypatch, no_subprocess, capsys
):
    monkeypatch.setattr(sys, "argv", ["verify.py", "--json"])
    assert verify.main() == 0
    out = capsys.readouterr().out
    assert no_subprocess and no_subprocess[0][-1] == "--check"
    assert '"ok": true' in out and out.rstrip().endswith("all checks passed")


def test_main_skips_the_site_check_when_asked(monkeypatch, no_subprocess):
    monkeypatch.setattr(sys, "argv", ["verify.py", "--no-site"])
    assert verify.main() == 0
    assert no_subprocess == []


def test_main_skips_the_site_check_when_render_site_is_absent(monkeypatch, no_subprocess, tmp_path):
    monkeypatch.setattr(sys, "argv", ["verify.py"])
    monkeypatch.setattr(verify, "BUILD", tmp_path)
    assert verify.main() == 0
    assert no_subprocess == []


def test_main_fails_when_every_receipt_is_missing(monkeypatch, proof, no_subprocess, capsys):
    monkeypatch.setattr(sys, "argv", ["verify.py", "--no-site"])
    assert verify.main() == 1
    out = capsys.readouterr().out
    assert "6 check(s) FAILED" in out
    assert all(f"FAIL {name}.json exists" in out for name in verify.RECEIPTS)


def test_the_script_entry_point_exits_with_the_gate_result(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["verify.py", "--no-site"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(BUILD / "scripts" / "verify.py"), run_name="__main__")
    assert exc.value.code == 0
