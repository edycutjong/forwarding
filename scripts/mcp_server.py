#!/usr/bin/env python3
"""Forwarding Address as an MCP server — three tools over stdio, stdlib only.

    claude mcp add forwarding -- python3 /path/to/scripts/mcp_server.py

The judge's own model chooses which token and which removal to chase; this server decides
what is true. Every number in a tool result is arithmetic on CoinMarketCap rows (invariant I1),
and a just-in-time pair named as an event is refused, not adjudicated.

Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout — `initialize`, `tools/list`,
`tools/call`, `ping`; `notifications/initialized` is accepted and ignored; anything else is
-32601. That is the whole surface Claude Code and Claude Desktop need, and it is testable
with a pipe (tests/test_mcp.py). Logging goes to stderr; stdout is the wire.

Tools:
    where_did_liquidity_go   platform, address [, txn, maker, min_usd, cross_chain] → verdict
    largest_removals         platform, address [, min_usd, pages]                   → trigger list
    follow_maker             platform, address, maker                               → one wallet
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forwarding  # noqa: E402
from forwarding import (  # noqa: E402
    FOLLOW_PAGES,
    MIN_SHARE,
    MIN_USD,
    TRIGGER_PAGES,
    Client,
    NoCandidate,
    Throttled,
    fmt_utc,
    jit_txns,
    largest_removals,
    pair,
    pool_id,
    pools_of,
    short,
    ts_ms,
    usd,
    venue,
    walk,
)

PROTOCOL = "2024-11-05"
SERVER = {"name": "forwarding-address", "version": "1.0.0"}

TOOLS = [
    {
        "name": "where_did_liquidity_go",
        "description": (
            "Follow the wallet behind a large LP removal. Finds the largest non-JIT removal "
            "(>= min_usd and >= 10% of its pool) on the token — or the one named by txn — "
            "follows its maker across every pool of the token on this chain and every other "
            "EVM chain the asset trades on, and returns one of REBALANCE / MIGRATION / "
            "CONSOLIDATION / PARTIAL / EXIT / INCOMPLETE with the recovered share, the "
            "destination pool and the raw rows. Keyless. A transaction that both adds and "
            "removes (just-in-time liquidity) is refused as not an event."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "chain slug: ethereum, base, bsc, arbitrum, polygon, unichain …",
                },
                "address": {"type": "string", "description": "token contract address"},
                "txn": {
                    "type": "string",
                    "description": "a specific removal's transaction hash (optional)",
                },
                "maker": {
                    "type": "string",
                    "description": "that removal's wallet — finds txn by wallet, no page walk",
                },
                "min_usd": {"type": "number", "description": f"default {MIN_USD:,.0f}"},
                "cross_chain": {
                    "type": "boolean",
                    "description": "follow the wallet on the asset's other EVM chains (default on)",
                },
            },
            "required": ["platform", "address"],
        },
    },
    {
        "name": "largest_removals",
        "description": (
            "Non-JIT liquidity removals >= min_usd on a token, largest first, each with the "
            "share of its pool it represented, the maker wallet and the transaction hash. Use "
            "it to choose which removal to hand to where_did_liquidity_go."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "address": {"type": "string"},
                "min_usd": {"type": "number", "description": f"default {MIN_USD:,.0f}"},
                "pages": {
                    "type": "integer",
                    "description": f"pages of 100 rows, default {TRIGGER_PAGES}",
                },
            },
            "required": ["platform", "address"],
        },
    },
    {
        "name": "follow_maker",
        "description": (
            "Every liquidity add/remove one wallet made on a token, oldest first, with "
            "just-in-time transactions marked and the totals per pool identity. This is the "
            "raw join the verdict is computed from."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "address": {"type": "string"},
                "maker": {"type": "string", "description": "the wallet (the `m` field of a row)"},
            },
            "required": ["platform", "address", "maker"],
        },
    },
]


def log(msg):
    print(f"[forwarding-mcp] {msg}", file=sys.stderr, flush=True)


def _client():
    return Client(on_call=lambda r, body: log(f"GET {r['endpoint']} {r['status']} {r['ms']} ms"))


def _verdict_payload(v):
    d = asdict(v)
    d["headline"] = v.headline()
    return d


def tool_where_did_liquidity_go(args):
    c = _client()
    try:
        v = forwarding.investigate(
            args["platform"],
            args["address"],
            txn=args.get("txn"),
            maker=args.get("maker"),
            min_usd=float(args.get("min_usd", MIN_USD)),
            min_share=float(args.get("min_share", MIN_SHARE)),
            cross_chain=bool(args.get("cross_chain", True)),
            client=c,
        )
    except NoCandidate as e:
        return text_result(
            f"REFUSED — {e}",
            {"refused": str(e), "calls": c.calls},
        )
    except Throttled as e:
        return text_result(
            f"THROTTLED — CoinMarketCap's anonymous tier refused the trigger: {e}. "
            "Wait a minute and call again.",
            {"throttled": str(e), "calls": c.calls},
            is_error=True,
        )
    payload = _verdict_payload(v)
    payload["calls"] = c.calls
    payload["credits_used"] = c.credits
    return text_result(v.headline(), payload)


def tool_largest_removals(args):
    c = _client()
    removals, meta, _ = largest_removals(
        c,
        args["platform"],
        args["address"],
        min_usd=float(args.get("min_usd", MIN_USD)),
        pages=int(args.get("pages", TRIGGER_PAGES)),
    )
    if meta["error"] and not meta["rows"]:
        return text_result(f"ERROR — {meta['error']}", {"error": meta["error"]}, is_error=True)
    pools, _ = pools_of(c, args["platform"], args["address"])
    out = []
    for r in removals:
        p = pools.get(pool_id(r))
        share = usd(r) / (usd(r) + p["liqUsd"]) if p else None
        out.append(
            {
                "utc": fmt_utc(ts_ms(r)),
                "removed_usd": usd(r),
                "venue": venue(r),
                "pair": pair(r),
                "share_of_pool": share,
                "maker": r.get("m"),
                "txn": r.get("txn"),
            }
        )
    head = (
        f"{len(out)} non-JIT removal(s) ≥ ${float(args.get('min_usd', MIN_USD)):,.0f} in "
        f"{meta['rows']} rows ({meta['jit_txns']} JIT transactions discarded)"
    )
    return text_result(head, {"removals": out, "rows_scanned": meta["rows"], "calls": c.calls})


def tool_follow_maker(args):
    c = _client()
    rows, meta = walk(
        c,
        {"platform": args["platform"], "address": args["address"], "maker": args["maker"]},
        FOLLOW_PAGES,
    )
    if meta["error"] and not rows:
        return text_result(f"ERROR — {meta['error']}", {"error": meta["error"]}, is_error=True)
    jit = jit_txns(rows)
    rows.sort(key=ts_ms)
    per_pool = {}
    for r in rows:
        if r.get("txn") in jit:
            continue
        key = f"{venue(r)} · {pair(r)}"
        slot = per_pool.setdefault(key, {"added_usd": 0.0, "removed_usd": 0.0})
        slot["added_usd" if r.get("tp") == "add" else "removed_usd"] += usd(r)
    events = [
        {
            "utc": fmt_utc(ts_ms(r)),
            "tp": r.get("tp"),
            "usd": usd(r),
            "venue": venue(r),
            "pair": pair(r),
            "jit": r.get("txn") in jit,
            "txn": r.get("txn"),
            "row": r,
        }
        for r in rows
    ]
    head = (
        f"{len(rows)} event(s) by {short(args['maker'])} on this token, "
        f"{len(jit)} JIT transaction(s) marked, {len(per_pool)} pool identities"
    )
    return text_result(head, {"events": events, "per_pool": per_pool, "calls": c.calls})


HANDLERS = {
    "where_did_liquidity_go": tool_where_did_liquidity_go,
    "largest_removals": tool_largest_removals,
    "follow_maker": tool_follow_maker,
}


def text_result(headline, payload, is_error=False):
    return {
        "content": [
            {"type": "text", "text": headline},
            {"type": "text", "text": json.dumps(payload, indent=1, sort_keys=True, default=str)},
        ],
        "isError": is_error,
    }


def handle(msg):
    """One JSON-RPC message in, one out — or None for a notification."""
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return ok(
            mid, {"protocolVersion": proto, "capabilities": {"tools": {}}, "serverInfo": SERVER}
        )
    if method == "notifications/initialized" or (
        mid is None and str(method).startswith("notifications/")
    ):
        return None
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return err(mid, -32602, f"unknown tool: {name}")
        args = params.get("arguments") or {}
        missing = [k for k in _required(name) if k not in args]
        if missing:
            return ok(
                mid,
                text_result(
                    f"ERROR — missing argument(s): {', '.join(missing)}",
                    {"missing": missing},
                    is_error=True,
                ),
            )
        try:
            return ok(mid, fn(args))
        except Exception as e:  # a tool must answer, never kill the server
            log(f"tool {name} failed: {type(e).__name__}: {e}")
            return ok(
                mid,
                text_result(f"ERROR — {type(e).__name__}: {e}", {"error": str(e)}, is_error=True),
            )
    return err(mid, -32601, f"method not found: {method}")


def _required(name):
    for t in TOOLS:
        if t["name"] == name:
            return t["inputSchema"].get("required", [])
    return []


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(inp=None, out=None):
    inp = inp or sys.stdin
    out = out or sys.stdout
    log("ready — keyless, three tools")
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps(err(None, -32700, "parse error")) + "\n")
            out.flush()
            continue
        reply = handle(msg)
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


if __name__ == "__main__":
    serve()
