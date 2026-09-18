"""The rule, on rows in the live shape. Every threshold in docs/SPEC.md has a test here.

Regression tests are named for the defect they pin, with the date it was seen live.
"""

from conftest import HERO_ADD, HERO_REMOVE, MAKER, T0, UNI, USDC, V3, V4, WETH, row

import forwarding
from forwarding import FULL, PARTIAL_MIN, adjudicate, jit_txns, pool_id, usd, window_rows


def adds(*rows, platform="ethereum"):
    return [{"platform": platform, "row": r} for r in rows]


# ── The hero: the killer number is arithmetic on two `tu` fields ──────────────────────────────


def test_hero_is_a_migration_and_the_share_is_the_two_tu_fields_divided():
    a = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum")
    assert a["kind"] == "MIGRATION"
    assert a["severity"] == "amber"
    assert abs(a["recovered_share"] - 2918987.6118409373 / 2921711.1957615116) < 1e-12
    assert round(a["recovered_share"], 4) == 0.9991
    assert a["elapsed_s"] == 252  # 4 min 12 s, from the two `ts` strings
    assert a["destination"]["venue"] == "Uniswap v4 (Ethereum)"
    assert a["destination"]["pair"] == "UNI/USDC"


def test_a_migration_names_a_destination_that_is_not_the_source_pool():
    """Invariant I3."""
    a = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum")
    assert tuple(a["destination"]["identity"][1:]) != pool_id(HERO_REMOVE)[0:1]
    assert a["destination"]["identity"][1] == V4
    assert pool_id(HERO_REMOVE)[0] == V3


def test_two_runs_on_the_same_rows_are_byte_identical():
    """Invariant I4 — the verdict is a pure function of the rows."""
    import json

    one = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum")
    two = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum")
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


# ── The five classes and their thresholds ─────────────────────────────────────────────────────


def test_rebalance_is_the_same_wallet_back_into_the_same_pool():
    back = row("add", 2132019.35, ts=T0 + 264_000)  # same venue, same pair
    a = adjudicate(row("remove", -2136295.77), adds(back), source_platform="ethereum")
    assert a["kind"] == "REBALANCE"
    assert a["severity"] == "grey"
    assert a["destination"] is None
    assert abs(a["recovered_share"] - 2132019.35 / 2136295.77) < 1e-9
    assert a["elapsed_s"] == 264


def test_a_different_quote_token_on_the_same_venue_is_a_different_pool():
    """2026-09-07 live: UNI/WETH out of v3, UNI/USDC into v3 — a migration across pairs."""
    out = row("remove", -2346316.45, t1a=WETH, t1s="WETH")
    into = row("add", 2338824.04, ts=T0 + 360_000, t1a=USDC, t1s="USDC")
    a = adjudicate(out, adds(into), source_platform="ethereum")
    assert a["kind"] == "MIGRATION"
    assert a["destination"]["pair"] == "UNI/USDC"


def test_the_pair_order_does_not_split_one_pool_into_two():
    """token/pools lists WETH/UNI where the row says UNI/WETH — identity must be unordered."""
    out = row("remove", -100.0, t0a=UNI, t1a=WETH)
    into = row("add", 100.0, ts=T0 + 60_000, t0a=WETH, t1a=UNI)
    assert pool_id(out) == pool_id(into)
    a = adjudicate(out, adds(into), source_platform="ethereum")
    assert a["kind"] == "REBALANCE"


def test_consolidation_is_a_migration_into_a_pool_created_after_the_removal():
    pools = {("ethereum", pool_id(HERO_ADD)): {"addr": "0xnew", "pubAt_ms": T0 + 1, "fee_tiers": 1}}
    a = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum", pools=pools)
    assert a["kind"] == "CONSOLIDATION"
    assert a["severity"] == "amber"


def test_a_destination_older_than_the_removal_stays_a_migration():
    pools = {("ethereum", pool_id(HERO_ADD)): {"addr": "0xold", "pubAt_ms": T0 - 1, "fee_tiers": 1}}
    a = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum", pools=pools)
    assert a["kind"] == "MIGRATION"


def test_partial_is_between_ten_and_seventy_percent_across_all_pools():
    same = row("add", 30_000.0, ts=T0 + 100_000)
    other = row("add", 25_000.0, ts=T0 + 200_000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(same, other), source_platform="ethereum")
    assert a["kind"] == "PARTIAL"
    assert a["severity"] == "amber-red"
    assert abs(a["recovered_share"] - 0.55) < 1e-9  # PARTIAL reports the total put back


def test_exit_is_nothing_found_with_every_follow_complete():
    a = adjudicate(row("remove", -100_000.0), [], source_platform="ethereum", complete=True)
    assert a["kind"] == "EXIT"
    assert a["severity"] == "red"
    assert a["recovered_share"] == 0.0
    assert a["destination"] is None


def test_a_throttled_follow_is_incomplete_never_an_exit():
    """Invariant I6 — the anonymous tier can never manufacture an Exit."""
    a = adjudicate(row("remove", -100_000.0), [], source_platform="ethereum", complete=False)
    assert a["kind"] == "INCOMPLETE"
    assert a["severity"] == "red"


def test_a_throttled_follow_still_reports_a_migration_it_did_find():
    """Positive evidence stands; a throttle can only hide more, never invent less."""
    a = adjudicate(HERO_REMOVE, adds(HERO_ADD), source_platform="ethereum", complete=False)
    assert a["kind"] == "MIGRATION"
    assert a["complete"] is False


def test_the_full_threshold_is_inclusive_at_seventy_percent():
    exact = row("add", 70_000.0, ts=T0 + 1000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(exact), source_platform="ethereum")
    assert a["kind"] == "MIGRATION"
    under = row("add", 69_999.0, ts=T0 + 1000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(under), source_platform="ethereum")
    assert a["kind"] == "PARTIAL"
    assert FULL == 0.70 and PARTIAL_MIN == 0.10


def test_below_ten_percent_re_added_is_an_exit_not_a_partial():
    crumb = row("add", 9_999.0, ts=T0 + 1000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(crumb), source_platform="ethereum")
    assert a["kind"] == "EXIT"


def test_when_both_pools_clear_the_bar_the_larger_share_names_the_class():
    same = row("add", 80_000.0, ts=T0 + 1000)
    other = row("add", 75_000.0, ts=T0 + 2000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(same, other), source_platform="ethereum")
    assert a["kind"] == "REBALANCE"
    a = adjudicate(
        row("remove", -100_000.0),
        adds(
            row("add", 75_000.0, ts=T0 + 1000),
            row("add", 80_000.0, ts=T0 + 2000, f=V4, en="Uniswap v4 (Ethereum)"),
        ),
        source_platform="ethereum",
    )
    assert a["kind"] == "MIGRATION"


def test_removes_by_the_same_wallet_in_the_window_are_never_counted_as_recovery():
    """Invariant I2 — only adds count, and only the maker's."""
    second_remove = row("remove", -500_000.0, ts=T0 + 1000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(second_remove), source_platform="ethereum")
    assert a["kind"] == "EXIT"
    assert a["recovered_usd"] == 0.0


def test_the_destination_is_the_pool_that_received_the_most():
    small = row("add", 20_000.0, ts=T0 + 1000, f=V4, en="Uniswap v4 (Ethereum)")
    big = row("add", 60_000.0, ts=T0 + 5000, t1a=WETH, t1s="WETH")
    a = adjudicate(row("remove", -100_000.0), adds(small, big), source_platform="ethereum")
    assert a["kind"] == "MIGRATION"
    assert a["destination"]["pair"] == "UNI/WETH"
    assert a["destination"]["added_usd"] == 60_000.0
    assert a["elapsed_s"] == 5  # first add INTO THE DESTINATION, not the first add anywhere


def test_an_add_before_the_removal_inside_the_window_reports_a_negative_elapsed():
    early = row("add", 100_000.0, ts=T0 - 90_000, f=V4, en="Uniswap v4 (Ethereum)")
    a = adjudicate(row("remove", -100_000.0), adds(early), source_platform="ethereum")
    assert a["kind"] == "MIGRATION"
    assert a["elapsed_s"] == -90


# ── Cross-chain ───────────────────────────────────────────────────────────────────────────────


def test_an_add_on_another_chain_counts_as_another_pool():
    on_bsc = row("add", 100_000.0, ts=T0 + 1000, t0a="0xbf5140a22578168fd562dccf235e5d43a02ce9b1")
    a = adjudicate(
        row("remove", -100_000.0), adds(on_bsc, platform="bsc"), source_platform="ethereum"
    )
    assert a["kind"] == "MIGRATION"
    assert a["destination"]["platform"] == "bsc"


def test_the_same_factory_and_pair_on_another_chain_is_not_the_same_pool():
    """Uniswap v3's factory has the same address on several chains; the key includes the chain."""
    twin = row("add", 100_000.0, ts=T0 + 1000)  # identical identity fields
    a = adjudicate(
        row("remove", -100_000.0), adds(twin, platform="arbitrum"), source_platform="ethereum"
    )
    assert a["kind"] == "MIGRATION"
    assert a["destination"]["platform"] == "arbitrum"


def test_cross_chain_adds_sum_with_same_chain_adds():
    here = row("add", 40_000.0, ts=T0 + 1000, f=V4, en="Uniswap v4 (Ethereum)")
    there = row("add", 35_000.0, ts=T0 + 2000)
    a = adjudicate(
        row("remove", -100_000.0),
        adds(here) + adds(there, platform="polygon"),
        source_platform="ethereum",
    )
    assert a["kind"] == "MIGRATION"
    assert abs(a["recovered_share"] - 0.75) < 1e-9


# ── JIT and the window ────────────────────────────────────────────────────────────────────────


def test_a_transaction_that_adds_and_removes_is_not_an_event():
    """PEPE, live 2026-09-18: 85 of 100 rows were same-txn add+remove pairs."""
    a = row("add", 369_569.0, txn="0xjit", en=None)  # unlabeled venue, no `en`
    r = row("remove", -369_569.0, txn="0xjit", en=None)
    assert jit_txns([a, r]) == {"0xjit"}
    assert jit_txns([row("add", 1.0), row("remove", -1.0)]) == set()


def test_window_rows_drop_jit_and_keep_only_the_window_oldest_first():
    inside = row("add", 10.0, ts=T0 + 3600_000)
    late = row("add", 10.0, ts=T0 + 7 * 3600_000)
    early = row("add", 10.0, ts=T0 - 7 * 3600_000)
    jit_a = row("add", 5.0, ts=T0 + 10, txn="0xjit")
    jit_r = row("remove", -5.0, ts=T0 + 10, txn="0xjit")
    rows, meta = window_rows(
        [late, inside, early, jit_a, jit_r],
        {"pages": 1, "error": None, "throttled": False},
        t0_ms=T0,
    )
    assert rows == [inside]
    assert meta["jit_dropped"] == 2
    assert meta["rows_in_window"] == 1
    assert meta["reached_window_start"] is True  # `early` is older than the window's start


def test_unlabeled_venues_fall_back_to_the_factory_for_identity_and_naming():
    """75 of 100 PEPE rows carried no `en`/`eid` — the factory is the only stable venue key."""
    r = row("remove", -1.0, en=None, f="0xe48aee124f9ae1b9a8b6e8a1a0e6e5d4c3b2a190")
    assert "en" not in r and "eid" not in r
    assert pool_id(r)[0] == "0xe48aee124f9ae1b9a8b6e8a1a0e6e5d4c3b2a190"
    assert forwarding.venue(r) == "unlabeled DEX (factory 0xe48a…)"


def test_usd_reads_the_magnitude_and_tolerates_a_missing_value():
    assert usd({"tu": -2921711.1957615116}) == 2921711.1957615116
    assert usd({"tu": "12.5"}) == 12.5
    assert usd({}) == 0.0
    assert usd({"tu": "n/a"}) == 0.0


def test_the_maker_field_never_enters_the_arithmetic_only_the_join():
    """A row by ANOTHER wallet must not be handed to adjudicate — follow_maker keys on `m`,
    adjudicate trusts its input. This pins that the fixture rows all carry the hero maker."""
    assert HERO_REMOVE["m"] == HERO_ADD["m"] == MAKER
