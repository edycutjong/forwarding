"""One property-based verification of adjudicate() over its whole input space.

Coverage says the lines ran. This says that across PROPERTY_CASES generated investigations —
any removal size, any mix of adds across the source pool, other pools and other chains, any
completion state — the six invariants in docs/SPEC.md never once failed. The case count is
published in README.md and DEMO.md; test_published_counts pins it.
"""

import json

from conftest import T0, V4, WETH, row
from hypothesis import given, settings
from hypothesis import strategies as st

from forwarding import FULL, KINDS, PARTIAL_MIN, SEVERITY, adjudicate, pool_id

PROPERTY_CASES = 2000

BSC = "0xbf5140a22578168fd562dccf235e5d43a02ce9b1"
usd_amount = st.floats(min_value=0.0, max_value=5e7, allow_nan=False, allow_infinity=False)
offset_s = st.integers(min_value=-6 * 3600, max_value=6 * 3600)
placement = st.sampled_from(["same", "other-venue", "other-pair", "other-chain"])
side = st.sampled_from(["add", "add", "add", "remove"])


def make(placement_, tp, amount, offset):
    ts = T0 + offset * 1000
    signed = amount if tp == "add" else -amount
    if placement_ == "same":
        return {"platform": "ethereum", "row": row(tp, signed, ts=ts)}
    if placement_ == "other-venue":
        return {
            "platform": "ethereum",
            "row": row(tp, signed, ts=ts, f=V4, en="Uniswap v4 (Ethereum)"),
        }
    if placement_ == "other-pair":
        return {"platform": "ethereum", "row": row(tp, signed, ts=ts, t1a=WETH, t1s="WETH")}
    return {"platform": "bsc", "row": row(tp, signed, ts=ts, t0a=BSC)}


adds_strategy = st.lists(
    st.tuples(placement, side, usd_amount, offset_s).map(lambda t: make(*t)), max_size=12
)


@settings(max_examples=PROPERTY_CASES, deadline=None)
@given(
    removed=st.floats(min_value=1.0, max_value=5e7, allow_nan=False, allow_infinity=False),
    adds=adds_strategy,
    complete=st.booleans(),
)
def test_adjudication_invariants_hold_over_the_whole_input_space(removed, adds, complete):
    removal = row("remove", -removed)
    a = adjudicate(removal, adds, source_platform="ethereum", complete=complete)

    # the class is one of the published six, and its severity is the published one
    assert a["kind"] in KINDS
    assert a["severity"] == SEVERITY[a["kind"]]

    # I2 — recovered never exceeds what the wallet added; removes in the window never count
    added = sum(abs(x["row"]["tu"]) for x in adds if x["row"]["tp"] == "add")
    assert a["recovered_usd"] <= added + 1e-6
    assert a["same_pool_usd"] + a["other_pools_usd"] <= added + 1e-6
    assert a["adds_counted"] == sum(1 for x in adds if x["row"]["tp"] == "add")

    # shares are ratios of the removal, and add up
    assert a["same_share"] >= 0 and a["other_share"] >= 0
    assert abs(a["total_share"] - (a["same_share"] + a["other_share"])) < 1e-9
    assert abs(a["same_share"] * removed - a["same_pool_usd"]) < 1e-3
    assert abs(a["other_share"] * removed - a["other_pools_usd"]) < 1e-3

    # the thresholds, exactly as docs/SPEC.md states them
    k = a["kind"]
    if k == "REBALANCE":
        assert a["same_share"] >= FULL and a["same_share"] >= a["other_share"]
        assert a["recovered_share"] == a["same_share"]
    elif k in ("MIGRATION", "CONSOLIDATION"):
        assert a["other_share"] >= FULL
        assert a["recovered_share"] == a["other_share"]
        # I3 — a migration names a destination that is not the source pool
        d = a["destination"]
        assert d is not None
        assert (d["platform"], d["identity"][1], tuple(d["identity"][2])) != (
            "ethereum",
            *pool_id(removal),
        )
        assert d["added_usd"] <= a["other_pools_usd"] + 1e-6
    elif k == "PARTIAL":
        assert PARTIAL_MIN <= a["total_share"]
        assert a["same_share"] < FULL and a["other_share"] < FULL
        assert a["recovered_share"] == a["total_share"]
    elif k == "EXIT":
        # I5/I6 — nothing meaningful found, and every follow completed
        assert a["total_share"] < PARTIAL_MIN and complete is True
    else:
        assert k == "INCOMPLETE"
        assert a["total_share"] < PARTIAL_MIN and complete is False

    # CONSOLIDATION needs a pubAt, and none was supplied here
    assert k != "CONSOLIDATION"

    # I4 — byte-identical on a second run
    again = adjudicate(removal, adds, source_platform="ethereum", complete=complete)
    assert json.dumps(a, sort_keys=True) == json.dumps(again, sort_keys=True)

    # I1 — every float is arithmetic on rows: the removal's own magnitude is echoed exactly
    assert a["removed_usd"] == removed
