#!/usr/bin/env python3
"""Render site/index.html, site/judge.html, site/pitch/index.html and JUDGE.md from the receipts.

    python3 scripts/render_site.py            # write site/index.html, site/judge.html, site/pitch/index.html, JUDGE.md
    python3 scripts/render_site.py --check    # exit 1 if what is on disk is not this render

Every number on either surface comes from docs/proof/*.json, which are real keyless runs. The
templates in scripts/site_templates/ use {{slot}} tokens and the render fails if any slot is
left unfilled, so a placeholder can never reach the committed HTML and a number can never be
typed in by hand. The HTML under site/ is generated output: edit the template or the receipt,
never the page. site/judge.html — the /judge route — is JUDGE.md itself, converted to HTML by
md_to_html() below, so the judge guide on GitHub and the one on the site cannot disagree.
site/pitch/index.html — the deck at /pitch — is the same contract: scripts/site_templates/pitch.html
with every figure a slot, so the deck, the landing page and the README cannot carry different numbers.
"""

import hashlib
import html
import io
import json
import re
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forwarding  # noqa: E402
from forwarding import fmt_elapsed, fmt_utc, pair, short, ts_ms, usd, venue  # noqa: E402

BUILD = Path(__file__).resolve().parents[1]
TEMPLATES = BUILD / "scripts" / "site_templates"
PROOF = BUILD / "docs" / "proof"
SITE = BUILD / "site"

REPO = "https://github.com/edycutjong/forwarding"
SITE_URL = "https://forwarding.edycu.dev"
# One host. Vercel serves site/ and the two functions at SITE_URL (the production domain);
# forwarding-cmc.vercel.app is the deployment alias and 308s here, so the live box is same-origin.
ALIAS_URL = "https://forwarding-cmc.vercel.app"
EVENT = "https://dorahacks.io/hackathon/coinmarketcap-api-202609/detail"
EVENT_BUIDLS = "https://dorahacks.io/hackathon/coinmarketcap-api-202609/buidl"
AUTHOR = "Edy Cu"
X_HANDLE = "@edycutjong"
OG_IMAGE = SITE / "assets" / "og-image.png"
ICON_ANIMATED = BUILD / "docs" / "assets" / "icon-animated.svg"
OG_SIZE = (1200, 630)
PROPERTY_CASES = 2000
CLI_CMD = (
    "python3 scripts/forwarding.py investigate --platform ethereum "
    "--address 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
)
MCP_CMD = "claude mcp add forwarding -- python3 $PWD/scripts/mcp_server.py"
CLONE_CMD = f"git clone {REPO}.git && cd forwarding && {CLI_CMD}"
# The version stamp in the landing footer and on the deck: `git describe --tags --abbrev=0`,
# never typed. A clone with no tag reachable (a shallow CI checkout) renders this fallback, and
# --check then leaves the stamp out of the comparison rather than reporting drift it cannot judge.
# `vercel --prod` runs from a full clone, so the deployed pages carry the tag `git describe` saw.
VERSION_FALLBACK = "v0.0.0-dev"
FEEDBACK_MD = BUILD / "FEEDBACK.md"
MCP_SESSION = PROOF / "mcp_session.jsonl"
# The committed receipts the landing page's switcher carries, in the order the pills appear:
# the sweep's largest first, then the event the mechanism was found on, then every other
# outcome the same published rule produced. Each is a real keyless run; none is picked by hand.
SWITCHER = ["hero", "uni_v3_v4", "rebalance", "runner_up", "exit", "jit"]
LIST_EP = "/v1/dex/liquidity-change/list"
# Every off-site link the script emits ends in this (LANDING_DESIGN.md 6.4) — the arrow the eye
# reads and the words a screen reader needs; the page script carries the same EXT for the links
# it re-renders on a token switch or a live run.
EXT = '<span class="arrow arrow-ext" aria-hidden="true">↗</span><span class="sr-only"> (opens in a new tab)</span>'

ENDPOINTS = [
    (
        "/v1/dex/liquidity-change/list",
        "the trigger (removals ≥ $100k) and the join: `maker=` returns one wallet's adds and removes across every pool of the token",
    ),
    (
        "/v1/dex/token/pools",
        "every pool of the token: identity, liquidity now (share-of-pool), pool address, creation time (Consolidation)",
    ),
    (
        "/v1/dex/search",
        "asset resolution: address → CoinMarketCap id, then the same id on every other chain",
    ),
    (
        "/v2/cryptocurrency/info",
        "the canonical cross-chain contract registry the search rows are checked against",
    ),
    (
        "/v4/dex/pairs/quotes/latest",
        "depth confirmation: the destination pool's liquidity now, and the source pool's",
    ),
    ("/v1/dex/token", "the card header: name, symbol, token liquidity"),
]
COLOURS = {
    "REBALANCE": "var(--rebalance)",
    "MIGRATION": "var(--migration)",
    "CONSOLIDATION": "var(--migration)",
    "PARTIAL": "var(--partial)",
    "EXIT": "var(--exit)",
    "INCOMPLETE": "#7A2E2E",
}


def load(name):
    p = PROOF / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def money(x, dp=0):
    return f"${x:,.{dp}f}"


def compact_money(x):
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:,.0f}"


def esc(s):
    return html.escape(str(s), quote=True)


def png_size(path):
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        sys.exit(f"{path.relative_to(BUILD)} is not a PNG")
    return struct.unpack(">II", head[16:24])


DESC_MAX = 125  # og:description ceiling; the <meta name=description> window is 50-160


def meta_description(v, r, dest, elapsed_s):
    """One sentence of the hero receipt plus the pitch, kept under the social-card ceiling.
    The venue is the first thing dropped when a long DEX name would push it over — every
    number stays."""
    pitch = " Follow the wallet — keyless, live, CoinMarketCap."
    pct = f"{v['recovered_share'] * 100:.1f}%"
    later = f"{fmt_elapsed(elapsed_s)} later"
    candidates = [
        f"{money(v['removed_usd'])} left {pair(r)} on {venue(r)}; the same wallet put {pct} into "
        f"{dest.get('venue', '')} {later}.",
        f"{money(v['removed_usd'])} left {pair(r)}; the same wallet re-added {pct} {later}.",
        f"{money(v['removed_usd'])} removed; the same wallet re-added {pct} {later}.",
    ]
    for lead in candidates:
        if len(lead + pitch) <= DESC_MAX:
            desc = lead + pitch
            break
    else:
        desc = candidates[-1] + pitch
    if not 50 <= len(desc) <= DESC_MAX:
        sys.exit(f"meta description is {len(desc)} chars; want 50-{DESC_MAX}")
    return desc


def og_meta(verdict):
    """The social-card tags, only when the image exists at exactly 1200x630 — never a dead link.
    The alt text is derived from the hero receipt like every other number on the page."""
    if not OG_IMAGE.exists():
        return ""
    w, h = png_size(OG_IMAGE)
    if (w, h) != OG_SIZE:
        sys.exit(f"og-image.png is {w}x{h}; the card must be exactly {OG_SIZE[0]}x{OG_SIZE[1]}")
    v = hashlib.sha1(OG_IMAGE.read_bytes()).hexdigest()[:8]
    url = f"{SITE_URL}/assets/og-image.png?v={v}"
    alt = (
        f"A red LP-removed alert becoming an {verdict['severity']} {verdict['kind']} verdict: "
        f"{verdict['recovered_share'] * 100:.1f}% recovered."
    )
    return (
        f'<meta property="og:image" content="{url}">\n'
        f'<meta property="og:image:width" content="{w}">\n'
        f'<meta property="og:image:height" content="{h}">\n'
        f'<meta property="og:image:alt" content="{esc(alt)}">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:image" content="{url}">'
    )


def json_panel(row, side):
    """A liquidity-change row as highlighted JSON, every field, nothing truncated."""
    order = [
        "ts",
        "tp",
        "tu",
        "m",
        "en",
        "eid",
        "f",
        "t0a",
        "t1a",
        "t0s",
        "t1s",
        "a0",
        "a1",
        "txn",
        "h",
        "lgid",
        "txId",
    ]
    keys = [k for k in order if k in row] + [k for k in row if k not in order]
    lines = []
    for k in keys:
        v = row[k]
        val = json.dumps(v, ensure_ascii=False)
        cls = None
        if k == "m":
            cls = "m"
        elif k == "tu":
            cls = "tu-r" if side == "remove" else "tu-a"
        elif k in ("tp", "en", "t1s", "ts", "a0", "txn"):
            cls = "hl"
        else:
            cls = "dim"
        lines.append(f'  <span class="k">"{esc(k)}"</span>: <span class="{cls}">{esc(val)}</span>')
    return "{\n" + ",\n".join(lines) + "\n}"


def trace_rows(calls):
    out = []
    for c in calls:
        params = " ".join(
            f"{k}={short(v, 6) if len(str(v)) > 24 else v}"
            for k, v in c["params"].items()
            if k != "limit"
        )
        sha = c.get("sha256") or ""
        credit = c.get("credit_count")
        out.append(
            '        <li><span class="ep">GET '
            f"{esc(c['endpoint'])} <span>{esc(params)}</span></span>"
            f'<span class="chip">{c["status"]}</span>'
            f'<span class="ms">{c["ms"]} ms</span>'
            f'<span class="cr">credit_count {credit if credit is not None else "–"} · 0 billed</span>'
            f'<span class="sha" data-short="{sha[:8]}" data-full="{sha}" title="sha256 of the bytes received">{sha[:8]}</span></li>'
        )
    return "\n".join(out)


def cover_icon():
    """The project's animated mark, inlined on the cover so the deck loads no extra asset. The
    SMIL loops are cut from indefinite to two: the story plays twice, then rests on the poster
    frame (frame 0 == the end state by the icon's own storyboard)."""
    if not ICON_ANIMATED.exists():
        sys.exit(f"{ICON_ANIMATED.relative_to(BUILD)} is missing — run scripts/sync-assets.sh")
    svg = ICON_ANIMATED.read_text()
    return svg.replace('repeatCount="indefinite"', 'repeatCount="2"')


def deck_trace(calls):
    """The live run's trace as the CLI prints it, one line per call: endpoint, the parameters
    that matter, status, ms. Long values (addresses, cursors) are shortened the way the CLI does."""
    out = []
    for c in calls:
        params = " ".join(
            f"{k}={short(v, 4) if len(str(v)) > 24 else v}"
            for k, v in c["params"].items()
            if k != "limit"
        )
        if len(params) > 50:  # the cursor lines: keep the column, mark the cut
            params = params[:49] + "…"
        out.append(
            f'<span class="dim">    GET </span><span class="m">{esc(c["endpoint"]):<30}</span>'
            f'<span class="dim">{esc(params):<52}</span><span class="ok">{c["status"]}</span>'
            f'<span class="dim">{c["ms"]:>6} ms</span>'
        )
    return "\n".join(out)


def base_ctx(b):
    if not b:
        return {
            "base.headline": "Base rate — not captured yet",
            "base.bar": "",
            "base.legend": "",
            "base.aria": "",
            "base.n": "0",
            "base.min_usd": "100,000",
            "base.tokens": "11",
            "base.captured": "—",
            "base.method": "—",
            "base.summary": "not captured yet",
            "base.put_back_pct": "—",
            "base.calls": "0",
        }
    n = b["n"]
    split = b["split"]
    order = ["REBALANCE", "MIGRATION", "CONSOLIDATION", "PARTIAL", "EXIT", "INCOMPLETE"]
    bar, legend, aria = [], [], []
    for k in order:
        c = split.get(k, 0)
        if not c:
            continue
        pct = c / n * 100
        bar.append(
            f'      <div style="width:{pct:.2f}%;background:{COLOURS[k]}" title="{k} {c} ({pct:.0f}%)"></div>'
        )
        legend.append(
            f'      <span><i style="background:{COLOURS[k]}"></i>{k.lower()} <b>{pct:.0f}%</b> ({c})</span>'
        )
        aria.append(f"{k.lower()} {pct:.0f}%")
    back = b["put_back_share"]
    summary = " · ".join(f"{k.lower()} {split.get(k, 0)}" for k in order if split.get(k, 0))
    return {
        "base.headline": (
            f"{back * 100:.0f}% of “LP pulled” alerts are the same wallet putting it back within 6 h"
        ),
        "base.bar": "\n".join(bar),
        "base.legend": "\n".join(legend),
        "base.aria": ", ".join(aria),
        "base.n": f"{n:,}",
        "base.min_usd": f"{b['min_usd']:,.0f}",
        "base.tokens": str(len(b["watchlist"])),
        "base.captured": b["captured_utc"].replace("T", " ").replace("Z", " UTC"),
        "base.method": esc(b["method"]),
        "base.summary": summary,
        "base.put_back_pct": f"{back * 100:.0f}",
        "base.calls": f"{b['calls_made']:,}",
    }


def mini_card(name, d):
    if not d:
        return ""
    if name == "jit":
        rows = d["rows"]
        add = next(r for r in rows if r["tp"] == "add")
        rem = next(r for r in rows if r["tp"] == "remove")
        return (
            '    <div class="mini grey"><h3 class="grey-t">Refused · JIT</h3>'
            f'<div class="n">{esc(money(d["usd"]))}</div>'
            f"<p><b>{esc(d['sym'])}</b> · +{esc(money(usd(add)))} and −{esc(money(usd(rem)))} in <b>one transaction</b> "
            f"({esc(venue(rem))}) — just-in-time liquidity, not an event.</p>"
            f"<p>Named by hash, the agent answers: <i>{esc(d['refused'])}</i></p>"
            f'<p><a href="{REPO}/blob/main/docs/proof/jit.json" target="_blank" rel="noopener noreferrer">jit.json{EXT}</a></p></div>'
        )
    v = d["verdict"]
    r = v["removal"]
    kind = v["kind"]
    cls = {
        "EXIT": "red",
        "REBALANCE": "grey",
        "MIGRATION": "amber",
        "CONSOLIDATION": "amber",
        "PARTIAL": "amber",
        "INCOMPLETE": "red",
    }[kind]
    tcls = {"red": "red-t", "grey": "grey-t", "amber": "amber-t"}[cls]
    if kind == "EXIT":
        chains = ", ".join(f["platform"] for f in v["follows"])
        body = (
            f"<p>−{esc(money(v['removed_usd']))} out of <b>{esc(venue(r))} · {esc(pair(r))}</b> "
            f"by <span class='addr'>{esc(short(r['m']))}</span>.</p>"
            f"<p>Nothing re-added by that wallet within ±{v['window_h']['back']:.0f} h on <b>{esc(chains)}</b> "
            f"— every follow completed (200), so this is a real Exit, stated with the window it searched.</p>"
        )
        n = "0%"
    elif kind == "REBALANCE":
        note = ""
        if v["adjudication"].get("other_removals_in_window"):
            note = (
                f" The wallet cycled again inside the window ({v['adjudication']['other_removals_in_window']} more removal(s)), "
                f"so the share is over all of them — disclosed, not hidden."
            )
        body = (
            f"<p>−{esc(money(v['removed_usd']))} out of <b>{esc(venue(r))} · {esc(pair(r))}</b> "
            f"by <span class='addr'>{esc(short(r['m']))}</span>, back into the <b>same pool</b> "
            f"<b>{esc(fmt_elapsed(v['elapsed_s']))}</b> later.{esc(note)}</p>"
        )
        n = f"{v['recovered_share'] * 100:.0f}%"
    else:
        dest = v.get("destination") or {}
        body = (
            f"<p>−{esc(money(v['removed_usd']))} out of <b>{esc(venue(r))} · {esc(pair(r))}</b>, "
            f"{esc(money(v['recovered_usd']))} into <b>{esc(dest.get('venue', ''))} · {esc(dest.get('pair', ''))}</b>.</p>"
        )
        n = f"{v['recovered_share'] * 100:.1f}%"
    return (
        f'    <div class="mini {cls}"><h3 class="{tcls}">{esc(kind)} · {esc(d["sym"])}</h3>'
        f'<div class="n">{n}<span style="font-size:.5em;color:var(--muted);font-weight:500"> recovered</span></div>'
        f"{body}"
        f"<p>captured {esc(d['captured_utc'].replace('T', ' ').replace('Z', ' UTC'))} · {d['calls_made']} calls · 0 credits · "
        f'<a href="{REPO}/blob/main/docs/proof/{name}.json" target="_blank" rel="noopener noreferrer">{name}.json{EXT}</a></p></div>'
    )


# ── JUDGE.md → /judge: a converter for exactly the Markdown the judge guide uses ─────────────

_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?![*\w])")


def _href(url):
    """Relative links on the GitHub page become absolute blob links on the site."""
    if url.startswith(("http://", "https://", "mailto:", "#")):
        return url
    return f"{REPO}/blob/main/{url.lstrip('./')}"


def _inline(text):
    """Escape, then bold / italic / links / code — code spans are protected first so the
    characters inside them are never read as markup."""
    text = html.escape(text, quote=False)
    spans = []

    def keep(m):
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = _INLINE_CODE.sub(keep, text)
    text = _LINK.sub(
        lambda m: (
            f'<a href="{_href(m.group(2))}"'
            + (
                ' target="_blank" rel="noopener noreferrer"'
                if _href(m.group(2)).startswith("http")
                else ""
            )
            + f">{m.group(1)}</a>"
        ),
        text,
    )
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{spans[int(m.group(1))]}</code>", text)


def _table(rows):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], cells[2:]  # cells[1] is the |---| separator
    out = ["<table>"]
    if any(head):
        out.append(
            "<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead>"
        )
    out.append("<tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def md_to_html(md):
    """Headings, paragraphs, fenced code, pipe tables, ordered and unordered lists with wrapped
    continuation lines, and the inline forms in _inline(). Anything else is a paragraph — the
    page is regenerated by `make site` and checked by `make check`, so a construct this does
    not know shows up as visible Markdown in review, never as silent loss."""
    out, para, i = [], [], 0
    lines = md.splitlines()

    def flush():
        if para:
            text = " ".join(x.strip() for x in para)
            cls = ' class="claim"' if text.startswith("**") and text.endswith("**") else ""
            out.append(f"<p{cls}>{_inline(text)}</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        if st.startswith("<!--") and st.endswith("-->"):
            i += 1
            continue
        if st.startswith("```"):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(block), quote=False) + "</code></pre>")
            continue
        if st.startswith("#"):
            flush()
            level = len(st) - len(st.lstrip("#"))
            out.append(f"<h{level}>{_inline(st[level:].strip())}</h{level}>")
            i += 1
            continue
        if st.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        m = re.match(r"^(\d+)\.\s+|^[-*]\s+", ln)
        if m:
            flush()
            tag = "ol" if ln[0].isdigit() else "ul"
            items = []
            while i < len(lines):
                cur = lines[i]
                mm = re.match(r"^(\d+)\.\s+|^[-*]\s+", cur)
                if mm and (cur[0].isdigit()) == (tag == "ol"):
                    items.append([cur[mm.end() :]])
                    i += 1
                elif cur.startswith("  ") and cur.strip() and items:
                    items[-1].append(cur.strip())
                    i += 1
                else:
                    break
            out.append(
                f"<{tag}>\n"
                + "\n".join(f"<li>{_inline(' '.join(it))}</li>" for it in items)
                + f"\n</{tag}>"
            )
            continue
        if not st:
            flush()
            i += 1
            continue
        para.append(ln)
        i += 1
    flush()
    return "\n".join(out)


def render(template, ctx):
    out = template
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", str(v))
    left = sorted(set(re.findall(r"\{\{([a-zA-Z0-9_.]+)\}\}", out)))
    if left:
        sys.exit(f"unfilled slots: {left}")
    return out


def test_count():
    """The offline and live test counts, from pytest itself — never typed."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts=", "-m", "not live"],
            capture_output=True,
            text=True,
            cwd=BUILD,
            timeout=120,
        )
        offline = _collected(r.stdout)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts=", "-m", "live"],
            capture_output=True,
            text=True,
            cwd=BUILD,
            timeout=120,
        )
        live = _collected(r.stdout)
        return offline, live
    except Exception as e:  # pytest missing on a bare judge machine: fall back to the receipt
        counts = load("tests")
        if counts:
            return counts["offline"], counts["live"]
        sys.exit(f"cannot count tests ({e}) and docs/proof/tests.json is absent")


def _collected(stdout):
    """pytest's summary line: '105 tests collected', '5/110 tests collected (105 deselected)',
    or 'no tests collected (105 deselected)'."""
    if re.search(r"no tests collected", stdout):
        return 0
    m = re.search(r"(\d+)(?:/\d+)? tests? collected", stdout)
    if not m:
        raise ValueError(f"unrecognised pytest summary: {stdout[-200:]!r}")
    return int(m.group(1))


# ── The landing page (scripts/site_templates/landing.html) — the family skeleton of
#    LANDING_DESIGN.md with this project's own truth in every slot ──────────────────────────


def version():
    """`git describe --tags --abbrev=0`, or the fallback when no tag is reachable."""
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            cwd=BUILD,
            timeout=10,
        )
        tag = r.stdout.strip()
        if r.returncode == 0 and re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            return tag
    except Exception:  # noqa: BLE001 — no git on the machine: the fallback is the honest answer
        pass
    return VERSION_FALLBACK


def og_version():
    return hashlib.sha1(OG_IMAGE.read_bytes()).hexdigest()[:8] if OG_IMAGE.exists() else "none"


def tagline():
    """The README's tagline, verbatim — the lede under the h1 is that line and nothing else."""
    m = re.search(r"<p><em>(.+?)</em></p>", (BUILD / "README.md").read_text())
    if not m:
        sys.exit("README.md has no <p><em>tagline</em></p> — the landing lede is that line")
    return html.unescape(m.group(1))


def assert_endpoints_named():
    """Every path scripts/forwarding.py calls must be in ENDPOINTS, or the API table lies."""
    code = (BUILD / "scripts" / "forwarding.py").read_text()
    called = set(re.findall(r'"(/v\d/[a-z0-9/_-]+)"', code))
    missing = sorted(called - {p for p, _ in ENDPOINTS})
    if missing:
        sys.exit(f"scripts/forwarding.py calls {missing}, which ENDPOINTS does not name")


def call_url(c):
    return forwarding.BASE + c["endpoint"] + "?" + urllib.parse.urlencode(c["params"])


def utc_h(iso):
    """2026-09-18T23:16:31Z → 2026-09-18 23:16:31 UTC"""
    return iso.replace("T", " ").replace("Z", " UTC")


def pct1(x):
    return f"{x * 100:.1f}%"


def kind_class(kind):
    return {
        "MIGRATION": "",
        "CONSOLIDATION": "",
        "PARTIAL": "partial",
        "REBALANCE": "rebalance",
        "EXIT": "exit",
        "INCOMPLETE": "exit",
    }.get(kind, "refused")


def sev_word(kind, severity):
    if kind in ("EXIT", "INCOMPLETE"):
        return f"severity stays <em>{esc(severity)}</em>"
    return f"severity rewritten <em>red → {esc(severity)}</em>"


def join_call(calls, d=None):
    """The call the rows on the page came from: the maker= follow on the removal's own chain
    (the join every verdict rests on) — or, for a refusal that never joined, the trigger page
    whose stored bytes hold the refused pair. jit.json's pair sits on the third page, not the
    first, and the receipt block used to hash the first."""
    for c in calls:
        if c["endpoint"] == LIST_EP and "maker" in c["params"]:
            return c
    txn = (d or {}).get("txn")
    for c in calls:
        if (
            txn
            and c["endpoint"] == LIST_EP
            and holds_txn((d or {}).get("responses", {}).get(c.get("sha256")), txn)
        ):
            return c
    for c in calls:
        if c["endpoint"] == LIST_EP:
            return c
    return calls[0]


def holds_txn(body, txn):
    """Whether a stored /liquidity-change/list response carries a row with this txn hash."""
    rows = ((body or {}).get("data") or {}).get("lcs") or []
    return any(isinstance(r, dict) and r.get("txn") == txn for r in rows)


def is_join(c):
    return c.get("endpoint") == LIST_EP and "maker" in (c.get("params") or {})


def params_text(params, n=6):
    return " ".join(
        f"{k}={short(v, n) if len(str(v)) > 24 else v}" for k, v in params.items() if k != "limit"
    )


def calls_table(calls):
    """Every call behind a receipt: endpoint · params · status · ms · credit_count · sha256."""
    rows = []
    for i, c in enumerate(calls, 1):
        att = c.get("attempts") or 1
        status = f"{c['status']}" + (f" · {att} attempts" if att > 1 else "")
        rows.append(
            f'<tr><td class="mono">{i}</td><td class="mono">GET {esc(c["endpoint"])}</td>'
            f'<td class="mono">{esc(params_text(c["params"]))}</td>'
            f'<td class="mono ok-t">{esc(status)}</td><td class="mono">{c["ms"]}</td>'
            f'<td class="mono">{c.get("credit_count", "–")} · 0 billed</td>'
            f'<td class="mono">{esc(c.get("sha256", ""))}</td></tr>'
        )
    return (
        "<table><thead><tr><th>#</th><th>endpoint</th><th>params</th><th>status</th>"
        "<th>ms</th><th>credit_count</th><th>sha256 of the bytes received</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def receipt_grid(d, name, calls):
    """The seven keys of the family receipt block, in the family order."""
    jc = join_call(calls, d)
    statuses = sorted({str(c["status"]) for c in calls})
    v = d.get("verdict") or {}
    chains = len(v.get("follows") or [])
    # The fifth and sixth keys name the call whose hash and URL they show: the maker= join for a
    # verdict, the trigger page that held the pair for a refusal — never "first", which it is not
    # (the token lookup is call 1 on every receipt).
    joined = is_join(jc)
    which = "join call" if joined else "trigger page"
    items = [
        (
            "endpoint",
            f'<span class="mono">{esc(LIST_EP)}?maker=</span> — the join'
            if joined
            else f'<span class="mono">{esc(LIST_EP)}</span> — the trigger page; refused before any join',
        ),
        (
            "calls",
            f"{d['calls_made']} · HTTP {', '.join(statuses)}"
            + (f" · {chains} chains followed" if chains else " · refused before any follow"),
        ),
        ("credits used", f'<span class="mono">{d["credits_used"]}</span> — {esc(d["auth"])}'),
        (
            "captured",
            f'<span class="mono">{esc(d["captured_utc"])}</span> · {d["wall_clock_s"]:.1f} s wall clock',
        ),
        (f"{which} sha256", f'<span class="mono">{esc(jc.get("sha256", "—"))}</span>'),
        (
            f"{which.split()[0]} request",
            f'<a class="mono" href="{esc(call_url(jc))}" target="_blank" rel="noopener noreferrer">{esc(call_url(jc))}{EXT}</a>',
        ),
        (
            "re-derive",
            f'<span class="mono">python3 scripts/verify.py</span> · <a href="{REPO}/blob/main/docs/proof/{name}.json" target="_blank" rel="noopener noreferrer">{name}.json{EXT}</a> · <a href="#calls">every call ↓</a>',
        ),
    ]
    return "".join(f'<div><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in items)


def row_tr(row, cls, platform=None):
    return (
        f'<tr class="{cls}"><td>{esc(row.get("tp"))}</td><td>{esc(fmt_utc(ts_ms(row)))}</td>'
        f"<td>{esc(venue(row))} · {esc(pair(row))}{' [' + esc(platform) + ']' if platform else ''}</td>"
        f"<td>{float(row['tu']):,.2f}</td><td>{float(row.get('a0') or 0):,.4f} {esc(row.get('t0s'))}</td>"
        f"<td>{float(row.get('a1') or 0):,.4f} {esc(row.get('t1s'))}</td>"
        f"<td>{esc(short(row.get('txn'), 6))}</td></tr>"
    )


ROWS_HEAD = (
    "<table><thead><tr><th>side</th><th>utc</th><th>pool</th><th>tu (usd)</th><th>a0</th>"
    "<th>a1</th><th>txn</th></tr></thead><tbody>"
)


def follows_table(v):
    """One row per chain the wallet was followed on, then the chains refused, with the reason."""
    rows = []
    dest = v.get("destination") or {}
    r = v["removal"]
    for f in v.get("follows") or []:
        n = f.get("rows_in_window") or 0
        st = (
            f"{'200' if f.get('complete') else 'throttled'} · "
            f"{'complete' if f.get('complete') else 'INCOMPLETE'}"
        )
        found, cls, mark = "nothing by this wallet", "", ""
        if f["platform"] == dest.get("platform") and v["kind"] in (
            "MIGRATION",
            "CONSOLIDATION",
            "PARTIAL",
        ):
            found = (
                f"the add: +{money(dest.get('added_usd', v['recovered_usd']))} into "
                f"{esc(dest.get('venue', ''))} · {esc(dest.get('pair', ''))}"
            )
            cls, mark = "routed", '<small class="mark">◀ found here</small>'
        elif f["platform"] == v["token"].get("platform") and v["kind"] == "REBALANCE":
            found = f"the add: back into the same pool, +{money(v['recovered_usd'])}"
            cls, mark = "routed", '<small class="mark">◀ found here</small>'
        elif f["platform"] == v["token"].get("platform") and n:
            found = "the removal itself — no add"
        rows.append(
            f'<tr class="{cls}"><td class="pool">{esc(f["platform"])}<small>{esc(short(f["address"], 6))}</small>{mark}</td>'
            f'<td class="mono">{n}</td><td>{found}</td><td class="mono">{f.get("pages", 1)}</td>'
            f'<td class="mono{" ok-t" if f.get("complete") else " red"}">{esc(st)}</td></tr>'
        )
    for x in v.get("refused") or []:
        chain, _, why = x.partition(": ")
        rows.append(
            f'<tr class="refused"><td class="pool">{esc(chain)}</td><td class="mono">—</td>'
            f'<td colspan="3" class="reason">refused · {esc(why)}</td></tr>'
        )
    if not rows:
        rows.append(
            f'<tr><td class="pool">{esc(r.get("t0s", ""))}</td><td colspan="4" class="reason">'
            "no follow — the pair was refused before the trigger</td></tr>"
        )
    return "".join(rows)


def rule_line(v):
    rule = v.get("rule") or {}
    w = v.get("window_h") or {}
    return (
        f"rule: ≥ {rule.get('full', 0.7) * 100:.0f} % re-added elsewhere → Migration · "
        f"≥ {rule.get('full', 0.7) * 100:.0f} % same pool → Rebalance · "
        f"{rule.get('partial_min', 0.1) * 100:.0f}–{rule.get('full', 0.7) * 100:.0f} % → Partial · "
        f"nothing, every follow 200 → Exit · a throttled follow → Incomplete, never Exit · "
        f"window ±{w.get('back', 6):.0f} h · JIT pairs discarded first"
    )


def verdict_slots(name, d):
    """The product section for one committed verdict receipt — every string from the receipt."""
    v = d["verdict"]
    r = v["removal"]
    kind = v["kind"]
    tok = v.get("token") or {}
    sym = tok.get("sym") or d.get("sym") or r.get("t0s") or ""
    dest = v.get("destination") or {}
    adds = [e for e in v["evidence"] if e["row"].get("tp") == "add"]
    add = next(
        (
            e["row"]
            for e in adds
            if venue(e["row"]) == dest.get("venue") and pair(e["row"]) == dest.get("pair")
        ),
        adds[0]["row"] if adds else None,
    )
    pct = pct1(v["recovered_share"])
    elapsed = fmt_elapsed(v.get("elapsed_s"))
    when = "later" if (v.get("elapsed_s") or 0) >= 0 else "earlier"
    share_of_pool = (
        f"{v['removal_share_of_pool'] * 100:.1f}% of the pool"
        if v.get("removal_share_of_pool") is not None
        else "share of pool unknown"
    )
    alert = (
        f"<b>−{money(v['removed_usd'])}</b> out of {esc(venue(r))} · {esc(pair(r))} by "
        f"<b>{esc(short(r['m'], 6))}</b>, {esc(fmt_utc(ts_ms(r)))} · {share_of_pool}."
    )
    n_follow = len(v.get("follows") or [])
    if kind in ("MIGRATION", "CONSOLIDATION", "PARTIAL") and dest:
        liq = dest.get("liquidity_now_usd")
        support = (
            f"{alert} <b>+{money(dest.get('added_usd', v['recovered_usd']))}</b> into "
            f"{esc(dest.get('venue', ''))} · {esc(dest.get('pair', ''))} <b>{elapsed} {when}</b>"
            + (f" · the pool now holds {money(liq)}" if liq else "")
            + (" · a pool created after the removal" if kind == "CONSOLIDATION" else "")
        )
        line = (
            f"▶ {kind} · {sev_word(kind, v['severity']).replace('<em>', '').replace('</em>', '')} · "
            f"{pct} recovered into {dest.get('venue', '')} · {dest.get('pair', '')}, {elapsed} {when}"
        )
    elif kind == "REBALANCE":
        other = (v.get("adjudication") or {}).get("other_removals_in_window") or 0
        support = (
            f"{alert} <b>+{money(v['recovered_usd'])}</b> back into the <b>same pool</b> "
            f"<b>{elapsed} {when}</b>"
            + (
                f" · the wallet removed {other} more time(s) inside the window, so the share is over all of them — disclosed, not hidden"
                if other
                else ""
            )
        )
        line = (
            f"▶ REBALANCE · severity rewritten red → {v['severity']} · {pct} back into the same "
            f"pool {elapsed} later"
        )
    elif kind == "INCOMPLETE":
        bad = ", ".join(f["platform"] for f in v.get("follows") or [] if not f.get("complete"))
        support = (
            f"{alert} A follow did not complete on <b>{esc(bad)}</b> — a throttle is never an Exit."
        )
        line = f"▶ INCOMPLETE · not an Exit · re-run; the follow was throttled on {bad}"
    else:  # EXIT
        chains = ", ".join(f["platform"] for f in v.get("follows") or [])
        support = (
            f"{alert} Nothing re-added by <b>{esc(short(r['m'], 6))}</b> within "
            f"±{(v.get('window_h') or {}).get('back', 6):.0f} h on <b>{esc(chains)}</b> — "
            f"every follow answered 200, so this is a real Exit, stated with the window it searched."
        )
        line = (
            f"▶ EXIT · severity stays {v['severity']} · nothing re-added within "
            f"±{(v.get('window_h') or {}).get('back', 6):.0f} h on {n_follow} chains, every follow 200"
        )
    # the rows panel
    jc = join_call(d["calls"], d)
    cap = (
        f'maker <span class="mono">{esc(r["m"])}</span> · endpoint <span class="mono">{esc(LIST_EP)}?maker=</span> · '
        f'sha256 <span class="mono">{esc((jc.get("sha256") or "")[:16])}…</span> · '
        f'<a href="{REPO}/blob/main/docs/proof/{name}.json" target="_blank" rel="noopener noreferrer">the whole receipt{EXT}</a>'
    )
    trs = [row_tr(r, "gone")]
    for e in adds[:4]:
        trs.append(row_tr(e["row"], "leg", e["platform"]))
    rows_json = [r] + [e["row"] for e in adds[:4]]
    arith = []
    if add is not None and kind != "EXIT":
        arith.append(
            f"<li>tu_add {abs(float(add['tu'])):,.6f} ÷ tu_remove {abs(float(r['tu'])):,.6f} = "
            f"{v['recovered_share']:.4f} → {pct} recovered</li>"
        )
        ms = int(add["ts"]) - int(r["ts"])
        arith.append(
            f"<li>ts_add {esc(add['ts'])} − ts_remove {esc(r['ts'])} = {ms:,} ms → {fmt_elapsed(ms / 1000)} {when}</li>"
        )
        a0r, a0a = float(r.get("a0") or 0), float(add.get("a0") or 0)
        same = (
            "identical to ten decimals" if abs(abs(a0r) - abs(a0a)) < 1e-9 else "not the same size"
        )
        arith.append(
            f"<li>a0 {a0r:,.10f} {esc(r.get('t0s'))} out · +{a0a:,.10f} {esc(add.get('t0s'))} in → {same}</li>"
        )
        pool_move = (
            "same pool"
            if venue(add) == venue(r) and pair(add) == pair(r)
            else f"{esc(pair(r))} → {esc(pair(add))}"
        )
        arith.append(
            f"<li>same m on both rows · {pool_move} · both rows from one keyless call → {kind}</li>"
        )
        title = 'The two rows, verbatim — one <span class="mono">maker=</span> call'
    else:
        chains = ", ".join(f["platform"] for f in v.get("follows") or [])
        arith.append(
            f"<li>the removal: tu {float(r['tu']):,.6f} · m {esc(short(r['m'], 6))} · {esc(fmt_utc(ts_ms(r)))}</li>"
        )
        arith.append(
            f"<li>adds by that maker within ±{(v.get('window_h') or {}).get('back', 6):.0f} h on {esc(chains)}: none · every follow 200</li>"
        )
        arith.append("<li>nothing to divide → EXIT, stated with the window it searched</li>")
        title = "The one row — nothing to join"
    return {
        "context": (
            f"<b>{esc(sym)}</b> · {esc(tok.get('platform', ''))} · {esc(short(tok.get('address', ''), 6))} · "
            f"rule: {esc(d.get('selection_rule', ''))} · captured {esc(utc_h(d['captured_utc']))} · "
            f"{d['calls_made']} calls · {d['credits_used']} credits · keyless"
        ),
        "hero_class": kind_class(kind),
        "share_num": f"{v['recovered_share'] * 100:.1f}",
        "share": pct,
        "claim": f"{esc(kind)} · {sev_word(kind, v['severity'])}",
        "support": support,
        "table": follows_table(v),
        "route_class": kind_class(kind) if kind in ("EXIT", "INCOMPLETE") else "",
        "route_line": line,
        "route_rule": rule_line(v),
        "rows_title": title,
        "rows_cap": cap,
        "rows_table": ROWS_HEAD + "".join(trs) + "</tbody></table>",
        "rows_arith": "".join(arith),
        "rows_json": esc(json.dumps(rows_json, indent=1, ensure_ascii=False)),
        "receipt": receipt_grid(d, name, d["calls"]),
        "calls": calls_table(d["calls"]),
        "calls_n": str(len(d["calls"])),
    }


def jit_slots(name, d):
    """The refusal receipt: a same-transaction add + remove, handed to investigate() by hash."""
    rows = d["rows"]
    add = next(x for x in rows if x["tp"] == "add")
    rem = next(x for x in rows if x["tp"] == "remove")
    jc = join_call(d["calls"], d)
    support = (
        f"<b>+{money(usd(add))}</b> and <b>−{money(usd(rem))}</b> in one transaction on "
        f"{esc(venue(rem))} · {esc(pair(rem))}, {esc(fmt_utc(ts_ms(rem)))} — just-in-time liquidity. "
        f"Named by hash, the agent answers: <b>{esc(d['refused'])}</b>"
    )
    trs = row_tr(rem, "gone") + row_tr(add, "leg")
    return {
        "context": (
            f"<b>{esc(d['sym'])}</b> · {esc(d['platform'])} · {esc(short(d['address'], 6))} · "
            f"rule: {esc(d['selection_rule'])} · captured {esc(utc_h(d['captured_utc']))} · "
            f"{d['calls_made']} calls · {d['credits_used']} credits · keyless"
        ),
        "hero_class": "refused",
        "share_num": "0",
        "share": "refused",
        "claim": "Not an event · <em>JIT</em> · no severity to rewrite",
        "support": support,
        "table": (
            f'<tr><td class="pool">{esc(d["platform"])}<small>{esc(short(d["address"], 6))}</small></td>'
            '<td class="mono">—</td><td colspan="3" class="reason">no follow — a pair that adds and '
            "removes in one transaction is discarded before the trigger</td></tr>"
        ),
        "route_class": "refused",
        "route_line": f"▶ refused · not an event — the same txn {short(d['txn'], 6)} adds and removes",
        "route_rule": "rule: a transaction that both adds and removes is just-in-time liquidity; it never reaches adjudication",
        "rows_title": "The pair the agent refused — one transaction, both sides",
        "rows_cap": (
            f'txn <span class="mono">{esc(d["txn"])}</span> · endpoint <span class="mono">{esc(LIST_EP)}</span> · '
            f'sha256 <span class="mono">{esc((jc.get("sha256") or "")[:16])}…</span> · '
            f'<a href="{REPO}/blob/main/docs/proof/{name}.json" target="_blank" rel="noopener noreferrer">the whole receipt{EXT}</a>'
        ),
        "rows_table": ROWS_HEAD + trs + "</tbody></table>",
        "rows_arith": (
            f"<li>remove tu {float(rem['tu']):,.2f} · add tu {float(add['tu']):,.2f} · same txn on both rows</li>"
            f"<li>{esc(short(add['txn'], 6))} == {esc(short(rem['txn'], 6))} → just-in-time liquidity, not an event → refused</li>"
        ),
        "rows_json": esc(json.dumps([rem, add], indent=1, ensure_ascii=False)),
        "receipt": receipt_grid(d, name, d["calls"]),
        "calls": calls_table(d["calls"]),
        "calls_n": str(len(d["calls"])),
    }


def pill(name, d):
    if name == "jit":
        return f"{esc(d['sym'])} {compact_money(d['usd'])}<small>refused · JIT</small>"
    v = d["verdict"]
    sym = (v.get("token") or {}).get("sym") or d.get("sym") or ""
    label = v["kind"].lower()
    if v["kind"] in ("MIGRATION", "CONSOLIDATION", "PARTIAL", "REBALANCE"):
        label += f" · {v['recovered_share'] * 100:.{1 if v['recovered_share'] < 1 else 0}f}%"
    extra = " v3→v4" if name == "uni_v3_v4" else ""
    return f"{esc(sym)}{extra} −{compact_money(v['removed_usd'])}<small>{label}</small>"


def hero_viz(v):
    """The frozen moment, drawn from the receipt's own two rows: the removal bar (red) and the
    add bar (amber once the join lands), bar length is each row's |tu|, joined by the wallet's
    hairpin. No text inside the SVG — the key beneath it carries the words."""
    r = v["removal"]
    dest = v.get("destination") or {}
    adds = [e["row"] for e in v["evidence"] if e["row"].get("tp") == "add"]
    add = next(
        (a for a in adds if venue(a) == dest.get("venue") and pair(a) == dest.get("pair")),
        adds[0] if adds else None,
    )
    rows = [("gone", r)] + ([("leg l1", add)] if add is not None else [])
    mx = max(usd(x) for _, x in rows) or 1.0
    # the bars stop 130px short of the right edge so the hairpin has room to turn — on this
    # receipt the two bars are within 0.2 % of each other, which is the point of the picture
    # two rows, so each is drawn tall (the flagship's five-row block is 252 units high; this
    # one is 240) — at 375px wide a 44-unit bar is still 12px, not a hairline
    top, rowh, bar, x0, barmax = 28, 92, 44, 70, 1030
    mid = rowh // 2
    height = top * 2 + rowh * len(rows)
    sym = esc(r.get("t0s") or "")
    if add is not None:
        label = (
            f"Two liquidity rows of {sym} by the same wallet: the red bar is the removal from "
            f"{esc(pair(r))}, the amber bar the add into {esc(pair(add))} {fmt_elapsed(v.get('elapsed_s'))} "
            f"later; the hairpin is the wallet's trail from one to the other."
        )
    else:
        label = f"One liquidity row of {sym}: the removal, and nothing re-added by that wallet."
    parts = [
        f'<svg class="block" viewBox="0 0 1200 {height}" role="img" aria-label="{label}" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{label}</title>",
        f'<line class="rail" x1="40" y1="{top}" x2="40" y2="{height - top}"/>',
    ]
    ys = []
    for i, (cls, row) in enumerate(rows):
        y = top + i * rowh
        w = max(28.0, usd(row) / mx * barmax)
        ys.append((y, w))
        parts.append(
            f'<g class="row {cls}"><line class="tick" x1="34" y1="{y + mid}" x2="46" y2="{y + mid}"/>'
            f'<rect x="{x0}" y="{y + mid - bar // 2}" width="{w:.0f}" height="{bar}" rx="8"/></g>'
        )
    if len(ys) == 2:
        (y1, w1), (y2, w2) = ys
        xe1, xe2 = x0 + w1, x0 + w2
        xr = min(1180, max(xe1, xe2) + 64)
        r_ = abs(y2 - y1) / 2
        parts.append(
            f'<path class="hairpin" d="M{xe1:.0f} {y1 + mid} H{xr - r_:.0f} A{r_:.0f} {r_:.0f} 0 0 1 {xr - r_:.0f} {y2 + mid} '
            f'H{xe2 + 16:.0f} m14 -11 l-14 11 l14 11"/>'
        )
    parts.append("</svg>")
    key = (
        f'<span><i class="red"></i><span><b class="red">remove</b> · {esc(venue(r))} · {esc(pair(r))} · '
        f"<b>−{money(v['removed_usd'])}</b> · {esc(fmt_utc(ts_ms(r)))} — bar length is the row's |tu|</span></span>"
    )
    if add is not None:
        key += (
            f'<span><i class="leg"></i><span><b class="orange">add</b> · {esc(venue(add))} · {esc(pair(add))} · '
            f"<b>+{money(usd(add))}</b> · {fmt_elapsed(v.get('elapsed_s'))} later · the same maker "
            f"<b>{esc(short(r['m'], 6))}</b> · the same {abs(float(add.get('a0') or 0)):,.10f} {sym}</span></span>"
        )
    else:
        key += (
            f'<span><i class="leg"></i><span><b class="orange">add</b> · none by {esc(short(r["m"], 6))} within '
            f"±{(v.get('window_h') or {}).get('back', 6):.0f} h on {len(v.get('follows') or [])} chains</span></span>"
        )
    return "\n".join(parts), key


def lead_ctx(hero):
    """The h1 in two sentences with the number in it, the title and the descriptions."""
    v = hero["verdict"]
    r = v["removal"]
    dest = v.get("destination") or {}
    kind = v["kind"]
    pct = f"{v['recovered_share'] * 100:.1f}"
    elapsed = fmt_elapsed(v.get("elapsed_s"))
    back = (v.get("window_h") or {}).get("back", 6)
    n = len(v.get("follows") or [])
    line1 = f"LP removed: {money(v['removed_usd'])} out of {esc(pair(r))}."
    big = f'<span class="big" data-count="{pct}">{pct}%</span>'
    if kind in ("MIGRATION", "CONSOLIDATION"):
        tail = f"of it was in {esc(dest.get('pair', ''))} {elapsed} later. Same wallet."
        title_tail = "was one pool over"
    elif kind == "REBALANCE":
        tail = f"of it was back in the same pool {elapsed} later. Same wallet."
        title_tail = "was back in the same pool"
    elif kind == "PARTIAL":
        tail = f"of it came back within ±{back:.0f} h. Same wallet."
        title_tail = "came back"
    else:
        tail = f"came back within ±{back:.0f} h on {n} chains. A real exit."
        title_tail = "was a real exit"
    title = f"Forwarding Address — {pct}% of a {money(v['removed_usd'])} LP removal {title_tail}"
    if len(title) > 80:
        title = f"Forwarding Address — {pct}% of a {compact_money(v['removed_usd'])} LP removal {title_tail}"
    pitch = " Keyless CoinMarketCap API, 0 credits, every call receipted."
    pitch_short = " Keyless CoinMarketCap API, 0 credits."
    if kind in ("MIGRATION", "CONSOLIDATION", "PARTIAL"):
        moved = f"the same wallet put {pct}% into {dest.get('pair', '')} {elapsed} later."
    elif kind == "REBALANCE":
        moved = f"the same wallet put {pct}% back into the same pool {elapsed} later."
    else:
        moved = f"nothing came back within ±{back:.0f} h on {n} chains."
    lead = f"{tagline()} {money(v['removed_usd'])} left {pair(r)}"
    candidates = [
        f"{lead} on {venue(r)}; {moved}{pitch}",
        f"{lead}; {moved}{pitch}",
        f"{lead} on {venue(r)}; {moved}{pitch_short}",
        f"{lead}; {moved}{pitch_short}",
        f"{money(v['removed_usd'])} left {pair(r)}; {moved}{pitch}",
    ]
    desc = next((c for c in candidates if 120 <= len(c) <= 160), candidates[-1])
    if not 120 <= len(desc) <= 160:
        sys.exit(f"meta description is {len(desc)} chars; want 120-160")
    return {
        "hero.title": esc(title),
        "hero.description": esc(desc),
        "hero.line1": line1,
        "hero.line2": f"{big} {tail}",
        "hero.date": esc(hero["captured_utc"][:10]),
    }


def base_stats(b):
    if not b:
        return {}
    n = b["n"]
    s = b["split"]
    out = {"base.chains": str(len({p for p, _, _ in b["watchlist"]}))}
    for k in ("REBALANCE", "MIGRATION", "CONSOLIDATION", "PARTIAL", "EXIT", "INCOMPLETE"):
        out[f"base.{k.lower()}"] = f"{s.get(k, 0):,}"
        out[f"base.{k.lower()}_pct"] = f"{s.get(k, 0) / n * 100:.1f}"
    return out


def findings_ctx(live_run, bench_live):
    sweep = load("seed_sweep") or {}
    per = sweep.get("per_token") or []
    jit_by = [(t["sym"], t["jit_txns"]) for t in per if t.get("jit_txns")]
    bad_by = [(t["sym"], t["platform"]) for t in per if t.get("implausible_rows")]
    return {
        "find.implausible_by": esc(", ".join(f"{s} on {p}" for s, p in bad_by) or "no token"),
        "find.implausible": f"{sum(t.get('implausible_rows', 0) for t in per):,}",
        "find.sweep_rows": f"{sum(t.get('rows', 0) for t in per):,}",
        "find.sweep_removals": f"{sum(t.get('removals', 0) for t in per):,}",
        "find.jit": f"{sum(t.get('jit_txns', 0) for t in per):,}",
        "find.jit_by": esc(", ".join(f"{n} on {s}" for s, n in jit_by)),
        "find.backoffs": str(sum(1 for c in live_run["calls"] if (c.get("attempts") or 1) > 1)),
        "find.backoff_s": str(forwarding.BACKOFF_S),
        # the schedule the code runs (BACKOFF_S doubling RETRIES times), never typed: "15 / 30 / 60"
        "find.backoff_schedule": " / ".join(
            str(forwarding.BACKOFF_S * 2**i) for i in range(forwarding.RETRIES)
        ),
        "find.live_wall": f"{live_run['wall_clock_s']:.1f}",
        "find.sane_usd": compact_money(forwarding.SANE_USD).replace(".00", ""),
    }


def feedback_n():
    return str(len(re.findall(r"^## \d+\. ", FEEDBACK_MD.read_text(), re.M)))


def mcp_ctx():
    """The committed Claude Code session: tool calls, turns and wall clock from the raw stream."""
    if not MCP_SESSION.exists():
        return {"mcp.tools": "0", "mcp.turns": "0", "mcp.s": "0"}
    raw = MCP_SESSION.read_text()
    tools = len(re.findall(r'"name":"mcp__forwarding__[a-z_]+"', raw))
    turns = re.search(r'"num_turns":(\d+)', raw)
    ms = re.search(r'"duration_ms":(\d+)', raw)
    return {
        "mcp.tools": str(tools),
        "mcp.turns": turns.group(1) if turns else "0",
        "mcp.s": f"{int(ms.group(1)) / 1000:.0f}" if ms else "0",
    }


def api_rows(receipts):
    counts = dict.fromkeys((p for p, _ in ENDPOINTS), 0)
    for d in receipts:
        for c in d.get("calls") or []:
            if c["endpoint"] in counts:
                counts[c["endpoint"]] += 1
    rows = []
    for i, (p, what) in enumerate(ENDPOINTS):
        what_html = esc(what).replace("`maker=`", '<span class="mono">maker=</span>')
        rows.append(
            f"<tr{' class=engine' if i == 0 else ''}><td><code>/public-api{esc(p)}</code></td>"
            f'<td>{what_html}</td><td>none</td><td class="n">{counts[p]}</td></tr>'
        )
    return "".join(rows)


def term_ctx(live_run):
    """The terminal: the bare command's own output, replayed from the receipt it wrote through
    the CLI's own print functions — never a typed transcript."""
    lv = live_run["verdict"]
    v = forwarding.Verdict(**lv)
    out = io.StringIO()
    for c in live_run["calls"]:
        forwarding.print_trace_line(c, live_run["responses"].get(c.get("sha256")), out=out)
    forwarding.print_verdict(v, out=out)
    ok = sum(1 for c in live_run["calls"] if c["status"] == 200)
    body = out.getvalue().rstrip("\n").split("\n")
    lines = [
        f'<span class="p">$</span> <span class="cmd">git clone {esc(REPO)}.git &amp;&amp; cd forwarding</span>',
        f'<span class="p">$</span> <span class="cmd">{esc(CLI_CMD)}</span>',
        f'<span class="dim">forwarding address — keyless · {esc(v.token["platform"])} · {esc(v.token["address"])}</span>',
        "",
        '<span class="dim">  following the wallet</span>',
    ]
    block = None
    for ln in body:
        t = esc(ln.rstrip())
        s = ln.strip()
        if s.startswith("GET "):
            lines.append(f'<span class="dim">{t}</span>')
        elif s.startswith("● LP REMOVED"):
            block = "rd"
            lines.append(f'<span class="rd">{t}</span>')
        elif s.startswith("◆ "):
            block = "hi"
            lines.append(f'<span class="hi">{t}</span>')
        elif s.startswith("remove  ts="):
            lines.append(f'<span class="rd">{t}</span>')
        elif s.startswith("add     ts="):
            lines.append(f'<span class="hi">{t}</span>')
        elif s in ("the rows (verbatim fields)", "refused", "notes"):
            block = "dim"
            lines.append(f'<span class="bl">{t}</span>')
        elif not s:
            block = None
            lines.append("")
        elif block == "hi":
            lines.append(f'<span class="hi">{t}</span>')
        elif block == "rd":
            lines.append(f'<span class="cmd">{t}</span>')
        else:
            lines.append(f'<span class="dim">{t}</span>')
    lines += [
        "",
        f'<span class="ok">  {len(live_run["calls"])} calls · {ok} × 200 · {live_run["credits_used"]} credits · '
        f"{live_run['wall_clock_s']:.1f} s · keyless</span>",
        '<span class="dim">  wrote docs/proof/live_run.json</span>',
    ]
    return "\n".join(lines)


def proof_links(hero, live_run, base, mcp):
    items = [
        (
            "hero.json",
            f"{(hero['verdict'].get('token') or {}).get('sym', '')} · {hero['verdict']['kind']} {pct1(hero['verdict']['recovered_share'])} · "
            f"{hero['calls_made']} calls · {utc_h(hero['captured_utc'])}",
        ),
        (
            "live_run.json",
            f"the bare command · {live_run['calls_made']} calls · {live_run['wall_clock_s']:.1f} s · {utc_h(live_run['captured_utc'])}",
        ),
    ]
    if base:
        items.append(
            (
                "base_rate.json",
                f"{base['n']} removals ≥ ${base['min_usd']:,.0f} · {len(base['watchlist'])} tokens · {base['calls_made']} calls · {utc_h(base['captured_utc'])}",
            )
        )
    items.append(
        (
            "mcp_session.md",
            f"Claude Code, for real · {mcp['mcp.tools']} tool calls · {mcp['mcp.turns']} turns · {mcp['mcp.s']} s · the JIT refusal",
        )
    )
    return "".join(
        f'<a href="{REPO}/blob/main/docs/proof/{esc(f)}" target="_blank" rel="noopener noreferrer">'
        f'<div class="f">docs/proof/{esc(f)}{EXT}</div>'
        f'<div class="m">{esc(m)}</div></a>'
        for f, m in items
    )


def landing_ctx(hero, live_run, base, bench_live):
    """Everything scripts/site_templates/landing.html needs beyond the shared context."""
    assert_endpoints_named()
    receipts = []
    for name in SWITCHER:
        d = load(name)
        if not d:
            continue
        slots = jit_slots(name, d) if name == "jit" else verdict_slots(name, d)
        receipts.append({"name": name, "label": pill(name, d), "slots": slots, "raw": d})
    if not receipts or receipts[0]["name"] != "hero":
        sys.exit("docs/proof/hero.json must be the first switcher receipt")
    first = receipts[0]["slots"]
    v = hero["verdict"]
    viz, viz_key = hero_viz(v)
    mcp = mcp_ctx()
    ctx = {
        "site": SITE_URL,
        "og.v": og_version(),
        "version": version(),
        "hero.lede": esc(tagline()),
        "hero.og_description": esc(
            meta_description(v, v["removal"], v.get("destination") or {}, v.get("elapsed_s") or 0)
        ),
        "hero.og_alt": esc(
            f"A red LP-removed alert becoming an {v['severity']} {v['kind']} verdict: "
            f"{v['recovered_share'] * 100:.1f}% recovered."
        ),
        "hero.viz": viz,
        "hero.viz_key": viz_key,
        "hero.viz_foot": (
            f"{esc((v.get('token') or {}).get('sym', ''))} · {esc((v.get('token') or {}).get('platform', ''))} · "
            f"{len(v['evidence'])} rows from one maker= call · {len(v.get('follows') or [])} chains followed · "
            f"{hero['calls_made']} calls · {hero['wall_clock_s']:.1f} s"
        ),
        "switcher": "".join(
            f'<button type="button" data-i="{i}" aria-pressed="{"true" if i == 0 else "false"}">{r["label"]}</button>'
            for i, r in enumerate(receipts)
        ),
        "clone_cmd": esc(CLONE_CMD),
        "term": term_ctx(live_run),
        "api.rows": api_rows([r["raw"] for r in receipts] + [live_run]),
        "feedback.n": feedback_n(),
        "proof_links": proof_links(hero, live_run, base, mcp),
        "receipts_json": json.dumps(
            {"receipts": [{"name": r["name"], "slots": r["slots"]} for r in receipts]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).replace("</", "<\\/"),
    }
    ctx.update({f"lp.{k}": val for k, val in first.items()})
    ctx.update(lead_ctx(hero))
    ctx.update(findings_ctx(live_run, bench_live))
    ctx.update(base_stats(base))
    ctx.update(mcp)
    return ctx


def context():
    hero = load("hero")
    if not hero:
        sys.exit("docs/proof/hero.json is missing — run: python3 scripts/seed.py")
    v = hero["verdict"]
    r = v["removal"]
    adds = [e["row"] for e in v["evidence"] if e["row"].get("tp") == "add"]
    dest = v.get("destination") or {}
    add = next(
        (a for a in adds if venue(a) == dest.get("venue") and pair(a) == dest.get("pair")),
        adds[0] if adds else r,
    )
    exit_ = load("exit")
    rebalance = load("rebalance")
    jit = load("jit")
    base = load("base_rate")
    bench_replay = load("bench_replay") or {}
    bench_live = load("bench_live") or {}
    live_run = load("live_run")
    if not live_run:
        sys.exit("docs/proof/live_run.json is missing — run the zero-flag investigate")
    # The deck quotes the live run's call count and wall clock (DEMO.md's receipt) beside the
    # hero's verdict; the two receipts must agree on the verdict or one of them is stale.
    lv = live_run["verdict"]
    if (lv["kind"], round(lv["recovered_share"], 4), lv["removal"]["txn"]) != (
        v["kind"],
        round(v["recovered_share"], 4),
        r["txn"],
    ):
        sys.exit("docs/proof/live_run.json and hero.json disagree on the verdict — re-seed one")
    offline, live = test_count()
    (PROOF / "tests.json").write_text(
        json.dumps({"offline": offline, "live": live, "property_cases": PROPERTY_CASES}, indent=1)
    )

    others = [(n, d) for n, d in (("exit", exit_), ("rebalance", rebalance), ("jit", jit)) if d]
    others_summary = ", ".join(
        f"**{d['verdict']['kind']}** ({d['sym']}, {d['verdict']['recovered_share'] * 100:.0f}% recovered)"
        if n != "jit"
        else f"**refused** ({d['sym']} JIT pair, ${d['usd']:,.0f})"
        for n, d in others
    )
    elapsed_s = v["elapsed_s"] or 0
    desc = meta_description(v, r, dest, elapsed_s)
    ctx = {
        "repo": REPO,
        "site_url": SITE_URL,
        "site_host": SITE_URL.replace("https://", ""),
        "event": EVENT,
        "author": AUTHOR,
        "x_handle": X_HANDLE,
        "og.meta": og_meta(v),
        "meta.description": esc(desc),
        "cli_cmd": CLI_CMD,
        "mcp_cmd": MCP_CMD,
        "tests": str(offline),
        "tests_live": str(live),
        "property_cases": f"{PROPERTY_CASES:,}",
        "rendered_utc": time.strftime("%Y-%m-%d", time.gmtime()),
        "hero.removed_usd": money(v["removed_usd"]),
        "hero.recovered_usd": money(v["recovered_usd"]),
        "hero.recovered_pct": f"{v['recovered_share'] * 100:.1f}",
        "hero.share_4dp": f"{v['recovered_share']:.4f}",
        "hero.kind": v["kind"],
        "hero.severity": v["severity"],
        "hero.venue": esc(venue(r)),
        "hero.pair": esc(pair(r)),
        "hero.utc": fmt_utc(ts_ms(r)),
        "hero.maker": r["m"],
        "hero.maker_short": short(r["m"]),
        # None means the pool was not among the 20 the API returns, so the share is unknown —
        # rendering it as 0.0% put a number on the judged surface that no row supports
        "hero.share_of_pool": (
            f"{v['removal_share_of_pool'] * 100:.1f}%"
            if v.get("removal_share_of_pool") is not None
            else "an unknown share"
        ),
        "hero.txn_short": short(r["txn"], 6),
        "hero.add_txn_short": short(add["txn"], 6),
        "hero.dest_venue": esc(dest.get("venue", "")),
        "hero.dest_pair": esc(dest.get("pair", "")),
        "hero.dest_liq": compact_money(dest.get("liquidity_now_usd") or 0),
        "hero.elapsed": fmt_elapsed(elapsed_s),
        "hero.elapsed_s": str(elapsed_s),
        "hero.a0": f"{abs(float(add.get('a0') or 0)):,.2f}",
        "hero.sym": esc(v["token"].get("sym") or ""),
        "hero.trace_rows": trace_rows(hero["calls"]),
        "hero.remove_json": json_panel(r, "remove"),
        "hero.add_json": json_panel(add, "add"),
        "hero.add_tu": f"{abs(float(add['tu'])):,.2f}",
        "hero.remove_tu": f"{abs(float(r['tu'])):,.2f}",
        "hero.calls": str(hero["calls_made"]),
        "hero.wall": f"{hero['wall_clock_s']:.1f} s",
        "hero.captured": hero["captured_utc"].replace("T", " ").replace("Z", " UTC"),
        "hero.captured_iso": hero["captured_utc"],
        "hero.headline": (
            f"◆ {v['kind']} · severity {v['severity']}\n"
            f"    {v['recovered_share'] * 100:.1f}% recovered — {money(v['recovered_usd'])} of {money(v['removed_usd'])}\n"
            f"    +{money(dest.get('added_usd', v['recovered_usd']))} into {dest.get('venue', '')} · {dest.get('pair', '')} ({dest.get('platform', '')})   "
            f"{fmt_elapsed(elapsed_s)} later · pool now holds {money(dest.get('liquidity_now_usd') or 0)}"
        ),
        "other_cards": "\n".join(mini_card(n, d) for n, d in others),
        "others.summary": others_summary,
        "jit.sym": esc(jit["sym"]) if jit else "—",
        "jit.usd": f"{jit['usd']:,.0f}" if jit else "—",
        "endpoints.count": str(len(ENDPOINTS)),
        "endpoints.others": str(
            len(ENDPOINTS) - 1
        ),  # "the other N endpoints" in the API lede, counted with the h2
        "trigger.rows": str(
            forwarding.TRIGGER_PAGES * forwarding.PAGE
        ),  # the trigger's depth: 3 pages × 100 rows
        "endpoints.rows": "\n".join(
            f'        <tr><td class="mono">{esc(p)}</td><td>{esc(what).replace("`maker=`", "<span class=mono>maker=</span>")}</td><td class="ok-t">yes · 0 credits</td></tr>'
            for p, what in ENDPOINTS
        ),
        "bench.replay_p50": f"{bench_replay.get('adjudicate', {}).get('p50', 0):.3f}",
        "bench.replay_n": str(bench_replay.get("adjudicate", {}).get("n", 0)),
        "bench.live_p50": f"{bench_live.get('investigate', {}).get('p50', 0):.1f}",
        "bench.live_p95": f"{bench_live.get('investigate', {}).get('p95', 0):.1f}",
        "bench.live_n": str(bench_live.get("investigate", {}).get("n", 0)),
        # deck-only slots (scripts/site_templates/pitch.html)
        "alias_host": ALIAS_URL.replace("https://", ""),
        "event_buidls": EVENT_BUIDLS,
        "icon.animated": cover_icon(),
        "live.calls": str(live_run["calls_made"]),
        "live.wall": f"{live_run['wall_clock_s']:.1f} s",
        "live.captured": live_run["captured_utc"].replace("T", " ").replace("Z", " UTC"),
        "live.trace": deck_trace(live_run["calls"]),
        "hero.dest_liq_full": money(dest.get("liquidity_now_usd") or 0),
        "hero.ts_remove": esc(r["ts"]),
        "hero.ts_add": esc(add["ts"]),
        "hero.elapsed_ms": f"{int(add['ts']) - int(r['ts']):,}",
        "hero.remove_tu_raw": repr(float(r["tu"])),
        "hero.add_tu_raw": repr(float(add["tu"])),
        "hero.remove_a0_raw": repr(float(r.get("a0") or 0)),
        "hero.add_a0_raw": repr(float(add.get("a0") or 0)),
        "hero.remove_t1s": esc(r.get("t1s") or ""),
        "hero.add_t1s": esc(add.get("t1s") or ""),
    }
    ctx.update(base_ctx(base))
    ctx.update(landing_ctx(hero, live_run, base, bench_live))
    return ctx


# the two shapes the version takes on the pages: the landing footer chip and the deck's "· MIT · v…"
_VER_STAMP = re.compile(r'(class="ver[^"]*"[^>]*>|· MIT · )v\d+\.\d+\.\d+(?:-dev)?<')


def main():
    check = "--check" in sys.argv
    ctx = context()
    judge_md = render((TEMPLATES / "JUDGE.md").read_text(), ctx)
    outputs = {
        SITE / "index.html": render((TEMPLATES / "landing.html").read_text(), ctx),
        BUILD / "JUDGE.md": judge_md,
        # /judge on the site: the same Markdown, converted — never a second source of numbers
        SITE / "judge.html": render(
            (TEMPLATES / "judge.html").read_text(), {**ctx, "judge.body": md_to_html(judge_md)}
        ),
        # /pitch: the deck, same slots, same receipts — a judge comparing the deck, the landing
        # page and the README sees one set of numbers
        SITE / "pitch" / "index.html": render((TEMPLATES / "pitch.html").read_text(), ctx),
    }
    if check:
        # the render stamp is a date; a page rendered yesterday is not drift — and a clone with
        # no tag reachable (a shallow CI checkout) cannot judge the version stamp, so it is left
        # out of the comparison there and said so, never reported as drift
        no_tag = ctx["version"] == VERSION_FALLBACK
        stale = []
        for p, out in outputs.items():
            on_disk = p.read_text() if p.exists() else ""

            def strip(s):
                s = re.sub(r"rendered \d{4}-\d{2}-\d{2}", "rendered DATE", s)
                return _VER_STAMP.sub(r"\1vX<", s) if no_tag else s

            if strip(on_disk) != strip(out):
                stale.append(p)
        for p in stale:
            print(f"DRIFT: {p.relative_to(BUILD)} is not what the receipts render")
        if stale:
            sys.exit(1)
        print(
            "in sync: site/index.html, site/judge.html, site/pitch/index.html and JUDGE.md "
            "match docs/proof/*.json"
            + (
                " (no release tag reachable here — the version stamp was not compared)"
                if no_tag
                else ""
            )
        )
        return
    SITE.mkdir(parents=True, exist_ok=True)
    for path, out in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out)
    print(
        "rendered "
        + ", ".join(f"{p.relative_to(BUILD)} ({len(o):,} B)" for p, o in outputs.items())
    )


if __name__ == "__main__":
    main()
