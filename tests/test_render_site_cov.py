"""scripts/render_site.py, offline: every slot, every converter branch, both modes of main().

The committed receipts under docs/proof/ are the fixtures — copied into a tmp mirror so the
render never writes into the repository. pytest and git are the only subprocesses the script
spawns; both are replaced by canned results.
"""

import copy
import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import forwarding
import pytest
import render_site as rs

BUILD = Path(__file__).resolve().parents[1]
REAL_PROOF = BUILD / "docs" / "proof"


def real(name):
    return json.loads((REAL_PROOF / f"{name}.json").read_text())


def png_bytes(w, h):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + chunk
        + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    )


def call(endpoint, params, status=200, ms=100, sha="ab" * 32, credit=1, attempts=None):
    c = {"endpoint": endpoint, "params": params, "status": status, "ms": ms}
    if sha is not None:
        c["sha256"] = sha
    if credit is not None:
        c["credit_count"] = credit
    if attempts is not None:
        c["attempts"] = attempts
    return c


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """A copy of everything the render reads, with every path constant pointed at it."""
    root = tmp_path / "build"
    (root / "scripts").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "site" / "assets").mkdir(parents=True)
    shutil.copytree(REAL_PROOF, root / "docs" / "proof")
    shutil.copytree(BUILD / "scripts" / "site_templates", root / "scripts" / "site_templates")
    shutil.copytree(BUILD / "docs" / "assets", root / "docs" / "assets")
    shutil.copy(BUILD / "README.md", root / "README.md")
    shutil.copy(BUILD / "FEEDBACK.md", root / "FEEDBACK.md")
    shutil.copy(BUILD / "scripts" / "forwarding.py", root / "scripts" / "forwarding.py")
    (root / "site" / "assets" / "og-image.png").write_bytes(png_bytes(1200, 630))
    monkeypatch.setattr(rs, "BUILD", root)
    monkeypatch.setattr(rs, "TEMPLATES", root / "scripts" / "site_templates")
    monkeypatch.setattr(rs, "PROOF", root / "docs" / "proof")
    monkeypatch.setattr(rs, "SITE", root / "site")
    monkeypatch.setattr(rs, "OG_IMAGE", root / "site" / "assets" / "og-image.png")
    monkeypatch.setattr(rs, "ICON_ANIMATED", root / "docs" / "assets" / "icon-animated.svg")
    monkeypatch.setattr(rs, "FEEDBACK_MD", root / "FEEDBACK.md")
    monkeypatch.setattr(rs, "MCP_SESSION", root / "docs" / "proof" / "mcp_session.jsonl")
    return root


@pytest.fixture
def no_subprocess(monkeypatch):
    """pytest --collect-only and git describe answer from a table, never from a process."""
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        if cmd[:2] == ["git", "describe"]:
            return SimpleNamespace(returncode=0, stdout="v1.2.3\n")
        marker = cmd[-1]
        out = "no tests collected (5 deselected)" if marker == "live" else "105 tests collected"
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


@pytest.fixture
def hero():
    return real("hero")


@pytest.fixture
def verdict(hero):
    return copy.deepcopy(hero["verdict"])


def test_load_returns_the_receipt_or_none_when_the_file_is_absent(mirror):
    assert rs.load("hero")["verdict"]["kind"] == "MIGRATION"
    assert rs.load("nope") is None


def test_money_and_compact_money_pick_the_unit_by_magnitude():
    assert rs.money(1234.5) == "$1,234"
    assert rs.money(1234.5, 2) == "$1,234.50"
    assert rs.compact_money(2.5e9) == "$2.50B"
    assert rs.compact_money(21_330_274) == "$21.33M"
    assert rs.compact_money(229_834) == "$230k"
    assert rs.compact_money(12.7) == "$13"
    assert rs.esc('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"


def test_png_size_reads_the_header_and_refuses_a_non_png(mirror):
    assert rs.png_size(rs.OG_IMAGE) == (1200, 630)
    bad = mirror / "site" / "assets" / "bad.png"
    bad.write_bytes(b"GIF89a" + b"\0" * 30)
    with pytest.raises(SystemExit, match="is not a PNG"):
        rs.png_size(bad)


def test_meta_description_drops_the_venue_then_the_pair_to_fit_the_card(verdict):
    r = verdict["removal"]
    dest = verdict["destination"]
    d = rs.meta_description(verdict, r, dest, 132)
    assert d.endswith("Follow the wallet — keyless, live, CoinMarketCap.")
    assert 50 <= len(d) <= rs.DESC_MAX
    long_venue = dict(r, en="A venue whose name is far too long for a social card to carry")
    d2 = rs.meta_description(verdict, long_venue, dest, 132)
    assert "re-added" in d2 and "UNI/WBTC" in d2
    long_pair = dict(long_venue, t0s="TOKENWITHAVERYLONGSYMBOL", t1s="ANOTHERLONGSYMBOL")
    d3 = rs.meta_description(verdict, long_pair, dest, 132)
    assert d3.startswith("$21,330,275 removed;")


def test_meta_description_exits_when_even_the_shortest_form_is_too_long(verdict):
    verdict["removed_usd"] = 1e30
    verdict["recovered_share"] = 1e12
    with pytest.raises(SystemExit, match="meta description is"):
        rs.meta_description(verdict, verdict["removal"], {}, 10**9)


def test_og_meta_is_empty_without_the_image_and_refuses_the_wrong_size(mirror, verdict):
    tags = rs.og_meta(verdict)
    assert 'property="og:image:width" content="1200"' in tags
    assert "MIGRATION verdict: 99.8% recovered" in tags
    rs.OG_IMAGE.write_bytes(png_bytes(600, 315))
    with pytest.raises(SystemExit, match="og-image.png is 600x315"):
        rs.og_meta(verdict)
    rs.OG_IMAGE.unlink()
    assert rs.og_meta(verdict) == ""
    assert rs.og_version() == "none"


def test_og_version_hashes_the_image_bytes(mirror):
    assert len(rs.og_version()) == 8


def test_json_panel_keeps_the_field_order_and_classes_each_side():
    row = {"zzz": 1, "m": "0xabc", "tu": -5.0, "tp": "remove", "h": "1"}
    out = rs.json_panel(row, "remove")
    lines = out.splitlines()
    assert lines[1].startswith('  <span class="k">"tp"</span>: <span class="hl">')
    assert '"tu"</span>: <span class="tu-r">' in out
    assert '"m"</span>: <span class="m">' in out
    assert '"zzz"</span>: <span class="dim">' in out
    assert lines[-2].startswith('  <span class="k">"zzz"')
    assert '<span class="tu-a">' in rs.json_panel(row, "add")


def test_trace_rows_shortens_long_values_and_marks_missing_hash_and_credit():
    calls = [
        call("/v1/dex/token", {"address": "0x" + "a" * 40, "limit": "100", "q": "UNI"}),
        call("/v1/dex/search", {"q": "UNI"}, sha=None, credit=None),
    ]
    out = rs.trace_rows(calls)
    assert "address=0xaaaaaa…aaaaaa q=UNI" in out and "limit=" not in out
    assert 'data-short="" data-full=""' in out
    assert "credit_count – · 0 billed" in out
    assert "credit_count 1 · 0 billed" in out


def test_cover_icon_inlines_the_svg_with_two_loops_and_exits_when_missing(mirror):
    svg = rs.cover_icon()
    assert 'repeatCount="indefinite"' not in svg and "<svg" in svg
    rs.ICON_ANIMATED.unlink()
    with pytest.raises(SystemExit, match="icon-animated.svg is missing"):
        rs.cover_icon()


def test_deck_trace_cuts_the_cursor_lines_at_the_column():
    calls = [
        call("/v1/dex/token", {"platform": "ethereum", "limit": "100"}),
        call("/v1/dex/liquidity-change/list", {"lastId": "Q" * 30, "address": "0x" + "b" * 40}),
    ]
    out = rs.deck_trace(calls).splitlines()
    assert "platform=ethereum" in out[0] and "limit" not in out[0]
    assert "…" in out[1]


def test_base_ctx_renders_the_split_and_a_placeholder_without_the_receipt():
    assert rs.base_ctx(None)["base.headline"] == "Base rate — not captured yet"
    b = real("base_rate")
    ctx = rs.base_ctx(b)
    assert ctx["base.n"] == "335"
    assert "consolidation" not in ctx["base.bar"]
    assert ctx["base.aria"].startswith("rebalance 58%")
    assert ctx["base.summary"] == "rebalance 194 · migration 19 · partial 10 · exit 112"
    assert ctx["base.put_back_pct"] == f"{b['put_back_share'] * 100:.0f}"


def test_base_stats_counts_the_chains_and_the_split():
    assert rs.base_stats(None) == {}
    out = rs.base_stats(real("base_rate"))
    assert out["base.exit"] == "112" and out["base.consolidation_pct"] == "0.0"
    assert int(out["base.chains"]) >= 1


def test_mini_card_draws_every_kind_and_nothing_for_a_missing_receipt():
    assert rs.mini_card("exit", None) == ""
    jit = rs.mini_card("jit", real("jit"))
    assert "Refused · JIT" in jit and "jit.json" in jit
    ex = rs.mini_card("exit", real("exit"))
    assert 'class="mini red"' in ex and "Nothing re-added" in ex and "ethereum, bsc" in ex
    reb = real("rebalance")
    plain = rs.mini_card("rebalance", reb)
    assert 'class="mini grey"' in plain and "cycled again" not in plain
    reb["verdict"]["adjudication"]["other_removals_in_window"] = 2
    assert "cycled again inside the window (2 more" in rs.mini_card("rebalance", reb)
    mig = rs.mini_card("hero", real("hero"))
    assert 'class="mini amber"' in mig and "99.8%" in mig and "Ring Exchange" in mig
    partial = real("hero")
    partial["verdict"]["kind"] = "PARTIAL"
    partial["verdict"]["destination"] = None
    assert "into <b> · </b>" in rs.mini_card("hero", partial)


def test_href_makes_relative_links_absolute_blob_links():
    assert rs._href("https://x.y/z") == "https://x.y/z"
    assert rs._href("#calls") == "#calls"
    assert rs._href("./docs/proof/hero.json") == f"{rs.REPO}/blob/main/docs/proof/hero.json"


def test_inline_protects_code_spans_and_renders_bold_italic_and_links():
    out = rs._inline("run `a **b** <c>` then **bold** and *it* [here](docs/x.md) [up](#calls)")
    assert "<code>a **b** &lt;c&gt;</code>" in out
    assert "<strong>bold</strong>" in out and "<em>it</em>" in out
    blob = f'<a href="{rs.REPO}/blob/main/docs/x.md" target="_blank" rel="noopener noreferrer">'
    assert f"{blob}here</a>" in out
    assert '<a href="#calls">up</a>' in out


def test_table_omits_the_header_row_when_every_head_cell_is_blank():
    with_head = rs._table(["| a | b |", "|---|---|", "| 1 | 2 |"])
    assert "<thead><tr><th>a</th><th>b</th></tr></thead>" in with_head
    assert "<tr><td>1</td><td>2</td></tr>" in with_head
    no_head = rs._table(["|  |  |", "|---|---|", "| 1 | 2 |"])
    assert "<thead>" not in no_head


MD = """<!-- a comment -->
# Title

**A claim on its own.**

Plain text that
wraps onto a second line.

```
code <here>
```

| k | v |
|---|---|
| a | b |

1. first
   continued
2. second
- bullet
- another

* star bullet
-

```
unterminated
"""


def test_md_to_html_knows_exactly_the_constructs_the_judge_guide_uses():
    out = rs.md_to_html(MD)
    assert "<h1>Title</h1>" in out
    assert '<p class="claim"><strong>A claim on its own.</strong></p>' in out
    assert "<p>Plain text that wraps onto a second line.</p>" in out
    assert "<pre><code>code &lt;here&gt;</code></pre>" in out
    assert "<td>a</td><td>b</td>" in out
    assert "<ol>\n<li>first continued</li>\n<li>second</li>\n</ol>" in out
    assert "<ul>\n<li>bullet</li>\n<li>another</li>\n</ul>" in out
    assert "<li>star bullet</li>" in out
    assert "<pre><code>unterminated</code></pre>" in out
    assert "<!--" not in out


def test_md_to_html_flushes_nothing_for_leading_blank_lines():
    assert rs.md_to_html("\n\n") == ""
    assert rs.md_to_html("- one\n\n- two") == "<ul>\n<li>one</li>\n</ul>\n<ul>\n<li>two</li>\n</ul>"


def test_render_fills_every_slot_or_exits_naming_the_ones_left():
    assert rs.render("a {{x}} {{y.z}}", {"x": 1, "y.z": "two"}) == "a 1 two"
    with pytest.raises(SystemExit, match=r"unfilled slots: \['y.z'\]"):
        rs.render("a {{x}} {{y.z}}", {"x": 1})


def test_collected_parses_each_shape_of_the_pytest_summary():
    assert rs._collected("no tests collected (105 deselected)") == 0
    assert rs._collected("5/110 tests collected (105 deselected)") == 5
    assert rs._collected("1 test collected") == 1
    with pytest.raises(ValueError, match="unrecognised pytest summary"):
        rs._collected("something else entirely")


def test_test_count_asks_pytest_and_falls_back_to_the_receipt(mirror, no_subprocess, monkeypatch):
    assert rs.test_count() == (105, 0)
    assert len(no_subprocess) == 2

    def boom(*a, **kw):
        raise FileNotFoundError("pytest")

    monkeypatch.setattr(subprocess, "run", boom)
    receipt = real("tests")
    assert rs.test_count() == (receipt["offline"], receipt["live"])
    (rs.PROOF / "tests.json").unlink()
    with pytest.raises(SystemExit, match="cannot count tests"):
        rs.test_count()


def test_version_is_the_reachable_tag_or_the_fallback(monkeypatch, no_subprocess):
    assert rs.version() == "v1.2.3"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=128, stdout="")
    )
    assert rs.version() == rs.VERSION_FALLBACK
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="v1.2\n")
    )
    assert rs.version() == rs.VERSION_FALLBACK

    def boom(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert rs.version() == rs.VERSION_FALLBACK


def test_tagline_is_the_readme_lede_and_exits_when_it_is_gone(mirror):
    assert rs.tagline() == "Where the pulled liquidity went."
    (mirror / "README.md").write_text("# no lede\n")
    with pytest.raises(SystemExit, match="README.md has no"):
        rs.tagline()


def test_assert_endpoints_named_exits_when_the_engine_calls_an_unlisted_path(mirror):
    rs.assert_endpoints_named()
    src = mirror / "scripts" / "forwarding.py"
    src.write_text(src.read_text() + '\nX = "/v9/dex/secret"\n')
    with pytest.raises(SystemExit, match=r"calls \['/v9/dex/secret'\]"):
        rs.assert_endpoints_named()


def test_small_formatters():
    c = call("/v1/dex/token", {"a": "1", "b": "x y"})
    assert rs.call_url(c) == forwarding.BASE + "/v1/dex/token?a=1&b=x+y"
    assert rs.utc_h("2026-09-18T23:16:31Z") == "2026-09-18 23:16:31 UTC"
    assert rs.pct1(0.99798) == "99.8%"
    assert rs.kind_class("PARTIAL") == "partial"
    assert rs.kind_class("MIGRATION") == ""
    assert rs.kind_class("JIT") == "refused"
    assert rs.sev_word("EXIT", "red") == "severity stays <em>red</em>"
    assert rs.sev_word("MIGRATION", "amber") == "severity rewritten <em>red → amber</em>"
    assert rs.is_join(call(rs.LIST_EP, {"maker": "0x1"}))
    assert not rs.is_join({"endpoint": rs.LIST_EP})
    assert rs.params_text({"limit": "100", "q": "UNI", "address": "0x" + "c" * 40}, 2) == (
        "q=UNI address=0xcc…cc"
    )


def test_join_call_prefers_the_maker_join_then_the_page_holding_the_txn_then_any_list_page():
    tok = call("/v1/dex/token", {})
    page1 = call(rs.LIST_EP, {"page": 1}, sha="s1")
    page2 = call(rs.LIST_EP, {"page": 2}, sha="s2")
    join = call(rs.LIST_EP, {"maker": "0x1"}, sha="j")
    assert rs.join_call([tok, page1, join]) is join
    d = {
        "txn": "0xdead",
        "responses": {"s2": {"data": {"lcs": ["junk", {"txn": "0xdead"}]}}, "s1": None},
    }
    assert rs.join_call([tok, page1, page2], d) is page2
    assert rs.join_call([tok, page1, page2], {"txn": None}) is page1
    assert rs.join_call([tok, page1, page2]) is page1
    assert rs.join_call([tok], {"txn": "0xdead", "responses": {}}) is tok
    assert not rs.holds_txn(None, "0xdead")
    assert not rs.holds_txn({"data": {}}, "0xdead")


def test_calls_table_notes_the_retries_and_the_missing_hash():
    out = rs.calls_table(
        [
            call("/v1/dex/token", {"q": "1"}, attempts=3),
            call("/v1/dex/search", {"q": "2"}, sha=None, credit=None),
        ]
    )
    assert "200 · 3 attempts" in out
    assert "– · 0 billed" in out
    assert out.count("<tr>") == 3


def test_receipt_grid_names_the_join_for_a_verdict_and_the_trigger_page_for_a_refusal(hero):
    out = rs.receipt_grid(hero, "hero", hero["calls"])
    assert "?maker=</span> — the join" in out
    assert "5 chains followed" in out
    assert "join call sha256" in out and "join request" in out
    jit = real("jit")
    out = rs.receipt_grid(jit, "jit", jit["calls"])
    assert "refused before any join" in out and "refused before any follow" in out
    assert "trigger page sha256" in out and "trigger request" in out


def test_row_tr_appends_the_platform_only_when_given(verdict):
    r = verdict["removal"]
    assert "[arbitrum]" in rs.row_tr(r, "leg", "arbitrum")
    plain = rs.row_tr(r, "gone")
    assert "[" not in plain and 'class="gone"' in plain
    bare = rs.row_tr({"tp": "add", "ts": "0", "tu": "1"}, "leg")
    assert "0.0000 None" in bare


def test_follows_table_marks_where_the_add_was_found_on_each_kind(verdict):
    mig = rs.follows_table(verdict)
    assert mig.count("◀ found here") == 1 and "Ring Exchange" in mig
    assert "refused · 8FU95xFJ" in mig
    assert mig.count('class="refused"') == len(verdict["refused"])
    reb = real("rebalance")["verdict"]
    out = rs.follows_table(reb)
    assert "back into the same pool" in out and "◀ found here" in out
    ex = real("exit")["verdict"]
    out = rs.follows_table(ex)
    assert "the removal itself — no add" in out and "nothing by this wallet" in out
    ex["follows"][1]["complete"] = False
    out = rs.follows_table(ex)
    assert "throttled · INCOMPLETE" in out and 'class="mono red"' in out
    empty = {"removal": {"t0s": "DAI"}, "follows": [], "refused": [], "kind": "EXIT"}
    assert "no follow — the pair was refused" in rs.follows_table(empty)


def test_rule_line_falls_back_to_the_published_thresholds():
    assert rs.rule_line({}) == (
        "rule: ≥ 70 % re-added elsewhere → Migration · ≥ 70 % same pool → Rebalance · "
        "10–70 % → Partial · nothing, every follow 200 → Exit · a throttled follow → "
        "Incomplete, never Exit · window ±6 h · JIT pairs discarded first"
    )
    assert "≥ 80 %" in rs.rule_line({"rule": {"full": 0.8}, "window_h": {"back": 12}})


def test_verdict_slots_for_the_migration_receipt(hero):
    s = rs.verdict_slots("hero", hero)
    assert s["share"] == "99.8%" and s["hero_class"] == "" and s["route_class"] == ""
    assert "share of pool unknown" in s["support"]
    assert "the pool now holds $102,239,395" in s["support"]
    assert s["route_line"].startswith("▶ MIGRATION · severity rewritten red → amber")
    assert "identical to ten decimals" in s["rows_arith"]
    assert "UNI/WBTC → UNI/WETH" in s["rows_arith"]
    assert s["rows_title"].startswith("The two rows, verbatim")
    assert s["calls_n"] == str(len(hero["calls"]))
    assert "2 min 12 s later" in s["route_line"]


def test_verdict_slots_for_a_consolidation_into_an_unmatched_pool_earlier(hero):
    v = hero["verdict"]
    v["kind"] = "CONSOLIDATION"
    v["destination"]["venue"] = "elsewhere"
    v["destination"]["liquidity_now_usd"] = None
    v["elapsed_s"] = -30
    v["removal_share_of_pool"] = 0.25
    v["evidence"][1]["row"]["a0"] = 1.0
    v["evidence"][1]["row"]["en"] = v["removal"]["en"]
    v["evidence"][1]["row"]["t1s"] = v["removal"]["t1s"]
    v["token"] = {}
    s = rs.verdict_slots("hero", hero)
    assert "a pool created after the removal" in s["support"]
    assert "pool now holds" not in s["support"]
    assert "30 s earlier" in s["support"] and "25.0% of the pool" in s["support"]
    assert "not the same size" in s["rows_arith"] and "same pool ·" in s["rows_arith"]
    assert s["context"].startswith("<b>UNI</b>")


def test_verdict_slots_for_a_rebalance_with_and_without_extra_removals():
    d = real("rebalance")
    s = rs.verdict_slots("rebalance", d)
    assert s["hero_class"] == "rebalance" and "disclosed" not in s["support"]
    assert s["route_line"].startswith("▶ REBALANCE · severity rewritten red → grey · 104.7%")
    d["verdict"]["adjudication"]["other_removals_in_window"] = 3
    assert "removed 3 more time(s)" in rs.verdict_slots("rebalance", d)["support"]
    d["verdict"]["adjudication"] = None
    d["verdict"]["token"] = {}
    d["sym"] = None
    assert rs.verdict_slots("rebalance", d)["context"].startswith("<b>LINK</b>")


def test_verdict_slots_for_an_exit_and_an_incomplete():
    d = real("exit")
    s = rs.verdict_slots("exit", d)
    assert s["hero_class"] == "exit" and s["route_class"] == "exit"
    assert "every follow answered 200" in s["support"]
    assert s["rows_title"] == "The one row — nothing to join"
    assert "nothing to divide → EXIT" in s["rows_arith"]
    assert "on 8 chains, every follow 200" in s["route_line"]
    d["verdict"]["kind"] = "INCOMPLETE"
    d["verdict"]["follows"][2]["complete"] = False
    s = rs.verdict_slots("exit", d)
    assert "did not complete on <b>arbitrum</b>" in s["support"]
    assert (
        s["route_line"]
        == "▶ INCOMPLETE · not an Exit · re-run; the follow was throttled on arbitrum"
    )
    assert s["route_class"] == "exit"


def test_jit_slots_and_pill_describe_the_refusal():
    d = real("jit")
    s = rs.jit_slots("jit", d)
    assert s["hero_class"] == "refused" and s["share"] == "refused"
    assert "just-in-time liquidity" in s["support"]
    assert s["route_line"].startswith("▶ refused · not an event — the same txn 0x3f8c69…f2fd84")
    assert s["calls_n"] == "4"
    assert rs.pill("jit", d) == "DAI $230k<small>refused · JIT</small>"


def test_pill_labels_each_kind_with_the_share_it_can_state(hero):
    assert rs.pill("hero", hero) == "UNI −$21.33M<small>migration · 99.8%</small>"
    assert rs.pill("uni_v3_v4", real("uni_v3_v4")).startswith("UNI v3→v4 −$2.92M")
    assert "rebalance · 105%" in rs.pill("rebalance", real("rebalance"))
    ex = real("exit")
    assert rs.pill("exit", ex).endswith("<small>exit</small>")
    ex["verdict"]["token"] = None
    ex["sym"] = None
    assert rs.pill("exit", ex).startswith(" −$")


def test_hero_viz_draws_the_hairpin_only_when_an_add_exists(verdict):
    svg, key = rs.hero_viz(verdict)
    assert 'class="hairpin"' in svg and svg.count("<rect") == 2
    assert "the amber bar the add into UNI/WETH" in svg
    assert "the same 1,962,475.5391248302 UNI" in key
    verdict["evidence"] = [verdict["evidence"][0]]
    svg, key = rs.hero_viz(verdict)
    assert "hairpin" not in svg and svg.count("<rect") == 1
    assert "nothing re-added by that wallet" in svg
    assert "none by 0x4f0aa5…8bb9a9 within ±6 h on 5 chains" in key
    verdict["removal"]["tu"] = "0"
    svg, _ = rs.hero_viz(verdict)
    assert 'width="28"' in svg


def test_lead_ctx_writes_the_h1_the_title_and_the_description_per_kind(mirror, hero):
    out = rs.lead_ctx(hero)
    assert out["hero.line1"] == "LP removed: $21,330,275 out of UNI/WBTC."
    assert 'data-count="99.8"' in out["hero.line2"] and "was in UNI/WETH" in out["hero.line2"]
    assert out["hero.title"].startswith("Forwarding Address — 99.8% of a $21,330,275 LP removal")
    assert 120 <= len(out["hero.description"]) <= 160
    assert out["hero.date"] == hero["captured_utc"][:10]
    reb = real("rebalance")
    out = rs.lead_ctx(reb)
    assert "back in the same pool" in out["hero.line2"] and "same pool" in out["hero.description"]
    ex = real("exit")
    out = rs.lead_ctx(ex)
    assert "A real exit." in out["hero.line2"] and "nothing came back" in out["hero.description"]
    ex["verdict"]["kind"] = "PARTIAL"
    out = rs.lead_ctx(ex)
    assert "came back within ±6 h" in out["hero.line2"]
    reb["verdict"]["removed_usd"] = 99_257_843
    out = rs.lead_ctx(reb)
    assert "of a $99.26M LP removal was back in the same pool" in out["hero.title"]


def test_lead_ctx_exits_when_no_description_fits_the_window(mirror, hero):
    hero["verdict"]["removal"]["t0s"] = "X" * 90
    with pytest.raises(SystemExit, match="want 120-160"):
        rs.lead_ctx(hero)


def test_findings_ctx_sums_the_sweep_and_counts_the_backoffs(mirror):
    live = real("live_run")
    live["calls"][0]["attempts"] = 2
    retried = sum(1 for c in live["calls"] if c.get("attempts", 1) > 1)
    out = rs.findings_ctx(live, {})
    assert out["find.backoffs"] == str(retried) and retried >= 1
    assert out["find.backoff_schedule"] == "15 / 30 / 60"
    assert out["find.sane_usd"] == "$10B"
    sweep = real("seed_sweep")
    sweep["per_token"] = sweep["per_token"][:2]
    sweep["per_token"][0]["jit_txns"] = 4
    sweep["per_token"][1]["implausible_rows"] = 13
    (rs.PROOF / "seed_sweep.json").write_text(json.dumps(sweep))
    out = rs.findings_ctx(live, {})
    assert out["find.jit_by"] == "4 on UNI" and out["find.implausible_by"] == "LINK on ethereum"
    (rs.PROOF / "seed_sweep.json").unlink()
    out = rs.findings_ctx(live, {})
    assert out["find.sweep_rows"] == "0" and out["find.implausible_by"] == "no token"


def test_feedback_n_counts_the_numbered_headings(mirror):
    assert int(rs.feedback_n()) >= 1
    rs.FEEDBACK_MD.write_text("# t\n\n## 1. a\n\n## 2. b\n\n## not numbered\n")
    assert rs.feedback_n() == "2"


def test_mcp_ctx_reads_the_session_stream_and_zeros_without_it(mirror):
    out = rs.mcp_ctx()
    assert int(out["mcp.tools"]) >= 1 and out["mcp.turns"] != "0"
    rs.MCP_SESSION.write_text('{"name":"mcp__forwarding__investigate"}\n')
    assert rs.mcp_ctx() == {"mcp.tools": "1", "mcp.turns": "0", "mcp.s": "0"}
    rs.MCP_SESSION.unlink()
    assert rs.mcp_ctx() == {"mcp.tools": "0", "mcp.turns": "0", "mcp.s": "0"}


def test_api_rows_counts_only_the_named_endpoints(hero):
    extra = {"calls": [call("/v9/dex/unlisted", {}), call("/v1/dex/token", {})]}
    out = rs.api_rows([hero, extra, {}])
    assert out.count("<tr") == len(rs.ENDPOINTS)
    assert "<tr class=engine>" in out
    assert '<span class="mono">maker=</span>' in out
    n_token = sum(1 for c in hero["calls"] if c["endpoint"] == "/v1/dex/token") + 1
    assert f'<td class="n">{n_token}</td>' in out


def test_term_ctx_replays_the_live_run_through_the_cli_printers():
    live = real("live_run")
    out = rs.term_ctx(live)
    assert '<span class="rd">  ● LP REMOVED' in out
    assert '<span class="hi">  ◆ MIGRATION' in out
    assert '<span class="bl">  the rows (verbatim fields)</span>' in out
    assert '<span class="rd">    remove  ts=' in out and '<span class="hi">    add     ts=' in out
    assert "wrote docs/proof/live_run.json" in out
    assert f"{live['calls_made']} calls" in out


def test_term_ctx_colours_the_lines_under_each_block(monkeypatch):
    live = real("live_run")

    def fake_verdict(v, out=None):
        out.write(
            "  ● LP REMOVED  x\n    maker y\n\n  ◆ EXIT · severity red\n    nothing re-added\n\n"
            "  refused\n    · Solana\n  notes\n    · a note\n    GET /v1/x\n"
        )

    monkeypatch.setattr(forwarding, "print_verdict", fake_verdict)
    out = rs.term_ctx(live).splitlines()
    assert '<span class="cmd">    maker y</span>' in out
    assert '<span class="hi">    nothing re-added</span>' in out
    assert '<span class="dim">    · Solana</span>' in out
    assert '<span class="dim">    GET /v1/x</span>' in out


def test_proof_links_lists_the_base_rate_only_when_it_exists(hero):
    live = real("live_run")
    mcp = {"mcp.tools": "3", "mcp.turns": "4", "mcp.s": "5"}
    with_base = rs.proof_links(hero, live, real("base_rate"), mcp)
    assert "base_rate.json" in with_base and "3 tool calls · 4 turns · 5 s" in with_base
    assert "base_rate.json" not in rs.proof_links(hero, live, None, mcp)


def test_landing_ctx_skips_missing_receipts_and_needs_the_hero_first(mirror, no_subprocess, hero):
    live = real("live_run")
    (rs.PROOF / "runner_up.json").unlink()
    ctx = rs.landing_ctx(hero, live, real("base_rate"), {})
    assert ctx["switcher"].count("<button") == len(rs.SWITCHER) - 1
    assert ctx["version"] == "v1.2.3" and ctx["lp.share"] == "99.8%"
    assert "runner_up" not in ctx["receipts_json"] and "<\\/" in ctx["receipts_json"]
    (rs.PROOF / "hero.json").unlink()
    with pytest.raises(SystemExit, match="hero.json must be the first"):
        rs.landing_ctx(hero, live, None, {})


def test_context_exits_when_a_receipt_is_missing_or_the_two_runs_disagree(mirror, no_subprocess):
    live_path = rs.PROOF / "live_run.json"
    live = json.loads(live_path.read_text())
    live["verdict"]["kind"] = "EXIT"
    live_path.write_text(json.dumps(live))
    with pytest.raises(SystemExit, match="disagree on the verdict"):
        rs.context()
    live_path.unlink()
    with pytest.raises(SystemExit, match="live_run.json is missing"):
        rs.context()
    (rs.PROOF / "hero.json").unlink()
    with pytest.raises(SystemExit, match="hero.json is missing"):
        rs.context()


def test_context_fills_every_slot_from_the_receipts(mirror, no_subprocess):
    ctx = rs.context()
    assert ctx["tests"] == "105" and ctx["tests_live"] == "0"
    assert json.loads((rs.PROOF / "tests.json").read_text())["offline"] == 105
    assert ctx["hero.kind"] == "MIGRATION" and ctx["hero.share_of_pool"] == "an unknown share"
    assert ctx["hero.add_txn_short"] == "0x9fafe3…71a0f3"
    assert "**EXIT** (LINK, 0% recovered)" in ctx["others.summary"]
    assert "**refused** (DAI JIT pair, $229,834)" in ctx["others.summary"]
    assert ctx["jit.sym"] == "DAI" and ctx["endpoints.count"] == "6"
    assert ctx["trigger.rows"] == "300"
    assert ctx["bench.replay_n"] != "0"


def test_context_without_the_optional_receipts_and_with_an_add_that_misses_the_destination(
    mirror, no_subprocess
):
    for name in ("exit", "rebalance", "jit", "base_rate", "bench_replay", "bench_live"):
        (rs.PROOF / f"{name}.json").unlink()
    hero_path = rs.PROOF / "hero.json"
    hero = json.loads(hero_path.read_text())
    hero["verdict"]["removal_share_of_pool"] = 0.5
    hero["verdict"]["destination"]["pair"] = "UNI/NOPE"
    hero["verdict"]["evidence"] = [hero["verdict"]["evidence"][0]]
    hero_path.write_text(json.dumps(hero))
    ctx = rs.context()
    assert ctx["jit.sym"] == "—" and ctx["jit.usd"] == "—"
    assert ctx["other_cards"] == "" and ctx["others.summary"] == ""
    assert ctx["hero.share_of_pool"] == "50.0%"
    assert ctx["hero.add_txn_short"] == ctx["hero.txn_short"]
    assert ctx["bench.replay_n"] == "0" and ctx["base.n"] == "0"
    assert ctx["hero.elapsed_ms"] == "0"


def test_main_writes_the_four_surfaces_then_reports_them_in_sync(
    mirror, no_subprocess, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["render_site.py"])
    rs.main()
    out = capsys.readouterr().out
    assert out.startswith("rendered site/index.html (")
    for p in ("site/index.html", "site/judge.html", "site/pitch/index.html", "JUDGE.md"):
        assert (mirror / p).exists(), p
    assert "{{" not in (mirror / "site" / "index.html").read_text()
    assert "v1.2.3" in (mirror / "site" / "index.html").read_text()
    monkeypatch.setattr(sys, "argv", ["render_site.py", "--check"])
    rs.main()
    out = capsys.readouterr().out
    assert out.startswith("in sync:") and "not compared" not in out


def test_main_check_reports_drift_and_ignores_the_version_when_no_tag_is_reachable(
    mirror, no_subprocess, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["render_site.py", "--check"])
    with pytest.raises(SystemExit) as e:
        rs.main()
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert out.count("DRIFT:") == 4 and "DRIFT: JUDGE.md" in out
    monkeypatch.setattr(sys, "argv", ["render_site.py"])
    rs.main()
    capsys.readouterr()
    monkeypatch.setattr(rs, "version", lambda: rs.VERSION_FALLBACK)
    monkeypatch.setattr(sys, "argv", ["render_site.py", "--check"])
    rs.main()
    assert "the version stamp was not compared" in capsys.readouterr().out


def test_the_script_renders_when_run_as_a_program(mirror, no_subprocess, monkeypatch, capsys):
    src = BUILD / "scripts" / "render_site.py"
    monkeypatch.setattr(sys, "argv", [str(src)])
    ns = {"__name__": "__main__", "__file__": str(mirror / "scripts" / "render_site.py")}
    exec(compile(src.read_text(), str(src), "exec"), ns)
    assert capsys.readouterr().out.startswith("rendered site/index.html")
    assert (mirror / "JUDGE.md").exists()
