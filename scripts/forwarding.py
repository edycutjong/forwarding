#!/usr/bin/env python3
"""Forwarding Address — follow the wallet behind a large LP removal.

Every "LP removed" alert stops at the row that fired it. This tool takes that row and asks the
one question the alert cannot answer: where did the liquidity GO? It follows the maker address
across every pool of the token — on the same chain and on every other EVM chain where the same
CoinMarketCap asset has a contract — and adjudicates between five outcomes:

    REBALANCE      the same wallet put >= 70% back into the SAME pool
    MIGRATION      the same wallet put >= 70% into a DIFFERENT pool      (CONSOLIDATION if that
                                                                          pool was created after
                                                                          the removal)
    PARTIAL        the same wallet re-added 10-70%
    EXIT           nothing found in the window — and every planned follow completed
    INCOMPLETE     a follow was throttled or failed; never reported as EXIT

Runs entirely on CoinMarketCap's KEYLESS /public-api surface. No API key, no signup, no install —
a judge runs this file unmodified:

    python3 scripts/forwarding.py investigate --platform ethereum --address 0x1f98...f984
    python3 scripts/forwarding.py investigate ... --txn 0xf560...4793 --maker 0xc3da...5e56
    python3 scripts/forwarding.py removals    --platform ethereum --address 0x1f98...f984
    python3 scripts/forwarding.py follow      --platform ethereum --address 0x... --maker 0x...
    python3 scripts/forwarding.py watch       --watchlist watchlist.json [--webhook URL]

Exit codes: 0 verdict produced · 3 no qualifying removal · 75 throttled (anonymous tier) · 2 usage.

Verified fields (live, 2026-09-18 and 2026-09-19), /v1/dex/liquidity-change/list:
    m    maker — the transaction sender's own wallet, never the position manager
    tp   'add' | 'remove'
    tu   signed USD value (negative on remove)
    en   venue name (absent on unlabeled DEXes — the factory `f` is always present)
    ts   event time, milliseconds, AS A STRING
    txn, lgid   transaction hash and log index — the row identity is the pair
"""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

BASE = "https://pro-api.coinmarketcap.com/public-api"  # keyless: the default and the judged path
BASE_KEYED = "https://pro-api.coinmarketcap.com"  # only when a key is exported — the escape hatch
KEY_VARS = ("CMC_API_KEY", "COINMARKETCAP_API_KEY", "CMC_PRO_API_KEY")
KEY_URL = "https://coinmarketcap.com/api"
PAGE = 100  # hard cap on the endpoint
RETRIES = 3
BACKOFF_S = 15  # 15 s, 30 s, 60 s — the anonymous tier clears in about a minute
SPACING_S = float(os.environ.get("FORWARDING_SPACING", "2.0"))  # between calls; the proxy lowers it

# ── The published rule (docs/SPEC.md) ─────────────────────────────────────────────────────────
MIN_USD = 100_000.0  # a removal below this is not an alert
MIN_SHARE = 0.10  # ...and neither is one that is < 10% of the pool it left
W_BACK_H = 6.0  # the window the wallet is followed in, before the removal
W_FWD_H = 6.0  # ...and after
FULL = 0.70  # REBALANCE / MIGRATION threshold on the re-added share
PARTIAL_MIN = 0.10  # below this, nothing meaningful came back
TRIGGER_PAGES = 3  # 300 rows >= MIN_USD is the trigger's depth
FOLLOW_PAGES = 10  # a maker's own rows: 1,000 is more than any LP has in +-6 h
SIZE_POOLS = 20  # /v1/dex/token/pools cap per call
SIBLING_MIN_LIQ = 10_000.0  # another chain is followed only if the asset has a market there

KINDS = ("REBALANCE", "MIGRATION", "CONSOLIDATION", "PARTIAL", "EXIT", "INCOMPLETE")
SEVERITY = {
    "REBALANCE": "grey",
    "MIGRATION": "amber",
    "CONSOLIDATION": "amber",
    "PARTIAL": "amber-red",
    "EXIT": "red",
    "INCOMPLETE": "red",
}
EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")

# The default watchlist: 11 mid-caps on 3 chains, the same set the base rate is measured over.
WATCHLIST = [
    ("ethereum", "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", "UNI"),
    ("ethereum", "0x514910771af9ca656af840dff83e8264ecf986ca", "LINK"),
    ("ethereum", "0x57e114b691db790c35207b2e685d4a43181e6061", "ENA"),
    ("ethereum", "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce", "SHIB"),
    ("ethereum", "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "AAVE"),
    ("ethereum", "0xd533a949740bb3306d119cc777fa900ba034cd52", "CRV"),
    ("ethereum", "0xaaee1a9723aadb7afa2810263653a34ba2c21c7a", "MOG"),
    ("ethereum", "0x6b175474e89094c44da98b954eedeac495271d0f", "DAI"),
    ("base", "0x532f27101965dd16442e59d40670faf5ebb142e4", "BRETT"),
    ("base", "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO"),
    ("bsc", "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82", "CAKE"),
]


class Throttled(Exception):
    """The anonymous tier refused the call the whole run depends on. Exit 75, never 'no data'."""


class NoCandidate(Exception):
    """No removal met the rule. A finding, not a failure: exit 3."""


# ── HTTP: keyless GET with backoff, every call receipted ──────────────────────────────────────


def api_key():
    """The optional escape hatch, read from the environment at call time. None = keyless."""
    for var in KEY_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def api_key_var():
    """The NAME of the variable a key came from — the half that is safe to print."""
    for var in KEY_VARS:
        if os.environ.get(var, "").strip():
            return var
    return None


def describe_http_error(e):
    """'HTTP 429 (error 1022): You've reached the limit…' — status, CMC's code, its message."""
    raw = b""
    with contextlib.suppress(Exception):
        raw = e.read()
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw or "")
    code = msg = None
    try:
        body = json.loads(text)
        status = body.get("status") if isinstance(body.get("status"), dict) else body
        code = status.get("error_code")
        msg = status.get("error_message") or status.get("message")
    except (ValueError, AttributeError):
        pass
    head = f"HTTP {e.code}"
    if code not in (None, "", "0", 0):
        head += f" (error {code})"
    if msg:
        return f"{head}: {' '.join(str(msg).split())[:140]}"
    return head


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Client:
    """One GET at a time, keyless by default, with backoff — and a receipt for every call.

    `calls` is the trace a judge reads: endpoint, params, status, ms, credit_count, sha256 of
    the exact bytes received, UTC. With keep_bodies=True the verbatim response is kept under its
    hash, which is how docs/proof/*.json embed the evidence they were computed from.
    """

    def __init__(self, spacing=None, keep_bodies=False, on_call=None, retries=RETRIES):
        self.spacing = SPACING_S if spacing is None else spacing
        self.keep_bodies = keep_bodies
        self.on_call = on_call
        self.retries = retries
        self.calls = []
        self.bodies = {}
        self._last = 0.0

    def _record(self, path, params, status, ms, body=None, raw=b"", error=None):
        sha = hashlib.sha256(raw).hexdigest() if raw else None
        credit = None
        if isinstance(body, dict):
            with contextlib.suppress(TypeError, ValueError, AttributeError):
                credit = int((body.get("status") or {}).get("credit_count"))
        receipt = {
            "endpoint": path,
            "params": {k: str(v) for k, v in params.items()},
            "status": status,
            "ms": ms,
            "credit_count": credit,
            "sha256": sha,
            "utc": utc_now(),
        }
        if error:
            receipt["error"] = error
        self.calls.append(receipt)
        if self.keep_bodies and sha and body is not None:
            self.bodies[sha] = body
        if self.on_call:
            self.on_call(receipt, body)
        return receipt

    def get(self, path, **params):
        """Returns the parsed body, or {"_err": …, "_throttled": bool} — errors are RETURNED.

        429 and any 5xx are transient on the anonymous tier (it reports the same throttle both
        ways) and are retried with backoff; a dropped connection is congestion too. Any other
        4xx is permanent and returns at once. A malformed body is a contract problem, not
        congestion, and is not retried either.
        """
        key = api_key()
        url = (BASE_KEYED if key else BASE) + path + "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if key:
            headers["X-CMC_PRO_API_KEY"] = key
        wait_first = self.spacing - (time.monotonic() - self._last)
        if self._last and wait_first > 0:
            time.sleep(wait_first)
        last = "unknown error"
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    status = resp.status
                ms = int((time.monotonic() - t0) * 1000)
                self._last = time.monotonic()
                try:
                    body = json.loads(raw)
                except ValueError as e:
                    self._record(path, params, status, ms, raw=raw, error=f"malformed JSON: {e}")
                    return {"_err": f"malformed JSON: {e}", "_throttled": False}
                self._record(path, params, status, ms, body=body, raw=raw)
                return body
            except urllib.error.HTTPError as e:
                ms = int((time.monotonic() - t0) * 1000)
                self._last = time.monotonic()
                last = describe_http_error(e)
                transient = e.code == 429 or 500 <= e.code < 600
                if not transient or attempt == self.retries:
                    self._record(path, params, e.code, ms, error=last)
                    return {"_err": last, "_throttled": transient, "_status": e.code}
                wait = BACKOFF_S * (2**attempt)
                print(
                    f"    throttled ({last}) — waiting {wait}s "
                    f"(attempt {attempt + 1}/{self.retries})",
                    file=sys.stderr,
                )
                time.sleep(wait)
            except (
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                ConnectionResetError,
            ) as e:
                ms = int((time.monotonic() - t0) * 1000)
                last = f"{type(e).__name__}: {e}"
                if attempt == self.retries:
                    self._record(path, params, 0, ms, error=last)
                    return {"_err": last, "_throttled": True, "_status": 0}
                wait = BACKOFF_S * (2**attempt)
                print(f"    connection dropped — waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as e:
                ms = int((time.monotonic() - t0) * 1000)
                last = f"{type(e).__name__}: {e}"
                self._record(path, params, 0, ms, error=last)
                return {"_err": last, "_throttled": False, "_status": 0}
        return {"_err": last, "_throttled": True}

    @property
    def credits(self):
        """Credits the envelope claims. Keyless calls report credit_count 1 with no account to
        bill; the receipt therefore reports 0 unless a key was actually sent."""
        if not api_key():
            return 0
        return sum(c.get("credit_count") or 0 for c in self.calls)


# ── Row helpers: every field is read through one of these, once ───────────────────────────────


def ts_ms(row):
    """`ts` is a millisecond epoch delivered as a string."""
    return int(str(row.get("ts")))


def usd(row):
    """|tu| — the USD value; the sign is the side, and `tp` is authoritative for that."""
    try:
        return abs(float(row.get("tu")))
    except (TypeError, ValueError):
        return 0.0


def row_key(row):
    return (row.get("txn"), str(row.get("lgid")))


def pool_id(row):
    """Pool identity from a liquidity-change row: (factory, {token0, token1}).

    Rows carry the venue and the pair, never the pool address — so two fee tiers of one pair on
    one venue collapse into one identity. Stated as a limit, not guessed around. The factory
    `f` is used rather than the name `en` because `en` is absent on unlabeled venues.
    """
    return (
        str(row.get("f") or "").lower(),
        tuple(sorted((str(row.get("t0a") or "").lower(), str(row.get("t1a") or "").lower()))),
    )


def venue(row):
    return row.get("en") or f"unlabeled DEX (factory {str(row.get('f') or '')[:6]}…)"


def pair(row):
    return f"{row.get('t0s')}/{row.get('t1s')}"


def jit_txns(rows):
    """Transactions that both add and remove — just-in-time liquidity, not an event."""
    sides = {}
    for r in rows:
        sides.setdefault(r.get("txn"), set()).add(r.get("tp"))
    return {t for t, s in sides.items() if {"add", "remove"} <= s}


def short(addr, n=4):
    a = str(addr or "")
    return a if len(a) <= 2 + 2 * n + 1 else f"{a[: 2 + n]}…{a[-n:]}"


def fmt_utc(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ms / 1000))


def fmt_elapsed(s):
    if s is None:
        return "—"
    sign = "−" if s < 0 else ""
    s = abs(int(s))
    if s < 60:
        return f"{sign}{s} s"
    if s < 3600:
        return f"{sign}{s // 60} min {s % 60:02d} s"
    return f"{sign}{s // 3600} h {(s % 3600) // 60:02d} min"


# ── The feed: cursor walk with stall detection ────────────────────────────────────────────────


def walk(client, params, pages, until=None):
    """Paginate /v1/dex/liquidity-change/list by the envelope's data.lastId.

    Rows are keyed by (txn, lgid); a page that adds nothing new is a stall and stops the walk
    rather than spinning. `until(page_rows)` returning True stops after that page — the window
    walk uses it to stop once the page reaches back past the window. Returns (rows, meta):
    meta.error is None on a clean walk, meta.complete says whether the walk ended for a good
    reason (exhausted, stopped by `until`, or the page budget) rather than an error.
    """
    seen, out, cursor = set(), [], None
    meta = {
        "pages": 0,
        "error": None,
        "throttled": False,
        "stalled": False,
        "exhausted": False,
        "stopped": False,
        "tlu": None,
        "lpc": None,
    }
    for _ in range(pages):
        q = dict(params, limit=PAGE)
        if cursor:
            q["lastId"] = cursor
        d = client.get("/v1/dex/liquidity-change/list", **q)
        if "_err" in d:
            meta["error"], meta["throttled"] = d["_err"], bool(d.get("_throttled"))
            break
        data = d.get("data") or {}
        meta["tlu"] = data.get("tlu", meta["tlu"])
        meta["lpc"] = data.get("lpc", meta["lpc"])
        batch = data.get("lcs") or []
        meta["pages"] += 1
        fresh = []
        for r in batch:
            k = row_key(r)
            if k in seen:
                continue
            seen.add(k)
            fresh.append(r)
        out += fresh
        if not batch:
            meta["exhausted"] = True
            break
        if not fresh:
            meta["stalled"] = True
            break
        cursor = data.get("lastId")
        if not cursor:
            meta["exhausted"] = True
            break
        if until and until(fresh):
            meta["stopped"] = True
            break
    meta["complete"] = meta["error"] is None
    return out, meta


def largest_removals(client, platform, address, *, min_usd=MIN_USD, pages=TRIGGER_PAGES):
    """Non-JIT removals >= min_usd, largest first. Returns (removals, meta, jit_pairs)."""
    rows, meta = walk(
        client, {"platform": platform, "address": address, "minVolume": int(min_usd)}, pages
    )
    jit = jit_txns(rows)
    removals = [
        r for r in rows if r.get("tp") == "remove" and r.get("txn") not in jit and usd(r) >= min_usd
    ]
    removals.sort(key=usd, reverse=True)
    meta["rows"] = len(rows)
    meta["jit_txns"] = len(jit)
    return removals, meta, [r for r in rows if r.get("txn") in jit]


def follow_maker(
    client, platform, address, maker, *, t0_ms, back_h=W_BACK_H, fwd_h=W_FWD_H, pages=FOLLOW_PAGES
):
    """THE JOIN. Every liquidity event by `maker` on `address`, inside t0 -+ the window.

    One keyless call per page (`maker=` is a server-side filter), walked until the page reaches
    back past the window. JIT transactions are dropped. Returns (rows_in_window, meta) —
    meta.complete is False if any page failed, and I6 says a follow that did not complete can
    never contribute to an EXIT.
    """
    lo = t0_ms - int(back_h * 3600_000)
    rows, meta = walk(
        client,
        {"platform": platform, "address": address, "maker": maker},
        pages,
        until=lambda page: min(ts_ms(r) for r in page) < lo,
    )
    return window_rows(rows, meta, t0_ms=t0_ms, back_h=back_h, fwd_h=fwd_h)


def window_rows(rows, meta, *, t0_ms, back_h=W_BACK_H, fwd_h=W_FWD_H):
    """Cut a maker's rows down to the window, JIT excluded, oldest first; annotate meta."""
    lo, hi = t0_ms - int(back_h * 3600_000), t0_ms + int(fwd_h * 3600_000)
    jit = jit_txns(rows)
    inside = [r for r in rows if lo <= ts_ms(r) <= hi and r.get("txn") not in jit]
    inside.sort(key=ts_ms)
    meta = dict(meta)
    meta["rows_total"] = len(rows)
    meta["rows_in_window"] = len(inside)
    meta["jit_dropped"] = sum(1 for r in rows if r.get("txn") in jit)
    meta["reached_window_start"] = bool(
        meta.get("exhausted") or meta.get("stopped") or (rows and min(map(ts_ms, rows)) < lo)
    )
    meta["window"] = {"from_ms": lo, "to_ms": hi}
    return inside, meta


# ── Pools, cross-chain resolution, confirmation ───────────────────────────────────────────────


def pools_of(client, platform, address):
    """The token's pools on one chain: identity → pool (largest liquidity wins a fee-tier tie)."""
    d = client.get("/v1/dex/token/pools", platform=platform, address=address, size=SIZE_POOLS)
    if "_err" in d:
        return {}, d["_err"]
    out = {}
    for p in d.get("data") or []:
        t0, t1 = p.get("t0") or {}, p.get("t1") or {}
        ident = (
            str(p.get("fa") or "").lower(),
            tuple(sorted((str(t0.get("addr") or "").lower(), str(t1.get("addr") or "").lower()))),
        )
        try:
            liq = float(p.get("liqUsd") or 0)
        except (TypeError, ValueError):
            liq = 0.0
        try:
            pub = int(str(p.get("pubAt"))) if p.get("pubAt") else None
        except ValueError:
            pub = None
        entry = {
            "addr": p.get("addr"),
            "venue": p.get("exn"),
            "pair": f"{t0.get('sym')}/{t1.get('sym')}",
            "liqUsd": liq,
            "pubAt_ms": pub,
            "fee_tiers": 1,
        }
        cur = out.get(ident)
        if cur is None:
            out[ident] = entry
        else:
            cur["fee_tiers"] += 1
            if liq > cur["liqUsd"]:
                entry["fee_tiers"] = cur["fee_tiers"]
                out[ident] = entry
    return out, None


def resolve_asset(client, platform, address):
    """(cid, symbol, sibling contracts of the same CMC asset on other EVM chains, refused[]).

    dex/search by address gives the asset id; dex/search by symbol lists the same id on every
    chain; v2/cryptocurrency/info is the canonical registry the search rows are checked against —
    an address absent from the registry is skipped and reported, never followed.
    """
    refused, notes = [], []
    d = client.get("/v1/dex/search", q=address, limit=10)
    if "_err" in d:
        return None, None, [], [f"asset resolution failed: {d['_err']}"], notes
    rows = (d.get("data") or {}).get("tks") or []
    mine = [r for r in rows if str(r.get("addr", "")).lower() == address.lower()]
    on_chain = [r for r in mine if str(r.get("plt", "")).lower() == platform.lower()]
    row = (on_chain or [r for r in mine if r.get("cid")] or [None])[0]
    if not row or not row.get("cid"):
        return None, None, [], ["asset has no CoinMarketCap id on this chain"], notes
    cid, sym = int(row["cid"]), row.get("s")

    d = client.get("/v1/dex/search", q=sym, limit=50)
    if "_err" in d:
        return cid, sym, [], [f"sibling search failed: {d['_err']}"], notes
    siblings = [
        r
        for r in (d.get("data") or {}).get("tks") or []
        if r.get("cid") == cid and str(r.get("addr", "")).lower() != address.lower()
    ]

    d = client.get("/v2/cryptocurrency/info", id=cid)
    registry = set()
    if "_err" in d:
        notes.append(f"registry unavailable ({d['_err']}); search rows followed unchecked")
        registry = None
    else:
        for c in ((d.get("data") or {}).get(str(cid)) or {}).get("contract_address") or []:
            registry.add(str(c.get("contract_address", "")).lower())

    out, seen = [], set()
    for r in siblings:
        slug = str(r.get("plt", "")).lower()
        addr = str(r.get("addr", ""))
        if (slug, addr.lower()) in seen:
            continue
        seen.add((slug, addr.lower()))
        if not EVM_ADDR.match(addr):
            refused.append(
                f"{r.get('plt')}: {short(addr, 6)} is not an EVM address — "
                "a maker there cannot be this wallet"
            )
            continue
        if registry is not None and addr.lower() not in registry:
            refused.append(
                f"{r.get('plt')}: {short(addr, 6)} is not in the asset's contract registry "
                "— skipped"
            )
            continue
        liq = _float(r.get("liq")) or 0.0
        if liq < SIBLING_MIN_LIQ:
            refused.append(
                f"{r.get('plt')}: {short(addr, 6)} holds ${liq:,.0f} of DEX liquidity — "
                f"below the ${SIBLING_MIN_LIQ:,.0f} floor, not followed"
            )
            continue
        out.append({"platform": slug, "address": addr, "liq": liq})
    return cid, sym, out, refused, notes


def pool_liquidity(client, platform, pool_addr):
    """Destination depth now, from /v4/dex/pairs/quotes/latest (quote[0].liquidity)."""
    d = client.get("/v4/dex/pairs/quotes/latest", network_slug=platform, contract_address=pool_addr)
    if "_err" in d:
        return None
    try:
        return float((d.get("data") or [{}])[0]["quote"][0]["liquidity"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ── Adjudication: pure arithmetic on API rows, nothing else ───────────────────────────────────


def adjudicate(removal, adds, *, source_platform, complete=True, pools=None):
    """Classify one removal from the adds the same wallet made inside the window.

    `adds` is a list of {"platform": slug, "row": verbatim add row}. JIT transactions must have
    been excluded before this is called (follow_maker does it). Every number here is a sum or a
    ratio of `tu` fields — invariant I1: the model never produces a number.
    """
    removed = usd(removal)
    src = (source_platform, pool_id(removal))
    same = other = 0.0
    by_dest, first_ts = {}, {}
    for a in adds:
        r = a["row"]
        if r.get("tp") != "add":
            continue
        key = (a["platform"], pool_id(r))
        u = usd(r)
        if key == src:
            same += u
        else:
            other += u
            by_dest[key] = by_dest.get(key, 0.0) + u
            first_ts[key] = min(first_ts.get(key, ts_ms(r)), ts_ms(r))
    same_share = same / removed if removed else 0.0
    other_share = other / removed if removed else 0.0
    total_share = same_share + other_share
    dest_key = max(by_dest, key=by_dest.get) if by_dest else None
    dest = None
    if dest_key:
        info = (pools or {}).get(dest_key) or {}
        dest_rows = [a["row"] for a in adds if (a["platform"], pool_id(a["row"])) == dest_key]
        dest = {
            "platform": dest_key[0],
            "venue": venue(dest_rows[0]),
            "pair": pair(dest_rows[0]),
            "identity": [dest_key[0], dest_key[1][0], list(dest_key[1][1])],
            "addr": info.get("addr"),
            "pubAt_ms": info.get("pubAt_ms"),
            "fee_tiers_collapsed": info.get("fee_tiers", 1),
            "added_usd": by_dest[dest_key],
            "first_add_ms": first_ts[dest_key],
        }
    if same_share >= FULL and same_share >= other_share:
        kind = "REBALANCE"
    elif other_share >= FULL:
        kind = "MIGRATION"
        if dest and dest["pubAt_ms"] and dest["pubAt_ms"] > ts_ms(removal):
            kind = "CONSOLIDATION"
    elif total_share >= PARTIAL_MIN:
        kind = "PARTIAL"
    elif complete:
        kind = "EXIT"
    else:
        kind = "INCOMPLETE"
    if kind == "REBALANCE":
        recovered_share, recovered = same_share, same
    elif kind == "PARTIAL":
        recovered_share, recovered = total_share, same + other
    else:
        recovered_share, recovered = other_share, other
    elapsed = None
    if dest and kind in ("MIGRATION", "CONSOLIDATION", "PARTIAL"):
        elapsed = (dest["first_add_ms"] - ts_ms(removal)) // 1000
    elif kind == "REBALANCE":
        same_rows = [a["row"] for a in adds if (a["platform"], pool_id(a["row"])) == src]
        if same_rows:
            elapsed = (min(ts_ms(r) for r in same_rows) - ts_ms(removal)) // 1000
    return {
        "kind": kind,
        "severity": SEVERITY[kind],
        "removed_usd": removed,
        "recovered_usd": recovered,
        "recovered_share": recovered_share,
        "same_pool_usd": same,
        "same_share": same_share,
        "other_pools_usd": other,
        "other_share": other_share,
        "total_share": total_share,
        "destination": dest,
        "elapsed_s": elapsed,
        "complete": complete,
        "adds_counted": sum(1 for a in adds if a["row"].get("tp") == "add"),
    }


# ── The investigation ─────────────────────────────────────────────────────────────────────────


@dataclass
class Verdict:
    kind: str
    severity: str
    recovered_share: float
    removed_usd: float
    recovered_usd: float
    token: dict
    removal: dict
    evidence: list  # [{"platform": slug, "row": verbatim}] — every same-maker row in the window
    destination: dict | None
    elapsed_s: int | None
    removal_share_of_pool: float | None
    source_pool: dict | None
    follows: list  # one entry per planned follow, with its completion state
    refused: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    adjudication: dict = field(default_factory=dict)
    window_h: dict = field(default_factory=lambda: {"back": W_BACK_H, "fwd": W_FWD_H})
    rule: dict = field(default_factory=lambda: {"full": FULL, "partial_min": PARTIAL_MIN})

    def headline(self):
        pct = f"{self.recovered_share * 100:.1f}%"
        if self.kind in ("MIGRATION", "CONSOLIDATION"):
            d = self.destination
            return (
                f"{self.kind} · {pct} recovered — ${self.recovered_usd:,.0f} of "
                f"${self.removed_usd:,.0f} into {d['venue']} · {d['pair']} "
                f"{fmt_elapsed(self.elapsed_s)} "
                f"{'later' if (self.elapsed_s or 0) >= 0 else 'earlier'}"
            )
        if self.kind == "REBALANCE":
            return (
                f"REBALANCE · {pct} put back into the same pool "
                f"({venue(self.removal)} · {pair(self.removal)}) "
                f"{fmt_elapsed(self.elapsed_s)} later"
            )
        if self.kind == "PARTIAL":
            return f"PARTIAL · {pct} re-added by the same wallet across its pools"
        if self.kind == "EXIT":
            n = len(self.follows)
            return (
                f"EXIT · nothing re-added by this wallet within ±{self.window_h['back']:.0f} h on "
                f"{n} chain{'s' if n != 1 else ''} searched"
            )
        return "INCOMPLETE · a follow was throttled — re-run; this is not an Exit"


def investigate(
    platform,
    address,
    *,
    txn=None,
    maker=None,
    min_usd=MIN_USD,
    min_share=MIN_SHARE,
    cross_chain=True,
    back_h=W_BACK_H,
    fwd_h=W_FWD_H,
    client=None,
    trigger_pages=TRIGGER_PAGES,
    newest=False,
):
    """trigger → JIT filter → follow the maker → pools → cross-chain → adjudicate → confirm.

    Raises NoCandidate (exit 3) when nothing meets the rule and Throttled (exit 75) when the
    trigger itself cannot be fetched. Every other failure degrades honestly: a throttled follow
    yields INCOMPLETE, a missing pool yields "share unknown", never a made-up number.
    """
    c = client or Client()
    refused, notes = [], []

    # 0. header: name, symbol, liquidity — context, never an input to the verdict
    token = {"platform": platform, "address": address, "sym": None, "name": None, "liqUsd": None}
    d = c.get("/v1/dex/token", platform=platform, address=address)
    if "_err" not in d:
        t = d.get("data") or {}
        token.update({"sym": t.get("sym"), "name": t.get("n"), "liqUsd": _float(t.get("liqUsd"))})
    else:
        notes.append(f"token header unavailable: {d['_err']}")

    # 1. trigger
    pre = None  # a maker walk already made — reused as the same-chain follow, not repeated
    if txn and maker:
        rows, meta = walk(
            c, {"platform": platform, "address": address, "maker": maker}, FOLLOW_PAGES
        )
        if meta["error"] and not rows:
            raise Throttled(meta["error"]) if meta["throttled"] else NoCandidate(meta["error"])
        pre = (rows, meta)
        jit = jit_txns(rows)
        hits = [r for r in rows if r.get("txn") == txn and r.get("tp") == "remove"]
        if not hits:
            raise NoCandidate(
                f"txn {short(txn, 6)} is not a removal by {short(maker)} on this token"
            )
        if txn in jit:
            raise NoCandidate(
                f"txn {short(txn, 6)} is not an event — it adds and removes in the same "
                "transaction (JIT)"
            )
        candidates = sorted(hits, key=usd, reverse=True)
        jit_rows = []
    else:
        candidates, meta, jit_rows = largest_removals(
            c, platform, address, min_usd=min_usd, pages=trigger_pages
        )
        if meta["error"] and not meta["rows"]:
            raise Throttled(meta["error"]) if meta["throttled"] else NoCandidate(meta["error"])
        if txn:
            in_jit = any(r.get("txn") == txn for r in jit_rows)
            if in_jit:
                raise NoCandidate(
                    f"txn {short(txn, 6)} is not an event — it adds and removes in the same "
                    "transaction (JIT)"
                )
            candidates = [r for r in candidates if r.get("txn") == txn]
            if not candidates:
                raise NoCandidate(
                    f"txn {short(txn, 6)} is not among the last {meta['rows']} removals ≥ "
                    f"${min_usd:,.0f} — pass --maker to find it by wallet"
                )
        elif newest:
            candidates.sort(key=ts_ms, reverse=True)
    if not candidates:
        raise NoCandidate(
            f"no non-JIT removal ≥ ${min_usd:,.0f} in the last {meta.get('rows', 0)} rows "
            f"({meta.get('jit_txns', 0)} JIT transactions discarded)"
        )

    # 2. pools: identity, liquidity now, pubAt — and the removal's share of its pool
    pools, perr = pools_of(c, platform, address)
    if perr:
        notes.append(f"pool list unavailable: {perr}")
    removal, share, source_pool = None, None, None
    for r in candidates:
        p = pools.get(pool_id(r))
        s = usd(r) / (usd(r) + p["liqUsd"]) if p and p["liqUsd"] is not None else None
        if txn or s is None or s >= min_share:
            removal, share, source_pool = r, s, p
            if s is None and pools:
                notes.append(
                    "share of pool unknown — the pool is not among the token's "
                    f"{SIZE_POOLS} largest, so it now holds less than the smallest of them"
                )
            break
        refused.append(
            f"{fmt_utc(ts_ms(r))} remove ${usd(r):,.0f} from {venue(r)} {pair(r)}: "
            f"{s * 100:.1f}% of a ${p['liqUsd']:,.0f} pool is below the "
            f"{min_share * 100:.0f}% threshold"
        )
    if removal is None:
        raise NoCandidate(
            f"{len(candidates)} removal(s) ≥ ${min_usd:,.0f} found, none ≥ {min_share * 100:.0f}% "
            "of its pool — large in dollars, small for the pool"
        )
    t0 = ts_ms(removal)
    maker = removal.get("m")

    # 3. follow the wallet on this chain — the join, one call
    follows, adds = [], []
    if pre:
        rows, fmeta = window_rows(pre[0], pre[1], t0_ms=t0, back_h=back_h, fwd_h=fwd_h)
    else:
        rows, fmeta = follow_maker(
            c, platform, address, maker, t0_ms=t0, back_h=back_h, fwd_h=fwd_h
        )
    follows.append(_follow_entry(platform, address, rows, fmeta))
    adds += [{"platform": platform, "row": r} for r in rows]

    # 4. the same asset on other chains
    cid, sym = None, token.get("sym")
    if cross_chain:
        cid, sym2, siblings, ref, nts = resolve_asset(c, platform, address)
        sym = sym or sym2
        refused += ref
        notes += nts
        for s in siblings:
            srows, smeta = follow_maker(
                c, s["platform"], s["address"], maker, t0_ms=t0, back_h=back_h, fwd_h=fwd_h
            )
            follows.append(_follow_entry(s["platform"], s["address"], srows, smeta))
            adds += [{"platform": s["platform"], "row": r} for r in srows]
    token["cid"] = cid
    token["sym"] = sym

    # 5. adjudicate — pure arithmetic on the rows above
    complete = all(f["complete"] for f in follows)
    keyed_pools = {(platform, k): v for k, v in pools.items()}
    a = adjudicate(removal, adds, source_platform=platform, complete=complete, pools=keyed_pools)
    dest = a["destination"]
    if dest and dest["platform"] != platform and dest["addr"] is None:
        # a cross-chain destination: name it from that chain's pool list (one more call)
        other_pools, oerr = pools_of(
            c, dest["platform"], _sibling_address(follows, dest["platform"])
        )
        if not oerr:
            keyed_pools.update({(dest["platform"], k): v for k, v in other_pools.items()})
            a = adjudicate(
                removal, adds, source_platform=platform, complete=complete, pools=keyed_pools
            )
            dest = a["destination"]

    # 6. confirm the depth now — the pool that holds it, and the one it left
    if dest and dest.get("addr"):
        dest["liquidity_now_usd"] = pool_liquidity(c, dest["platform"], dest["addr"])
    if source_pool and source_pool.get("addr"):
        source_pool = dict(source_pool)
        source_pool["liquidity_now_usd"] = pool_liquidity(c, platform, source_pool["addr"])
        source_pool["identity"] = [platform, pool_id(removal)[0], list(pool_id(removal)[1])]

    return Verdict(
        kind=a["kind"],
        severity=a["severity"],
        recovered_share=a["recovered_share"],
        removed_usd=a["removed_usd"],
        recovered_usd=a["recovered_usd"],
        token=token,
        removal=removal,
        evidence=adds,
        destination=dest,
        elapsed_s=a["elapsed_s"],
        removal_share_of_pool=share,
        source_pool=source_pool,
        follows=follows,
        refused=refused,
        notes=notes,
        adjudication={k: v for k, v in a.items() if k not in ("destination",)},
        window_h={"back": back_h, "fwd": fwd_h},
    )


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _follow_entry(platform, address, rows, meta):
    return {
        "platform": platform,
        "address": address,
        "rows_in_window": len(rows),
        "rows_total": meta.get("rows_total", 0),
        "pages": meta["pages"],
        "complete": meta["complete"],
        "reached_window_start": meta.get("reached_window_start", False),
        "jit_dropped": meta.get("jit_dropped", 0),
        "error": meta["error"],
        "throttled": meta["throttled"],
    }


def _sibling_address(follows, platform):
    for f in follows:
        if f["platform"] == platform:
            return f["address"]
    return None


def receipt(verdict, client, *, started, selection_rule=None, extra=None):
    """The JSON a run writes: the verdict, the verbatim rows, every call, every response."""
    out = {
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "wall_clock_s": round(time.time() - started, 2),
        "auth": (
            f"X-CMC_PRO_API_KEY from ${api_key_var()} — keyed escape hatch, not the default path"
            if api_key_var()
            else "none — CoinMarketCap keyless /public-api surface"
        ),
        "credits_used": client.credits,
        "calls_made": len(client.calls),
        "selection_rule": selection_rule,
        "verdict": asdict(verdict),
        "calls": client.calls,
        "responses": client.bodies,
    }
    if extra:
        out.update(extra)
    return out


# ── The autonomous loop ───────────────────────────────────────────────────────────────────────


def watch(
    watchlist,
    *,
    min_usd=MIN_USD,
    min_share=MIN_SHARE,
    interval_s=60,
    cycles=None,
    webhook=None,
    cross_chain=True,
    client=None,
    out=None,
    sleep=time.sleep,
):
    """Poll each watched token; every NEW qualifying removal is investigated as it lands.

    State is a per-token high-water mark on (ts, txn, lgid): rows at or below it were seen. The
    first pass primes the mark without firing, so a fresh start does not replay history — unless
    cycles=1, where the newest qualifying removal on each token IS the demo.
    """
    c = client or Client()
    out = out or sys.stdout
    seen = {}
    fired = []
    n = 0
    while cycles is None or n < cycles:
        n += 1
        print(f"[{utc_now()}] cycle {n} · {len(watchlist)} tokens · ≥ ${min_usd:,.0f}", file=out)
        for platform, address, sym in watchlist:
            rows, meta = walk(
                c, {"platform": platform, "address": address, "minVolume": int(min_usd)}, 1
            )
            if meta["error"]:
                print(f"  {sym:6s} {platform:9s} skipped — {meta['error']}", file=out)
                continue
            mark = seen.get((platform, address))
            fresh = [r for r in rows if mark is None or (ts_ms(r), row_key(r)) > mark]
            if rows:
                seen[(platform, address)] = max((ts_ms(r), row_key(r)) for r in rows)
            jit = jit_txns(rows)
            events = [
                r
                for r in fresh
                if r.get("tp") == "remove" and r.get("txn") not in jit and usd(r) >= min_usd
            ]
            if mark is None and cycles != 1:
                print(
                    f"  {sym:6s} {platform:9s} primed — {len(rows)} rows, {len(jit)} JIT txns",
                    file=out,
                )
                continue
            if not events:
                print(
                    f"  {sym:6s} {platform:9s} {len(fresh)} new rows, no qualifying removal",
                    file=out,
                )
                continue
            for r in sorted(events, key=usd, reverse=True)[: (1 if cycles == 1 else None)]:
                print(
                    f"  {sym:6s} {platform:9s} ● LP REMOVED ${usd(r):,.0f} {venue(r)} {pair(r)} "
                    f"maker {short(r.get('m'))} — following",
                    file=out,
                )
                try:
                    v = investigate(
                        platform,
                        address,
                        txn=r.get("txn"),
                        maker=r.get("m"),
                        min_usd=min_usd,
                        min_share=min_share,
                        cross_chain=cross_chain,
                        client=c,
                    )
                except NoCandidate as e:
                    print(f"         refused: {e}", file=out)
                    continue
                except Throttled as e:
                    print(f"         throttled: {e} — will retry next cycle", file=out)
                    continue
                print(f"         ◆ {v.headline()}  → severity rewritten to {v.severity}", file=out)
                fired.append(v)
                if webhook:
                    post_webhook(webhook, v)
        if cycles is not None and n >= cycles:
            break
        sleep(interval_s)
    return fired


def post_webhook(url, verdict):
    body = json.dumps(
        {"headline": verdict.headline(), "verdict": asdict(verdict)}, default=str
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except (urllib.error.URLError, OSError) as e:
        print(f"         webhook failed: {e}", file=sys.stderr)
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────


def print_trace_line(receipt_, body, out=None):
    """One line per call: endpoint, params, status, ms, what came back. The trace IS the proof."""
    out = out or sys.stdout
    p = receipt_["params"]
    what = " ".join(
        f"{k}={short(v, 6) if len(str(v)) > 24 else v}" for k, v in p.items() if k != "limit"
    )
    detail = ""
    if isinstance(body, dict) and "data" in body:
        data = body["data"]
        if isinstance(data, dict) and "lcs" in data:
            detail = f"{len(data['lcs'])} rows"
        elif isinstance(data, dict) and "tks" in data:
            detail = f"{len(data['tks'])} tokens"
        elif isinstance(data, list):
            detail = f"{len(data)} items"
    chip = str(receipt_["status"]) if receipt_["status"] else "ERR"
    print(
        f"    GET {receipt_['endpoint']:<32} {what:<58} {chip:>3} {receipt_['ms']:>5} ms  {detail}",
        file=out,
    )


def print_verdict(v, out=None):
    out = out or sys.stdout
    r = v.removal
    print(file=out)
    print(
        f"  ● LP REMOVED  −${usd(r):,.0f}   {venue(r)} · {pair(r)}   {fmt_utc(ts_ms(r))}",
        file=out,
    )
    share = (
        f"{v.removal_share_of_pool * 100:.1f}% of the pool walked out"
        if v.removal_share_of_pool is not None
        else "share of pool unknown"
    )
    print(f"    maker {r.get('m')} · {share} · txn {short(r.get('txn'), 6)}", file=out)
    print(file=out)
    print(f"  ◆ {v.kind} · severity {v.severity}", file=out)
    print(
        f"    {v.recovered_share * 100:.1f}% recovered — "
        f"${v.recovered_usd:,.0f} of ${v.removed_usd:,.0f}",
        file=out,
    )
    d = v.destination
    if d and v.kind in ("MIGRATION", "CONSOLIDATION", "PARTIAL"):
        liq = d.get("liquidity_now_usd")
        now = f" · pool now holds ${liq:,.0f}" if liq else ""
        when = fmt_elapsed(v.elapsed_s) + (" later" if (v.elapsed_s or 0) >= 0 else " earlier")
        print(
            f"    +${d['added_usd']:,.0f} into {d['venue']} · {d['pair']} ({d['platform']})"
            f"   {when}{now}",
            file=out,
        )
    if v.kind == "REBALANCE" and v.source_pool:
        liq = v.source_pool.get("liquidity_now_usd")
        print(
            f"    back into the same pool {fmt_elapsed(v.elapsed_s)} later"
            + (f" · pool now holds ${liq:,.0f}" if liq else ""),
            file=out,
        )
    if v.kind == "EXIT":
        chains = ", ".join(f["platform"] for f in v.follows)
        print(
            f"    nothing re-added by {short(r.get('m'))} within ±{v.window_h['back']:.0f} h "
            f"on {chains}",
            file=out,
        )
    if v.kind == "INCOMPLETE":
        bad = [f["platform"] for f in v.follows if not f["complete"]]
        print(
            f"    follow did not complete on: {', '.join(bad)} — "
            "a throttle is never an Exit; re-run",
            file=out,
        )
    adds = [a for a in v.evidence if a["row"].get("tp") == "add"]
    if adds:
        print(file=out)
        print("  the rows (verbatim fields)", file=out)
        print(
            f"    remove  ts={r.get('ts')} tu={r.get('tu')} m={r.get('m')} en={r.get('en')} "
            f"a0={r.get('a0')} txn={short(r.get('txn'), 6)}",
            file=out,
        )
        for a in adds[:4]:
            x = a["row"]
            print(
                f"    add     ts={x.get('ts')} tu={x.get('tu')} m={x.get('m')} en={x.get('en')} "
                f"a0={x.get('a0')} txn={short(x.get('txn'), 6)}  [{a['platform']}]",
                file=out,
            )
        if len(adds) > 4:
            print(f"    … {len(adds) - 4} more add(s) in the window", file=out)
        print(
            f"    {v.recovered_usd:,.2f} ÷ {v.removed_usd:,.2f} = {v.recovered_share:.4f}", file=out
        )
    if v.refused:
        print(file=out)
        print("  refused", file=out)
        for x in v.refused:
            print(f"    · {x}", file=out)
    if v.notes:
        print(file=out)
        print("  notes", file=out)
        for x in v.notes:
            print(f"    · {x}", file=out)


def throttle_advice(err):
    var = api_key_var()
    waits = " + ".join(f"{BACKOFF_S * 2**i} s" for i in range(RETRIES))
    hatch = (
        f"unset {var} to use the keyless surface instead"
        if var
        else f"export a free key from {KEY_URL} as CMC_API_KEY "
        "(an escape hatch, never a requirement)"
    )
    return (
        f"\nthe trigger could not be fetched — CoinMarketCap throttled the "
        f"{'keyed' if var else 'anonymous'} tier.\n\n  what happened   {err}\n"
        f"                  after {waits} of backoff.\n"
        f"  what to do      wait a minute and re-run, or {hatch}.\n"
    )


def parse_watchlist(path):
    if not path:
        return WATCHLIST
    with open(path) as fh:
        data = json.load(fh)
    return [(t["platform"], t["address"], t.get("symbol", short(t["address"]))) for t in data]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forwarding", description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--json", metavar="PATH", help="write the full receipt (verdict, rows, calls, responses)"
    )
    ap.add_argument("--quiet", action="store_true", help="no per-call trace")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--platform", default="ethereum")
        p.add_argument("--address", required=True)
        p.add_argument("--min-usd", type=float, default=MIN_USD)

    pi = sub.add_parser("investigate", help="the product: newest big removal → verdict")
    common(pi)
    pi.add_argument("--txn", help="a specific removal (transaction hash)")
    pi.add_argument("--maker", help="the removal's wallet — finds --txn by wallet, no page walk")
    pi.add_argument("--min-share", type=float, default=MIN_SHARE)
    pi.add_argument("--no-cross-chain", action="store_true", help="same-chain follow only")
    pi.add_argument(
        "--newest", action="store_true", help="the newest qualifying removal, not the largest"
    )
    pi.add_argument("--hours", type=float, default=W_BACK_H, help="window each side of the removal")
    pr = sub.add_parser("removals", help="non-JIT removals >= --min-usd, largest first")
    common(pr)
    pr.add_argument("--pages", type=int, default=TRIGGER_PAGES)
    pf = sub.add_parser("follow", help="one wallet's liquidity events on one token")
    common(pf)
    pf.add_argument("--maker", required=True)
    pf.add_argument("--hours", type=float, default=None, help="window around the newest removal")
    pw = sub.add_parser("watch", help="the autonomous loop")
    pw.add_argument(
        "--watchlist", help="JSON: [{platform, address, symbol}]; default: the built-in 11"
    )
    pw.add_argument("--min-usd", type=float, default=MIN_USD)
    pw.add_argument("--min-share", type=float, default=MIN_SHARE)
    pw.add_argument("--interval", type=int, default=60)
    pw.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="stop after N cycles (1 = one pass, fire on the newest)",
    )
    pw.add_argument("--webhook", help="POST every verdict here as JSON")
    pw.add_argument("--no-cross-chain", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2

    started = time.time()
    var = api_key_var()
    mode = f"keyed via ${var} (escape hatch — the default is keyless)" if var else "keyless"
    trace = None if a.quiet else print_trace_line
    c = Client(keep_bodies=bool(a.json), on_call=trace)

    if a.cmd == "investigate":
        print(f"forwarding address — {mode} · {a.platform} · {a.address}\n")
        print("  following the wallet")
        try:
            v = investigate(
                a.platform,
                a.address,
                txn=a.txn,
                maker=a.maker,
                min_usd=a.min_usd,
                min_share=a.min_share,
                cross_chain=not a.no_cross_chain,
                back_h=a.hours,
                fwd_h=a.hours,
                client=c,
                newest=a.newest,
            )
        except NoCandidate as e:
            print(f"\n  no verdict — {e}")
            return 3
        except Throttled as e:
            print(throttle_advice(e), file=sys.stderr)
            return 75
        print_verdict(v)
        elapsed = time.time() - started
        ok = sum(1 for x in c.calls if x["status"] == 200)
        print(
            f"\n  {len(c.calls)} calls · {ok} × 200 · {c.credits} credits · "
            f"{elapsed:.1f} s · {mode}"
        )
        if a.json:
            rule = (
                "the removal named by --txn"
                if a.txn
                else (
                    f"{'newest' if a.newest else 'largest'} non-JIT removal ≥ ${a.min_usd:,.0f} "
                    f"and ≥ {a.min_share * 100:.0f}% of its pool, last {TRIGGER_PAGES * PAGE} rows"
                )
            )
            with open(a.json, "w") as fh:
                json.dump(
                    receipt(v, c, started=started, selection_rule=rule),
                    fh,
                    indent=1,
                    sort_keys=True,
                )
            print(f"  wrote {a.json}")
        return 0

    if a.cmd == "removals":
        print(f"removals — {mode} · {a.platform} · {a.address} · ≥ ${a.min_usd:,.0f}\n")
        removals, meta, jit_rows = largest_removals(
            c, a.platform, a.address, min_usd=a.min_usd, pages=a.pages
        )
        if meta["error"] and not meta["rows"]:
            if meta["throttled"]:
                print(throttle_advice(meta["error"]), file=sys.stderr)
                return 75
            print(f"  error — {meta['error']}")
            return 1
        pools, _ = pools_of(c, a.platform, a.address)
        print(
            f"\n  {meta['rows']} rows · {meta['jit_txns']} JIT transactions discarded · "
            f"{len(removals)} removals\n"
        )
        for r in removals:
            p = pools.get(pool_id(r))
            s = (
                f"{usd(r) / (usd(r) + p['liqUsd']) * 100:5.1f}% of pool"
                if p
                else "  pool not in top 20"
            )
            print(
                f"  {fmt_utc(ts_ms(r))}  −${usd(r):>12,.0f}  {venue(r):<28} {pair(r):<12} {s}  "
                f"m={short(r.get('m'))}  txn={short(r.get('txn'), 6)}"
            )
        if not removals:
            print("  none")
            return 3
        return 0

    if a.cmd == "follow":
        print(f"follow — {mode} · {a.platform} · {a.address} · maker {a.maker}\n")
        rows, meta = walk(
            c, {"platform": a.platform, "address": a.address, "maker": a.maker}, FOLLOW_PAGES
        )
        if meta["error"] and not rows:
            if meta["throttled"]:
                print(throttle_advice(meta["error"]), file=sys.stderr)
                return 75
            print(f"  error — {meta['error']}")
            return 1
        jit = jit_txns(rows)
        print(f"\n  {len(rows)} events by this wallet · {len(jit)} JIT transactions marked\n")
        for r in sorted(rows, key=ts_ms):
            tag = "JIT" if r.get("txn") in jit else "   "
            print(
                f"  {fmt_utc(ts_ms(r))}  {r.get('tp'):<6} "
                f"{'+' if r.get('tp') == 'add' else '−'}${usd(r):>12,.0f}  "
                f"{venue(r):<28} {pair(r):<12} {tag} txn={short(r.get('txn'), 6)}"
            )
        return 0

    if a.cmd == "watch":
        wl = parse_watchlist(a.watchlist)
        print(f"watch — {mode} · {len(wl)} tokens · every {a.interval} s · ≥ ${a.min_usd:,.0f}\n")
        fired = watch(
            wl,
            min_usd=a.min_usd,
            min_share=a.min_share,
            interval_s=a.interval,
            cycles=a.cycles,
            webhook=a.webhook,
            cross_chain=not a.no_cross_chain,
            client=c,
        )
        print(f"\n  {len(fired)} verdict(s) · {len(c.calls)} calls · {c.credits} credits · {mode}")
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(
                    {
                        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                        "verdicts": [asdict(v) for v in fired],
                        "calls": c.calls,
                        "responses": c.bodies,
                    },
                    fh,
                    indent=1,
                    sort_keys=True,
                )
            print(f"  wrote {a.json}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
