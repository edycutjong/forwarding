"""The whole investigation through a routed fake client: every branch the judge can hit."""

import json
from dataclasses import asdict

import forwarding
import pytest
from conftest import (
    HERO_ADD,
    HERO_REMOVE,
    MAKER,
    OTHER,
    T0,
    THROTTLED,
    UNI,
    USDC,
    V3_POOL,
    V4,
    V4_POOL,
    FakeClient,
    envelope,
    ep,
    lc,
    row,
    scenario,
)
from forwarding import NoCandidate, Throttled, investigate, receipt

BSC_UNI = "0xbf5140a22578168fd562dccf235e5d43a02ce9b1"


def endpoints(c):
    return [x["endpoint"] for x in c.calls]


# ── The hero, zero flags ──────────────────────────────────────────────────────────────────────


def test_zero_flags_finds_the_largest_removal_and_follows_it_to_the_v4_pool(hero_routes):
    c = FakeClient(hero_routes)
    v = investigate("ethereum", UNI, client=c)
    assert v.kind == "MIGRATION" and v.severity == "amber"
    assert round(v.recovered_share, 4) == 0.9991
    assert v.removal == HERO_REMOVE  # the row is passed through verbatim
    # every same-maker row in the window, oldest first — the removal itself included
    assert v.evidence == [
        {"platform": "ethereum", "row": HERO_REMOVE},
        {"platform": "ethereum", "row": HERO_ADD},
    ]
    assert v.destination["addr"] == V4_POOL["addr"]
    assert v.destination["liquidity_now_usd"] == 1234567.0  # confirmed by a quotes call
    assert v.elapsed_s == 252
    assert (
        abs(v.removal_share_of_pool - 2921711.1957615116 / (2921711.1957615116 + 1387794.66)) < 1e-9
    )
    assert v.token["sym"] == "UNI" and v.token["cid"] == 7083
    assert v.refused == [] and v.follows[0]["complete"] is True


def test_the_trace_names_every_endpoint_the_investigation_used(hero_routes):
    c = FakeClient(hero_routes)
    investigate("ethereum", UNI, client=c)
    used = endpoints(c)
    assert used[:2] == ["/v1/dex/token", "/v1/dex/liquidity-change/list"]
    assert "/v1/dex/token/pools" in used
    assert used.count("/v1/dex/search") == 2
    assert "/v2/cryptocurrency/info" in used
    assert used.count("/v4/dex/pairs/quotes/latest") == 2  # destination and source depth
    assert all(x["status"] == 200 for x in c.calls)
    assert c.credits == 0


def test_the_receipt_embeds_every_response_under_the_hash_the_trace_cites(hero_routes):
    c = FakeClient(hero_routes, keep_bodies=True)
    v = investigate("ethereum", UNI, client=c)
    r = receipt(v, c, started=0, selection_rule="largest")
    assert r["auth"].startswith("none — CoinMarketCap keyless")
    assert r["credits_used"] == 0 and r["calls_made"] == len(c.calls)
    for call in r["calls"]:
        assert call["sha256"] in r["responses"]
    assert r["verdict"]["removal"]["txn"] == HERO_REMOVE["txn"]
    json.dumps(r)  # serialisable as-is


def test_same_chain_only_makes_no_cross_chain_calls(hero_routes):
    c = FakeClient(hero_routes)
    v = investigate("ethereum", UNI, client=c, cross_chain=False)
    assert v.kind == "MIGRATION"
    assert "/v1/dex/search" not in endpoints(c)
    assert "/v2/cryptocurrency/info" not in endpoints(c)
    assert len(v.follows) == 1


# ── The trigger ───────────────────────────────────────────────────────────────────────────────


def test_a_jit_transaction_named_by_txn_is_refused_as_not_an_event():
    jit_a = row("add", 369_569.0, txn="0xjit", en=None)
    jit_r = row("remove", -369_569.0, txn="0xjit", en=None)
    c = FakeClient(scenario(trigger_rows=[jit_a, jit_r, HERO_REMOVE], maker_rows=[]))
    with pytest.raises(NoCandidate, match="not an event"):
        investigate("ethereum", UNI, txn="0xjit", client=c)


def test_a_jit_transaction_named_by_txn_and_maker_is_refused_too():
    jit_a = row("add", 369_569.0, txn="0xjit")
    jit_r = row("remove", -369_569.0, txn="0xjit")
    c = FakeClient(scenario(trigger_rows=[], maker_rows=[jit_a, jit_r]))
    with pytest.raises(NoCandidate, match="not an event"):
        investigate("ethereum", UNI, txn="0xjit", maker=MAKER, client=c)


def test_jit_pairs_are_discarded_before_the_largest_is_chosen():
    jit_a = row("add", 9_000_000.0, txn="0xjit")
    jit_r = row("remove", -9_000_000.0, txn="0xjit")
    c = FakeClient(
        scenario(trigger_rows=[jit_r, jit_a, HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])
    )
    v = investigate("ethereum", UNI, client=c)
    assert v.removal["txn"] == HERO_REMOVE["txn"]


def test_a_removal_that_is_a_sliver_of_its_pool_is_refused_and_the_next_one_taken():
    """$4M out of a $170M pool is 2.3% — large in dollars, small for the pool (live 2026-09-19:
    a $490k UNI/WETH removal was 2.9% of its pool, and would have out-ranked nothing)."""
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    big_pool = dict(V3_POOL, addr="0xbig", liqUsd="170000000", t1={"addr": weth, "sym": "WETH"})
    sliver = row("remove", -4_000_000.0, ts=T0 + 1000, m=OTHER, t1a=weth, t1s="WETH")
    c = FakeClient(
        scenario(
            trigger_rows=[HERO_REMOVE, sliver],
            maker_rows=[HERO_ADD, HERO_REMOVE],
            pools=(V3_POOL, V4_POOL, big_pool),
        )
    )
    v = investigate("ethereum", UNI, client=c)
    assert v.removal["txn"] == HERO_REMOVE["txn"]
    assert len(v.refused) == 1 and "below the 10% threshold" in v.refused[0]


def test_when_every_removal_is_below_the_share_threshold_there_is_no_candidate():
    huge = dict(V3_POOL, liqUsd="900000000")
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[], pools=(huge,)))
    with pytest.raises(NoCandidate, match="none ≥ 10% of its pool"):
        investigate("ethereum", UNI, client=c)


def test_a_named_txn_is_never_refused_on_share_but_the_share_is_still_reported():
    huge = dict(V3_POOL, liqUsd="900000000")
    c = FakeClient(
        scenario(
            trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE], pools=(huge, V4_POOL)
        )
    )
    v = investigate("ethereum", UNI, txn=HERO_REMOVE["txn"], client=c)
    assert v.kind == "MIGRATION"
    assert v.removal_share_of_pool < 0.01


def test_a_removal_from_a_pool_outside_the_top_twenty_is_kept_with_its_share_unknown():
    c = FakeClient(
        scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE], pools=(V4_POOL,))
    )
    v = investigate("ethereum", UNI, client=c)
    assert v.removal_share_of_pool is None
    assert any("share of pool unknown" in n for n in v.notes)
    assert v.kind == "MIGRATION"


def test_a_txn_not_in_the_trigger_window_says_how_to_find_it():
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[]))
    with pytest.raises(NoCandidate, match="pass --maker"):
        investigate("ethereum", UNI, txn="0xelsewhere", client=c)


def test_txn_with_maker_walks_the_wallet_once_and_reuses_it_as_the_follow():
    c = FakeClient(scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE]))
    v = investigate("ethereum", UNI, txn=HERO_REMOVE["txn"], maker=MAKER, client=c)
    assert v.kind == "MIGRATION"
    same_chain_maker_calls = [
        x
        for x in c.calls
        if x["endpoint"] == "/v1/dex/liquidity-change/list"
        and x["params"].get("maker") == MAKER
        and x["params"]["platform"] == "ethereum"
    ]
    assert len(same_chain_maker_calls) == 1


def test_no_removal_at_all_is_a_finding_not_a_failure():
    c = FakeClient(scenario(trigger_rows=[row("add", 500_000.0)], maker_rows=[]))
    with pytest.raises(NoCandidate, match="no non-JIT removal"):
        investigate("ethereum", UNI, client=c)


def test_a_throttled_trigger_raises_throttled_never_no_data():
    routes = [(ep("/v1/dex/token"), {"data": {"sym": "UNI"}}), (lc(), THROTTLED)]
    with pytest.raises(Throttled):
        investigate("ethereum", UNI, client=FakeClient(routes))


def test_newest_picks_the_most_recent_qualifying_removal_not_the_largest():
    newer_smaller = row("remove", -200_000.0, ts=T0 + 3600_000, m=OTHER, txn="0xnewer")
    routes = scenario(trigger_rows=[HERO_REMOVE, newer_smaller], maker_rows=[HERO_ADD, HERO_REMOVE])
    routes.insert(0, (lc(platform="ethereum", address=UNI, maker=OTHER), envelope([newer_smaller])))
    v = investigate("ethereum", UNI, client=FakeClient(routes), newest=True)
    assert v.removal["txn"] == "0xnewer"
    assert v.kind == "EXIT"


def test_a_missing_token_header_is_a_note_not_a_failure():
    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])
    routes = [(ep("/v1/dex/token"), THROTTLED)] + routes  # shadows the token route
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    assert v.kind == "MIGRATION"
    assert any("token header unavailable" in n for n in v.notes)
    assert v.token["sym"] == "UNI"  # recovered from the search row


# ── Follows, completion, I6 ───────────────────────────────────────────────────────────────────


def test_a_follow_whose_page_was_refused_entirely_is_blocked_and_can_never_be_an_exit():
    """a2a r02 (2026-09-22): the ingestion guard (r01) turned an all-malformed page into a stall,
    and a stall was 'complete' — so a maker whose rows the API mangled would have read EXIT.
    Refused data means we could not look; I6 says that is INCOMPLETE, never an EXIT."""
    mangled = [dict(row("add", 1.0, txn=f"0x{i:02x}", m=MAKER), lgid=None) for i in range(3)]
    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=mangled)
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    assert v.kind == "INCOMPLETE"
    f = v.follows[0]
    assert f["complete"] is False and f["malformed_dropped"] == 3 and f["rows_total"] == 0


def test_a_follow_that_stopped_inside_the_window_has_not_seen_it_and_is_not_an_exit():
    """a2a r02 (2026-09-22, found while verifying): `complete` only said the walk ended without an
    error; a walk that stalled (or spent its page budget) before reaching the window start left
    the older part of the window unseen, and the verdict could still say EXIT. The follow entry
    already carried reached_window_start — it just was not consulted."""
    inside = [
        row("add", 1.0, ts=T0 - 60_000 * (i + 1), m=MAKER, txn=f"0xin{i:02x}", lgid=str(i))
        for i in range(3)
    ]  # every row is newer than the window start; the paged API then repeats itself
    page = [HERO_REMOVE] + inside
    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=page)
    routes = [
        (
            lc(platform="ethereum", address=UNI, maker=MAKER, page=None),
            envelope(page, last_id="C1"),
        ),
        (
            lc(platform="ethereum", address=UNI, maker=MAKER, page="C1"),
            envelope(page, last_id="C2"),
        ),
    ] + routes
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    f = v.follows[0]
    assert f["walk_complete"] is True and f["reached_window_start"] is False
    assert v.kind == "INCOMPLETE"
    # a2a r03: one field, one meaning — the entry's `complete` is the composite, so verify.py and
    # bench.py, which recompute the verdict from all(f["complete"]), apply the rule the receipt
    # was produced under instead of recomputing an EXIT the receipt refused
    assert f["complete"] is False
    assert all(x["complete"] for x in v.follows) is False
    assert (
        forwarding.adjudicate(
            v.removal,
            v.evidence,
            source_platform="ethereum",
            complete=all(x["complete"] for x in v.follows),
        )["kind"]
        == "INCOMPLETE"
    )


def test_an_exit_requires_every_planned_follow_to_have_completed():
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_REMOVE],
        siblings=[("BSC", BSC_UNI, 3_105_047.0)],
        sibling_rows={("bsc", BSC_UNI): []},
    )
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    assert v.kind == "EXIT" and v.severity == "red"
    assert [f["platform"] for f in v.follows] == ["ethereum", "bsc"]
    assert all(f["complete"] for f in v.follows)


def test_a_throttled_sibling_follow_makes_the_verdict_incomplete_never_exit():
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_REMOVE],
        siblings=[("BSC", BSC_UNI, 3_105_047.0)],
        sibling_rows={("bsc", BSC_UNI): THROTTLED},
    )
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    assert v.kind == "INCOMPLETE" and v.severity == "red"
    assert v.follows[1]["complete"] is False and v.follows[1]["throttled"] is True
    assert "throttled" in v.headline() or "INCOMPLETE" in v.headline()


def test_a_throttled_sibling_does_not_erase_a_migration_found_on_the_home_chain():
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_ADD, HERO_REMOVE],
        siblings=[("BSC", BSC_UNI, 3_105_047.0)],
        sibling_rows={("bsc", BSC_UNI): THROTTLED},
    )
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    assert v.kind == "MIGRATION"
    assert v.adjudication["complete"] is False


def test_a_cross_chain_re_add_is_named_with_its_own_chain_pool_and_depth():
    bsc_add = row(
        "add",
        2_900_000.0,
        ts=T0 + 600_000,
        t0a=BSC_UNI,
        f="0xbscfactory",
        en="PancakeSwap v3 (BSC)",
    )
    bsc_pool = {
        "addr": "0xbscpool",
        "pubAt": str(T0 - 10_000_000),
        "t0": {"addr": BSC_UNI, "sym": "UNI"},
        "t1": {"addr": USDC, "sym": "USDC"},
        "exn": "PancakeSwap v3 (BSC)",
        "liqUsd": "5000000",
        "fa": "0xbscfactory",
    }
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_REMOVE],
        siblings=[("BSC", BSC_UNI, 3_105_047.0)],
        sibling_rows={("bsc", BSC_UNI): [bsc_add]},
        liquidity={"0xbscpool": 7_777_777.0},
        extra=[(ep("/v1/dex/token/pools", platform="bsc"), {"data": [bsc_pool]})],
    )
    c = FakeClient(routes)
    v = investigate("ethereum", UNI, client=c)
    assert v.kind == "MIGRATION"
    assert v.destination["platform"] == "bsc"
    assert v.destination["addr"] == "0xbscpool"
    assert v.destination["liquidity_now_usd"] == 7_777_777.0
    assert v.elapsed_s == 600
    quotes = [x for x in c.calls if x["endpoint"] == "/v4/dex/pairs/quotes/latest"]
    assert quotes[0]["params"] == {"network_slug": "bsc", "contract_address": "0xbscpool"}


def test_solana_is_refused_and_an_unregistered_sibling_is_skipped_with_the_reason():
    sol = "8FU95xFJhUUkyyCLU13HSzDLs7oC4QZdXQHL6SCeab36"
    fake = "0x1111111111111111111111111111111111111111"
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_ADD, HERO_REMOVE],
        siblings=[
            ("Solana", sol, 41_693.0),
            ("Polygon", fake, 359_943.0),
            ("BSC", BSC_UNI, 3_105_047.0),
        ],
        registry=[BSC_UNI],
    )
    c = FakeClient(routes)
    v = investigate("ethereum", UNI, client=c)
    assert [f["platform"] for f in v.follows] == ["ethereum", "bsc"]
    assert any("Solana" in r and "not an EVM address" in r for r in v.refused)
    assert any("Polygon" in r and "not in the asset's contract registry" in r for r in v.refused)
    assert not any(x["params"].get("platform") == "polygon" for x in c.calls)


def test_a_chain_with_no_market_for_the_asset_is_not_followed():
    gnosis = "0x4537e328bf7e4efa29d05caea260d7fe26af9d74"
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_ADD, HERO_REMOVE],
        siblings=[("Gnosis", gnosis, 238.19)],
    )
    c = FakeClient(routes)
    v = investigate("ethereum", UNI, client=c)
    assert [f["platform"] for f in v.follows] == ["ethereum"]
    assert any("Gnosis" in r and "below the $10,000 floor" in r for r in v.refused)


def test_the_window_is_enforced_by_the_cursor_walk_not_by_start_time():
    """startTime is plan-gated (403 error 1013 keyless, 2026-09-18); it must never be sent."""
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    investigate("ethereum", UNI, client=c)
    for x in c.calls:
        assert "startTime" not in x["params"] and "endTime" not in x["params"]


def test_the_verdict_serialises_and_the_headline_reads_as_one_sentence(hero_routes):
    v = investigate("ethereum", UNI, client=FakeClient(hero_routes))
    d = asdict(v)
    json.dumps(d)
    assert v.headline().startswith(
        "MIGRATION · 99.9% recovered — $2,918,988 of $2,921,711 into "
        "Uniswap v4 (Ethereum) · UNI/USDC 4 min 12 s later"
    )


# ── watch(): the autonomous loop ──────────────────────────────────────────────────────────────


def test_watch_primes_on_the_first_pass_and_fires_only_on_new_rows(monkeypatch):
    import io

    newer = row("remove", -300_000.0, ts=T0 + 3_600_000, m=OTHER, txn="0xnew")
    pages = [[HERO_REMOVE], [newer, HERO_REMOVE]]
    routes = scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE])
    routes.insert(0, (lc(platform="ethereum", address=UNI, maker=OTHER), envelope([newer])))
    routes.insert(
        0, (lc(platform="ethereum", address=UNI, maker=False), lambda p, q: envelope(pages.pop(0)))
    )
    c = FakeClient(routes)
    out = io.StringIO()
    fired = forwarding.watch(
        [("ethereum", UNI, "UNI")], cycles=2, client=c, out=out, sleep=lambda s: None
    )
    text = out.getvalue()
    assert "primed" in text
    assert len(fired) == 1 and fired[0].removal["txn"] == "0xnew"
    assert fired[0].kind == "EXIT"
    assert "severity rewritten to red" in text


def test_watch_with_one_cycle_fires_on_the_newest_qualifying_removal_as_the_demo():
    import io

    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    out = io.StringIO()
    fired = forwarding.watch([("ethereum", UNI, "UNI")], cycles=1, client=c, out=out)
    assert len(fired) == 1 and fired[0].kind == "MIGRATION"
    assert "severity rewritten to amber" in out.getvalue()


def test_watch_posts_the_verdict_to_a_webhook_when_one_is_set(monkeypatch):
    import io

    posted = []
    monkeypatch.setattr(forwarding, "post_webhook", lambda url, v: posted.append((url, v.kind)))
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    forwarding.watch(
        [("ethereum", UNI, "UNI")], cycles=1, client=c, out=io.StringIO(), webhook="https://hook"
    )
    assert posted == [("https://hook", "MIGRATION")]


def test_watch_skips_a_throttled_token_and_keeps_going():
    import io

    routes = [(ep("/v1/dex/token"), {"data": {}}), (lc(address=UNI), THROTTLED)]
    out = io.StringIO()
    fired = forwarding.watch(
        [("ethereum", UNI, "UNI")], cycles=1, client=FakeClient(routes), out=out
    )
    assert fired == [] and "skipped" in out.getvalue()


def test_implausible_rows_are_refused_before_the_trigger_and_named_in_the_refusals():
    absurd = row("remove", -1e42, ts=T0 + 1000, m=OTHER, en=None, txn="0xabsurd")
    c = FakeClient(scenario(trigger_rows=[absurd, HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    v = investigate("ethereum", UNI, client=c)
    assert v.removal["txn"] == HERO_REMOVE["txn"]
    assert any("price-feed artefact" in x for x in v.refused)


def test_an_implausible_txn_named_by_hash_and_maker_is_refused():
    absurd = row("remove", -1e42, txn="0xabsurd")
    c = FakeClient(scenario(trigger_rows=[], maker_rows=[absurd]))
    with pytest.raises(NoCandidate, match="price-feed artefact"):
        investigate("ethereum", UNI, txn="0xabsurd", maker=MAKER, client=c)


# ── The branches the hero never takes: pools, the registry, the depth call ────────────────────


def test_pools_of_returns_the_error_and_the_investigation_notes_it():
    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])
    routes.insert(0, (ep("/v1/dex/token/pools", platform="ethereum"), THROTTLED))
    c = FakeClient(routes)
    pools, err = forwarding.pools_of(c, "ethereum", UNI)
    assert pools == {} and "429" in err
    v = investigate("ethereum", UNI, client=c)
    assert any(x.startswith("pool list unavailable:") for x in v.notes)
    assert v.removal_share_of_pool is None and v.source_pool is None


def test_pools_of_tolerates_unparseable_liquidity_and_publication_fields():
    from conftest import pool

    odd = pool("0xodd", V3_POOL["fa"], UNI, USDC, 1.0, 0)
    odd["liqUsd"], odd["pubAt"] = "n/a", "yesterday"
    c = FakeClient([(ep("/v1/dex/token/pools"), {"data": [odd]})])
    pools, err = forwarding.pools_of(c, "ethereum", UNI)
    assert err is None
    (entry,) = pools.values()
    assert entry["liqUsd"] == 0.0 and entry["pubAt_ms"] is None


def test_fee_tiers_of_one_identity_collapse_onto_the_deepest_pool():
    from conftest import pool

    thin = pool("0xthin", V3_POOL["fa"], UNI, USDC, 1_000.0, 1)
    deep = pool("0xdeep", V3_POOL["fa"], UNI, USDC, 9_000.0, 1)
    c = FakeClient([(ep("/v1/dex/token/pools"), {"data": [thin, deep]})])
    pools, _ = forwarding.pools_of(c, "ethereum", UNI)
    (entry,) = pools.values()
    assert entry["addr"] == "0xdeep" and entry["fee_tiers"] == 2
    # the other order collapses onto the same pool
    c = FakeClient([(ep("/v1/dex/token/pools"), {"data": [deep, thin]})])
    (entry,) = forwarding.pools_of(c, "ethereum", UNI)[0].values()
    assert entry["addr"] == "0xdeep" and entry["fee_tiers"] == 2


def test_resolve_asset_reports_each_way_the_search_and_the_registry_can_fail():
    search_addr = {"data": {"tks": [{"plt": "Ethereum", "addr": UNI, "cid": 7083, "s": "UNI"}]}}
    bsc = {"plt": "BSC", "addr": BSC_UNI, "cid": 7083, "s": "UNI", "liq": 3_105_047.0}

    # the address search itself fails
    c = FakeClient([(ep("/v1/dex/search", q=UNI), THROTTLED)])
    cid, sym, sibs, refused, notes = forwarding.resolve_asset(c, "ethereum", UNI)
    assert cid is None and sibs == [] and refused[0].startswith("asset resolution failed:")

    # the address is known but carries no CoinMarketCap id
    c = FakeClient([(ep("/v1/dex/search", q=UNI), {"data": {"tks": [{"addr": UNI, "plt": "x"}]}})])
    assert forwarding.resolve_asset(c, "ethereum", UNI)[3] == [
        "asset has no CoinMarketCap id on this chain"
    ]

    # the sibling search fails: the id is kept, nothing is followed
    c = FakeClient([(ep("/v1/dex/search", q=UNI), search_addr), (ep("/v1/dex/search"), THROTTLED)])
    cid, sym, sibs, refused, notes = forwarding.resolve_asset(c, "ethereum", UNI)
    assert cid == 7083 and sym == "UNI" and sibs == []
    assert refused[0].startswith("sibling search failed:")

    # the registry is unavailable: the search rows are followed, and the note says so
    c = FakeClient(
        [
            (ep("/v1/dex/search", q=UNI), search_addr),
            (ep("/v1/dex/search", q="UNI"), {"data": {"tks": [bsc, bsc]}}),
            (ep("/v2/cryptocurrency/info"), THROTTLED),
        ]
    )
    cid, sym, sibs, refused, notes = forwarding.resolve_asset(c, "ethereum", UNI)
    assert [s["platform"] for s in sibs] == ["bsc"]  # the duplicate row is folded
    assert notes[0].startswith("registry unavailable (") and refused == []


def test_pool_liquidity_is_none_when_the_call_fails_or_the_shape_is_wrong():
    c = FakeClient([(ep("/v4/dex/pairs/quotes/latest"), THROTTLED)])
    assert forwarding.pool_liquidity(c, "ethereum", "0xpool") is None
    c = FakeClient([(ep("/v4/dex/pairs/quotes/latest"), {"data": [{"quote": []}]})])
    assert forwarding.pool_liquidity(c, "ethereum", "0xpool") is None
    c = FakeClient(
        [(ep("/v4/dex/pairs/quotes/latest"), {"data": [{"quote": [{"liquidity": "x"}]}]})]
    )
    assert forwarding.pool_liquidity(c, "ethereum", "0xpool") is None


def test_a_txn_named_by_hash_and_maker_raises_when_the_wallet_walk_fails_or_misses():
    c = FakeClient([(ep("/v1/dex/token"), {"data": {}}), (lc(maker=MAKER), THROTTLED)])
    with pytest.raises(Throttled):
        investigate("ethereum", UNI, txn=HERO_REMOVE["txn"], maker=MAKER, client=c)
    permanent = {"_err": "HTTP 400: bad", "_throttled": False, "_status": 400}
    c = FakeClient([(ep("/v1/dex/token"), {"data": {}}), (lc(maker=MAKER), permanent)])
    with pytest.raises(NoCandidate, match="HTTP 400"):
        investigate("ethereum", UNI, txn=HERO_REMOVE["txn"], maker=MAKER, client=c)
    c = FakeClient(scenario(trigger_rows=[], maker_rows=[HERO_ADD]))
    with pytest.raises(NoCandidate, match="is not a removal by"):
        investigate("ethereum", UNI, txn=HERO_REMOVE["txn"], maker=MAKER, client=c)


def test_a_second_removal_by_the_wallet_inside_the_window_is_disclosed_in_the_notes():
    second = row("remove", -400_000.0, ts=T0 + 60_000, txn="0xsecond")
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE, second]))
    v = investigate("ethereum", UNI, client=c)
    assert v.adjudication["other_removals_in_window"] == 1
    assert any("1 other removal(s) totalling $400,000" in x for x in v.notes)


def test_the_headline_for_a_rebalance_and_a_partial_reads_as_one_sentence():
    back = row("add", 2_900_000.0, ts=T0 + 420_000, txn="0xback")
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_REMOVE, back]))
    v = investigate("ethereum", UNI, client=c)
    assert v.kind == "REBALANCE"
    assert v.headline().startswith("REBALANCE · 99.3% put back into the same pool")
    assert v.headline().endswith("(Uniswap v3 (Ethereum) · UNI/USDC) 7 min 00 s later")

    part = row("add", 900_000.0, ts=T0 + 420_000, txn="0xpart", en="Uniswap v4 (Ethereum)", f=V4)
    c = FakeClient(scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_REMOVE, part]))
    v = investigate("ethereum", UNI, client=c)
    assert v.kind == "PARTIAL"
    assert v.headline() == "PARTIAL · 30.8% re-added by the same wallet across its pools"


def test_a_sibling_address_is_none_for_a_chain_that_was_never_followed():
    assert forwarding._sibling_address([{"platform": "ethereum", "address": UNI}], "bsc") is None


def test_the_receipt_takes_extra_top_level_fields(hero_routes):
    import time

    c = FakeClient(hero_routes)
    v = investigate("ethereum", UNI, client=c)
    r = receipt(v, c, started=time.time(), extra={"note": "x"})
    assert r["note"] == "x" and r["verdict"]["kind"] == "MIGRATION"


def test_watch_reports_a_refused_and_a_throttled_investigation_and_keeps_going(monkeypatch):
    import io

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise (NoCandidate("nope") if len(calls) == 1 else Throttled("slow down"))

    monkeypatch.setattr(forwarding, "investigate", boom)
    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])
    out = io.StringIO()
    fired = forwarding.watch(
        [("ethereum", UNI, "UNI")], cycles=1, client=FakeClient(routes), out=out
    )
    assert fired == [] and "refused: nope" in out.getvalue()
    out = io.StringIO()
    fired = forwarding.watch(
        [("ethereum", UNI, "UNI")], cycles=1, client=FakeClient(routes), out=out
    )
    assert fired == [] and "throttled: slow down — will retry next cycle" in out.getvalue()


def test_post_webhook_posts_the_headline_and_the_verdict_and_survives_a_dead_url(
    monkeypatch, hero_routes, capsys
):
    import urllib.error
    import urllib.request

    v = investigate("ethereum", UNI, client=FakeClient(hero_routes))
    seen = {}

    class R:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert forwarding.post_webhook("https://hook", v) == 204
    assert seen["url"] == "https://hook" and seen["body"]["verdict"]["kind"] == "MIGRATION"
    assert seen["body"]["headline"] == v.headline()

    def dead(req, timeout=0):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", dead)
    assert forwarding.post_webhook("https://hook", v) is None
    assert "webhook failed" in capsys.readouterr().err


def test_watch_says_when_a_later_cycle_brings_nothing_new():
    import io

    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])
    out = io.StringIO()
    fired = forwarding.watch(
        [("ethereum", UNI, "UNI")],
        cycles=2,
        client=FakeClient(routes),
        out=out,
        sleep=lambda s: None,
    )
    assert fired == [] and "0 new rows, no qualifying removal" in out.getvalue()


def test_a_cross_chain_destination_whose_pool_list_is_throttled_is_named_without_an_address():
    bsc_add = row(
        "add",
        2_900_000.0,
        ts=T0 + 300_000,
        en="PancakeSwap v3 (BSC)",
        eid=1344,
        f="0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865",
        t0a=BSC_UNI,
        txn="0xbscadd",
    )
    routes = scenario(
        trigger_rows=[HERO_REMOVE],
        maker_rows=[HERO_REMOVE],
        siblings=[("BSC", BSC_UNI, 3_105_047.0)],
        sibling_rows={("bsc", BSC_UNI): [bsc_add]},
    )
    routes.insert(0, (ep("/v1/dex/token/pools", platform="bsc"), THROTTLED))
    v = investigate("ethereum", UNI, client=FakeClient(routes))
    assert v.kind == "MIGRATION" and v.destination["platform"] == "bsc"
    assert v.destination["addr"] is None and "liquidity_now_usd" not in v.destination


def test_watch_with_zero_cycles_polls_nothing():
    import io

    c = FakeClient([])
    assert forwarding.watch([("ethereum", UNI, "UNI")], cycles=0, client=c, out=io.StringIO()) == []
    assert c.calls == []


def test_watch_on_a_token_with_no_rows_primes_an_empty_mark():
    import io

    out = io.StringIO()
    fired = forwarding.watch(
        [("ethereum", UNI, "UNI")], cycles=1, client=FakeClient([(lc(), envelope([]))]), out=out
    )
    assert fired == [] and "0 new rows, no qualifying removal" in out.getvalue()
