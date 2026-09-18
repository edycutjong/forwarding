"""The numbers the judged surfaces state are the numbers the repository holds.

A README that says "111 tests" over a suite of 90 is a small lie a judge can check in ten
seconds. These tests make the published counts a function of the code, never a memory.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD / "scripts"))
import render_site  # noqa: E402
from test_property import PROPERTY_CASES  # noqa: E402


def collected(marker):
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts=", "-m", marker],
        capture_output=True,
        text=True,
        cwd=BUILD,
        timeout=120,
    )
    if "no tests collected" in r.stdout:
        return 0
    return int(re.search(r"(\d+)(?:/\d+)? tests? collected", r.stdout).group(1))


def test_the_property_case_count_the_pages_state_is_the_one_hypothesis_runs():
    assert render_site.PROPERTY_CASES == PROPERTY_CASES == 2000


@pytest.mark.parametrize("doc", ["README.md", "JUDGE.md", "DEMO.md"])
def test_the_test_count_each_judged_document_states_matches_pytest(doc):
    path = BUILD / doc
    if not path.exists():
        pytest.skip(f"{doc} not written yet")
    text = path.read_text()
    offline = collected("not live")
    stated = re.findall(r"\*\*(\d+)\*\*(?: offline)? tests", text)
    assert stated, f"{doc} states no test count"
    for s in stated:
        assert int(s) == offline, f"{doc} says {s} tests; pytest collects {offline}"


def test_the_readme_states_the_killer_number_from_the_hero_receipt():
    import json

    readme = BUILD / "README.md"
    hero = BUILD / "docs" / "proof" / "hero.json"
    if not readme.exists():
        pytest.skip("README not written yet")
    v = json.loads(hero.read_text())["verdict"]
    assert f"{v['recovered_share'] * 100:.1f}%" in readme.read_text()


def test_the_readiness_scanner_passes_on_this_repository():
    r = subprocess.run(
        [sys.executable, str(BUILD / "scripts" / "check_submission_readiness.py")],
        capture_output=True,
        text=True,
        cwd=BUILD,
        timeout=180,
    )
    assert r.returncode == 0, r.stdout


def test_the_readiness_scanner_can_fail(tmp_path, monkeypatch):
    import check_submission_readiness as csr

    (tmp_path / "README.md").write_text("Deployed at 0x... and the video is youtu.be/xxx\n")
    monkeypatch.setattr(csr, "ROOT", tmp_path)
    scanned, findings = csr.scan()
    assert scanned == ["README.md"]
    assert {f["kind"] for f in findings} == {"unfilled address"}  # first match per line wins
    (tmp_path / "README.md").write_text("TODO: finish\nhttps://youtu.be/VIDEO_ID\n")
    _, findings = csr.scan()
    assert [f["kind"] for f in findings] == ["TODO marker", "placeholder video"]
