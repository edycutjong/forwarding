"""The MCP server: the protocol through a real pipe, and the three tools in-process."""

import json
import subprocess
import sys
from pathlib import Path

import forwarding
import mcp_server
from conftest import HERO_ADD, HERO_REMOVE, MAKER, THROTTLED, UNI, FakeClient, ep, lc, row, scenario
from mcp_server import handle

SERVER = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"


def rpc(method, params=None, mid=1):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


# ── The wire, for real: a subprocess and a pipe, no network ──────────────────────────────────


def test_initialize_and_tools_list_over_a_real_pipe():
    lines = [
        json.dumps(
            rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest"},
                },
            )
        ),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(rpc("ping", mid=2)),
        json.dumps(rpc("tools/list", mid=3)),
        json.dumps(rpc("nope/none", mid=4)),
        "this is not json",
    ]
    p = subprocess.run(
        [sys.executable, str(SERVER)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    assert out[0]["id"] == 1 and out[0]["result"]["serverInfo"]["name"] == "forwarding-address"
    assert out[0]["result"]["protocolVersion"] == "2024-11-05"
    assert out[0]["result"]["capabilities"] == {"tools": {}}
    assert out[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    names = [t["name"] for t in out[2]["result"]["tools"]]
    assert names == ["where_did_liquidity_go", "largest_removals", "follow_maker"]
    assert out[3]["error"]["code"] == -32601
    assert out[4]["error"]["code"] == -32700
    assert len(out) == 5  # the notification got no reply
    assert "ready — keyless" in p.stderr  # logging never touches stdout


def test_every_tool_declares_a_schema_with_required_arguments():
    for t in mcp_server.TOOLS:
        assert t["inputSchema"]["type"] == "object"
        assert "platform" in t["inputSchema"]["required"]
        assert "address" in t["inputSchema"]["required"]
        assert len(t["description"]) > 80


# ── The tools, in-process, through the routed fake client ────────────────────────────────────


def _install(monkeypatch, routes):
    monkeypatch.setattr(mcp_server, "Client", lambda **kw: FakeClient(routes))
    monkeypatch.setattr(forwarding, "Client", lambda **kw: FakeClient(routes))


def test_where_did_liquidity_go_returns_the_headline_then_the_verdict_json(monkeypatch):
    _install(monkeypatch, scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]))
    reply = handle(
        rpc(
            "tools/call",
            {
                "name": "where_did_liquidity_go",
                "arguments": {"platform": "ethereum", "address": UNI},
            },
        )
    )
    res = reply["result"]
    assert res["isError"] is False
    assert res["content"][0]["text"].startswith("MIGRATION · 99.9% recovered")
    payload = json.loads(res["content"][1]["text"])
    assert payload["kind"] == "MIGRATION"
    assert payload["removal"]["txn"] == HERO_REMOVE["txn"]
    assert payload["credits_used"] == 0
    assert payload["calls"][0]["endpoint"] == "/v1/dex/token"


def test_a_jit_pair_named_as_an_event_is_refused_not_adjudicated(monkeypatch):
    """The guaranteed refusal from the spec: a same-transaction add+remove is not an event."""
    jit_a, jit_r = row("add", 369_569.0, txn="0xjit"), row("remove", -369_569.0, txn="0xjit")
    _install(monkeypatch, scenario(trigger_rows=[jit_a, jit_r, HERO_REMOVE], maker_rows=[]))
    reply = handle(
        rpc(
            "tools/call",
            {
                "name": "where_did_liquidity_go",
                "arguments": {"platform": "ethereum", "address": UNI, "txn": "0xjit"},
            },
        )
    )
    res = reply["result"]
    assert res["isError"] is False  # a refusal is a result, not a failure
    assert res["content"][0]["text"].startswith("REFUSED — txn 0xjit is not an event")
    assert "adds and removes in the same transaction" in res["content"][0]["text"]


def test_a_throttled_trigger_is_an_error_result_with_advice(monkeypatch):
    _install(monkeypatch, [(ep("/v1/dex/token"), {"data": {}}), (lc(), THROTTLED)])
    reply = handle(
        rpc(
            "tools/call",
            {
                "name": "where_did_liquidity_go",
                "arguments": {"platform": "ethereum", "address": UNI},
            },
        )
    )
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].startswith("THROTTLED")


def test_largest_removals_lists_with_share_of_pool_and_the_wallet(monkeypatch):
    _install(monkeypatch, scenario(trigger_rows=[HERO_REMOVE], maker_rows=[]))
    reply = handle(
        rpc(
            "tools/call",
            {"name": "largest_removals", "arguments": {"platform": "ethereum", "address": UNI}},
        )
    )
    res = reply["result"]
    assert res["content"][0]["text"].startswith("1 non-JIT removal(s) ≥ $100,000")
    payload = json.loads(res["content"][1]["text"])
    r = payload["removals"][0]
    assert r["maker"] == MAKER and r["txn"] == HERO_REMOVE["txn"]
    assert 0.67 < r["share_of_pool"] < 0.69


def test_follow_maker_returns_every_event_with_jit_marked_and_per_pool_totals(monkeypatch):
    jit_a, jit_r = row("add", 5e5, txn="0xjit", ts=1), row("remove", -5e5, txn="0xjit", ts=1)
    _install(
        monkeypatch, scenario(trigger_rows=[], maker_rows=[HERO_ADD, HERO_REMOVE, jit_a, jit_r])
    )
    reply = handle(
        rpc(
            "tools/call",
            {
                "name": "follow_maker",
                "arguments": {"platform": "ethereum", "address": UNI, "maker": MAKER},
            },
        )
    )
    res = reply["result"]
    assert res["content"][0]["text"].startswith("4 event(s) by 0xc3da…5e56")
    payload = json.loads(res["content"][1]["text"])
    assert [e["jit"] for e in payload["events"]] == [True, True, False, False]
    assert payload["per_pool"]["Uniswap v4 (Ethereum) · UNI/USDC"]["added_usd"] == HERO_ADD["tu"]
    assert (
        payload["per_pool"]["Uniswap v3 (Ethereum) · UNI/USDC"]["removed_usd"] == -HERO_REMOVE["tu"]
    )


def test_a_missing_required_argument_is_an_error_result_not_a_crash():
    reply = handle(
        rpc("tools/call", {"name": "follow_maker", "arguments": {"platform": "ethereum"}})
    )
    assert reply["result"]["isError"] is True
    assert "missing argument(s): address, maker" in reply["result"]["content"][0]["text"]


def test_an_unknown_tool_is_invalid_params():
    reply = handle(rpc("tools/call", {"name": "sell_everything", "arguments": {}}))
    assert reply["error"]["code"] == -32602


def test_a_tool_that_raises_answers_with_an_error_result_and_the_server_survives(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(mcp_server.HANDLERS, "follow_maker", boom)
    reply = handle(
        rpc(
            "tools/call",
            {"name": "follow_maker", "arguments": {"platform": "e", "address": "a", "maker": "m"}},
        )
    )
    assert reply["result"]["isError"] is True and "kaboom" in reply["result"]["content"][0]["text"]


def test_serve_reads_lines_and_writes_one_reply_per_request():
    import io

    inp = io.StringIO(
        json.dumps(rpc("tools/list"))
        + "\n\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
    )
    out = io.StringIO()
    mcp_server.serve(inp, out)
    replies = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert len(replies) == 1 and replies[0]["id"] == 1


# ── The protocol in-process: what the pipe test above proves, the tracer can also see ─────────


def test_the_handshake_ping_unknown_method_and_a_bad_line_in_process():
    import io

    reply = handle(rpc("initialize", {"protocolVersion": "2025-03-26"}))
    assert reply["result"]["protocolVersion"] == "2025-03-26"
    assert reply["result"]["serverInfo"]["name"] == "forwarding-address"
    assert handle(rpc("initialize"))["result"]["protocolVersion"] == mcp_server.PROTOCOL
    assert handle(rpc("ping", mid=2)) == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert handle(rpc("nope/none", mid=4))["error"]["code"] == -32601
    assert mcp_server._required("not-a-tool") == []

    out = io.StringIO()
    mcp_server.serve(io.StringIO("this is not json\n"), out)
    assert json.loads(out.getvalue())["error"]["code"] == -32700


def test_largest_removals_and_follow_maker_answer_a_throttle_with_an_error_result(monkeypatch):
    _install(monkeypatch, [(ep("/v1/dex/token"), {"data": {}}), (lc(), THROTTLED)])
    for name, args in (
        ("largest_removals", {"platform": "ethereum", "address": UNI}),
        ("follow_maker", {"platform": "ethereum", "address": UNI, "maker": MAKER}),
    ):
        reply = handle(rpc("tools/call", {"name": name, "arguments": args}))
        assert reply["result"]["isError"] is True
        assert reply["result"]["content"][0]["text"].startswith("ERROR — HTTP 429")


def test_the_server_module_runs_as_a_program(monkeypatch, capsys):
    import io
    import runpy

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(rpc("ping")) + "\n"))
    runpy.run_path(mcp_server.__file__, run_name="__main__")
    assert json.loads(capsys.readouterr().out) == {"jsonrpc": "2.0", "id": 1, "result": {}}
