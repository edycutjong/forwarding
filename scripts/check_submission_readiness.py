#!/usr/bin/env python3
"""Refuse to let a placeholder, a dead number, or a stale count reach a judge.

    python3 scripts/check_submission_readiness.py          # exit 1 on any finding
    python3 scripts/check_submission_readiness.py --json

Scans every judge-facing file for the things that quietly survive into a submission and
destroy it on sight — an unfilled address, a fake video link, a TODO, a template token — and
then checks two numbers that drift: the test count the README states must equal what pytest
collects, and the killer number the README states must be the one docs/proof/hero.json holds.

Exit 0 = clean. Exit 1 = something is not ready. Wired into `make check`.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TARGETS = ["README.md", "DEMO.md", "JUDGE.md", "FEEDBACK.md", "ARCHITECTURE.md"]
TARGET_GLOBS = ["docs/*.md", ".github/*.md"]

PATTERNS = [
    ("unfilled address", r"0x\.\.\."),
    ("placeholder video", r"youtu\.be/(xxx|your-video|VIDEO_ID)\b"),
    ("placeholder video", r"youtube\.com/watch\?v=(xxx|your-video|VIDEO_ID)\b"),
    ("TODO marker", r"\bTODO\b"),
    ("TODO marker", r"\bFIXME\b"),
    ("unreplaced template token", r"\[\[FILL\]\]"),
    ("unreplaced template token", r"\{\{[a-zA-Z0-9_.]+\}\}"),
    ("unreplaced template token", r"\[Project Name\]"),
    ("unreplaced template token", r"\bOWNER/REPO\b"),
    ("unreplaced template token", r"\[your-[a-z-]+\]"),
    ("placeholder URL", r"https?://\[[a-z-]+\]"),
    ("placeholder URL", r"example\.com/(your|placeholder)"),
    ("mock in the demo path", r"(?i)\b(MOCK|OFFLINE)=\w"),
]
SELF_REFERENTIAL = re.compile(r"placeholder|readiness|scanner|check_submission|template token")


def scan():
    files = [ROOT / t for t in TARGETS if (ROOT / t).exists()]
    for g in TARGET_GLOBS:
        files += sorted(ROOT.glob(g))
    findings = []
    for f in sorted(set(files)):
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if SELF_REFERENTIAL.search(line):
                continue
            for label, pat in PATTERNS:
                if re.search(pat, line, re.IGNORECASE if "youtu" in pat else 0):
                    findings.append(
                        {
                            "file": str(f.relative_to(ROOT)),
                            "line": n,
                            "kind": label,
                            "text": line.strip()[:110],
                        }
                    )
                    break
    return [str(f.relative_to(ROOT)) for f in sorted(set(files))], findings


def collected(marker):
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts=", "-m", marker],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    if "no tests collected" in r.stdout:
        return 0
    m = re.search(r"(\d+)(?:/\d+)? tests? collected", r.stdout)
    return int(m.group(1)) if m else None


def numbers():
    """The README's stated test count and killer number against what the repo holds."""
    findings = []
    readme = (ROOT / "README.md").read_text() if (ROOT / "README.md").exists() else ""
    offline = collected("not live")
    stated = re.search(r"\*\*(\d+)\*\* (?:offline )?tests", readme)
    if offline is not None and stated and int(stated.group(1)) != offline:
        findings.append(
            {
                "file": "README.md",
                "line": 0,
                "kind": "stale test count",
                "text": f"README says {stated.group(1)} tests, pytest collects {offline}",
            }
        )
    hero = ROOT / "docs" / "proof" / "hero.json"
    if hero.exists() and readme:
        v = json.loads(hero.read_text())["verdict"]
        pct = f"{v['recovered_share'] * 100:.1f}%"
        if pct not in readme:
            findings.append(
                {
                    "file": "README.md",
                    "line": 0,
                    "kind": "stale killer number",
                    "text": f"hero.json says {pct} recovered; the README never states it",
                }
            )
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    scanned, findings = scan()
    findings += numbers()
    if a.json:
        print(json.dumps({"scanned": scanned, "findings": findings}, indent=2))
    else:
        print(f"submission readiness — scanned {len(scanned)} judge-facing file(s)")
        for s in scanned:
            print(f"  · {s}")
        if findings:
            print(f"\n{len(findings)} finding(s) still in the submission:\n")
            for f in findings:
                print(f"  {f['file']}:{f['line']}  [{f['kind']}]  {f['text']}")
        else:
            print("\nclean — no unfilled addresses, fake links, TODOs, tokens or stale numbers")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
