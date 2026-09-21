"""Every branch of the readiness scanner, offline, against a throwaway repository root."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD / "scripts"))
import check_submission_readiness as csr  # noqa: E402

SCRIPT = BUILD / "scripts" / "check_submission_readiness.py"


class Completed:
    def __init__(self, stdout):
        self.stdout = stdout


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A fake repository root the scanner reads from, with pytest collection stubbed out."""
    monkeypatch.setattr(csr, "ROOT", tmp_path)
    monkeypatch.setattr(csr.subprocess, "run", lambda *a, **k: Completed("7 tests collected"))
    return tmp_path


@pytest.fixture
def hero(root):
    """Write docs/proof/hero.json with the given verdict fields and return the path."""

    def write(**verdict):
        path = root / "docs" / "proof" / "hero.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"verdict": {"recovered_share": 0.9991, **verdict}}))
        return path

    return write


def test_the_scanner_reads_the_named_files_and_both_globs_in_sorted_order(root):
    (root / "README.md").write_text("clean\n")
    (root / "DEMO.md").write_text("clean\n")
    (root / "docs").mkdir()
    (root / "docs" / "b.md").write_text("clean\n")
    (root / "docs" / "a.md").write_text("clean\n")
    (root / ".github").mkdir()
    (root / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("clean\n")
    scanned, findings = csr.scan()
    assert scanned == [
        ".github/PULL_REQUEST_TEMPLATE.md",
        "DEMO.md",
        "README.md",
        "docs/a.md",
        "docs/b.md",
    ]
    assert findings == []


def test_a_line_that_talks_about_placeholders_is_never_a_finding(root):
    (root / "README.md").write_text("the scanner rejects a placeholder like 0x... and TODO\n")
    _, findings = csr.scan()
    assert findings == []


@pytest.mark.parametrize(
    "line, kind",
    [
        ("contract at 0x...", "unfilled address"),
        ("https://youtu.be/xxx", "placeholder video"),
        ("https://YOUTU.BE/your-video", "placeholder video"),
        ("https://youtube.com/watch?v=VIDEO_ID", "placeholder video"),
        ("FIXME later", "TODO marker"),
        ("[[FILL]] me", "unreplaced template token"),
        ("{{project.name}}", "unreplaced template token"),
        ("[Project Name]", "unreplaced template token"),
        ("github.com/OWNER/REPO", "unreplaced template token"),
        ("[your-handle]", "unreplaced template token"),
        ("https://[domain]", "placeholder URL"),
        ("https://example.com/your-app", "placeholder URL"),
        ("run with MOCK=1", "mock in the demo path"),
        ("offline=true", "mock in the demo path"),
    ],
)
def test_each_pattern_is_caught_with_its_label(root, line, kind):
    (root / "JUDGE.md").write_text(f"{line}\n")
    _, findings = csr.scan()
    assert findings == [{"file": "JUDGE.md", "line": 1, "kind": kind, "text": line}]


def test_the_quoted_text_is_stripped_and_capped_at_110_characters(root):
    (root / "README.md").write_text("   TODO " + "x" * 200 + "   \n")
    _, findings = csr.scan()
    assert len(findings[0]["text"]) == 110
    assert findings[0]["text"].startswith("TODO x")


def test_the_case_sensitive_patterns_ignore_lowercase_todo(root):
    (root / "README.md").write_text("a todo list is fine\n")
    _, findings = csr.scan()
    assert findings == []


def test_collected_returns_zero_when_pytest_collects_nothing(monkeypatch):
    monkeypatch.setattr(csr.subprocess, "run", lambda *a, **k: Completed("no tests collected"))
    assert csr.collected("live") == 0


def test_collected_parses_the_count_from_a_deselected_summary(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return Completed("collected 12 items / 3 deselected / 9 selected\n9/12 tests collected")

    monkeypatch.setattr(csr.subprocess, "run", fake_run)
    assert csr.collected("not live") == 9
    assert seen["cmd"][-2:] == ["-m", "not live"]
    assert seen["kw"]["cwd"] == csr.ROOT


def test_collected_returns_none_when_the_summary_has_no_count(monkeypatch):
    monkeypatch.setattr(csr.subprocess, "run", lambda *a, **k: Completed("garbled"))
    assert csr.collected("not live") is None


def test_numbers_is_silent_without_a_readme_or_hero(root):
    assert csr.numbers() == []


def test_a_readme_stating_the_collected_count_is_not_stale(root):
    (root / "README.md").write_text("**7** offline tests\n")
    assert csr.numbers() == []


def test_a_readme_stating_a_different_count_is_stale(root):
    (root / "README.md").write_text("**9** tests\n")
    assert csr.numbers() == [
        {
            "file": "README.md",
            "line": 0,
            "kind": "stale test count",
            "text": "README says 9 tests, pytest collects 7",
        }
    ]


def test_an_unparseable_collection_never_flags_the_count(root, monkeypatch):
    monkeypatch.setattr(csr.subprocess, "run", lambda *a, **k: Completed("garbled"))
    (root / "README.md").write_text("**9** tests\n")
    assert csr.numbers() == []


def test_a_readme_without_a_stated_count_is_not_compared(root):
    (root / "README.md").write_text("no count here\n")
    assert csr.numbers() == []


def test_a_hero_without_a_readme_is_not_compared(root, hero):
    hero(removal_share_of_pool=None)
    assert csr.numbers() == []


def test_a_readme_missing_the_killer_number_is_stale(root, hero):
    hero(removal_share_of_pool=0.4)
    (root / "README.md").write_text("**7** tests and nothing about recovery\n")
    assert csr.numbers() == [
        {
            "file": "README.md",
            "line": 0,
            "kind": "stale killer number",
            "text": "hero.json says 99.9% recovered; the README never states it",
        }
    ]


def test_a_readme_stating_the_killer_number_with_a_known_pool_share_is_clean(root, hero):
    hero(removal_share_of_pool=0.4)
    (root / "README.md").write_text("**7** tests, 99.9% recovered\n")
    (root / "JUDGE.md").write_text("0.0% of the pool\n")
    assert csr.numbers() == []


def test_a_page_printing_a_pool_share_the_hero_does_not_hold_is_invented(root, hero):
    hero(removal_share_of_pool=None)
    (root / "README.md").write_text("**7** tests, 99.9% recovered\n")
    (root / "JUDGE.md").write_text("clean\n")
    (root / "site").mkdir()
    (root / "site" / "index.html").write_text("<p>was <b>0.0% of the pool</b></p>\n")
    assert csr.numbers() == [
        {
            "file": "site/index.html",
            "line": 0,
            "kind": "invented number",
            "text": "hero.json holds no share of pool; the page prints 0.0%",
        }
    ]


def test_a_page_printing_a_real_pool_share_is_not_invented(root, hero):
    hero()
    (root / "README.md").write_text("**7** tests, 99.9% recovered\n")
    (root / "JUDGE.md").write_text("42.0% of the pool\n")
    assert csr.numbers() == []


def test_main_prints_the_scanned_files_and_clean_verdict_and_exits_zero(root, monkeypatch, capsys):
    (root / "README.md").write_text("**7** tests\n")
    monkeypatch.setattr(sys, "argv", ["check_submission_readiness.py"])
    assert csr.main() == 0
    out = capsys.readouterr().out
    assert "scanned 1 judge-facing file(s)" in out
    assert "  · README.md" in out
    assert out.rstrip().endswith(
        "clean — no unfilled addresses, fake links, TODOs, tokens or stale numbers"
    )


def test_main_lists_every_finding_and_exits_one(root, monkeypatch, capsys):
    (root / "README.md").write_text("**9** tests\nTODO: finish\n")
    monkeypatch.setattr(sys, "argv", ["check_submission_readiness.py"])
    assert csr.main() == 1
    out = capsys.readouterr().out
    assert "2 finding(s) still in the submission:" in out
    assert "  README.md:2  [TODO marker]  TODO: finish" in out
    assert "  README.md:0  [stale test count]  README says 9 tests, pytest collects 7" in out


def test_the_json_flag_prints_a_machine_readable_report(root, monkeypatch, capsys):
    (root / "README.md").write_text("[[FILL]]\n")
    monkeypatch.setattr(sys, "argv", ["check_submission_readiness.py", "--json"])
    assert csr.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["scanned"] == ["README.md"]
    assert [f["kind"] for f in report["findings"]] == ["unreplaced template token"]


def test_running_the_script_as_a_program_exits_with_the_verdict(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_submission_readiness.py", "--json"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Completed("no tests collected"))
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    report = json.loads(capsys.readouterr().out)
    assert e.value.code == (1 if report["findings"] else 0)
    assert "README.md" in report["scanned"]
