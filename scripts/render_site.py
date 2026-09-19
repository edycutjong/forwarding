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
import json
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forwarding import fmt_elapsed, fmt_utc, pair, short, ts_ms, usd, venue  # noqa: E402

BUILD = Path(__file__).resolve().parents[1]
TEMPLATES = BUILD / "scripts" / "site_templates"
PROOF = BUILD / "docs" / "proof"
SITE = BUILD / "site"

REPO = "https://github.com/edycutjong/forwarding"
SITE_URL = "https://forwarding-cmc.vercel.app"
# GitHub Pages serves the same site/ at this host once the repository is public (site/CNAME,
# .github/workflows/pages.yml); the Vercel deployment stays the API host and a mirror.
PAGES_URL = "https://forwarding.edycu.dev"
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
            f'<p><a href="{REPO}/blob/main/docs/proof/jit.json" target="_blank" rel="noopener noreferrer">jit.json ↗</a></p></div>'
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
        f'<a href="{REPO}/blob/main/docs/proof/{name}.json" target="_blank" rel="noopener noreferrer">{name}.json ↗</a></p></div>'
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
        "pages_url": PAGES_URL,
        "pages_host": PAGES_URL.replace("https://", ""),
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
    return ctx


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
        # the render stamp is a date; a page rendered yesterday is not drift
        stale = []
        for p, out in outputs.items():
            on_disk = p.read_text() if p.exists() else ""
            strip = lambda s: re.sub(r"rendered \d{4}-\d{2}-\d{2}", "rendered DATE", s)  # noqa: E731
            if strip(on_disk) != strip(out):
                stale.append(p)
        for p in stale:
            print(f"DRIFT: {p.relative_to(BUILD)} is not what the receipts render")
        if stale:
            sys.exit(1)
        print(
            "in sync: site/index.html, site/judge.html, site/pitch/index.html and JUDGE.md "
            "match docs/proof/*.json"
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
