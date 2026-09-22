"""Permission boundaries — least privilege, proven rather than described.

Two boundaries exist in this product and both are tested against the outside world's shape:

* the agent surface (scripts/mcp_server.py) can read CoinMarketCap and nothing else — no tool
  takes a credential, a webhook or a URL, and no tool call can make the process send anything
  anywhere, however the arguments are padded;
* the hosted proxy (api/lookup.py) accepts exactly a chain slug and an EVM address, follows the
  wallet on that chain only, and carries no secret — so a visitor to the page can neither reach
  another host through it nor spend a credit that does not exist.

A third boundary — the judged path sends no key, and a keyed run announces itself so it can
never pass as keyless — is pinned in tests/test_fetch.py and tests/test_cli.py.
"""

import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import forwarding
import mcp_server
import pytest
from conftest import HERO_ADD, HERO_REMOVE, MAKER, UNI, FakeClient, scenario
from mcp_server import handle

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import health  # noqa: E402  — api/health.py, the Vercel function
import lookup  # noqa: E402  — api/lookup.py, the Vercel function

FORBIDDEN_ARGS = {"webhook", "url", "key", "api_key", "apikey", "token", "secret", "header"}


def rpc(method, params=None, mid=1):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open a real connection — GET or POST — fails the test."""

    def refuse(*a, **kw):
        raise AssertionError(f"network reached: {a[0].full_url if a else kw}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def test_no_tool_on_the_agent_surface_can_send_data_anywhere_or_be_handed_a_credential(
    monkeypatch, no_network
):
    """The model chooses where to look; nothing it says can make the server post, fetch a
    URL of its choosing, or attach a key. Every tool is driven with its arguments padded with
    the things an injected prompt would try, and the only traffic is CoinMarketCap GETs."""
    tools = handle(rpc("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "where_did_liquidity_go",
        "largest_removals",
        "follow_maker",
    ]
    for t in tools:
        props = set(t["inputSchema"]["properties"])
        assert not props & FORBIDDEN_ARGS, f"{t['name']} accepts {props & FORBIDDEN_ARGS}"
    assert "watch" not in mcp_server.HANDLERS  # the loop that can POST is CLI-only

    seen = []
    monkeypatch.setattr(
        mcp_server,
        "_client",
        lambda: FakeClient(
            scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE]),
            on_call=lambda r, body: seen.append(r),
        ),
    )
    padding = {
        "webhook": "https://attacker.example/collect",
        "url": "https://attacker.example",
        "api_key": "canary-value-that-must-never-be-echoed",
        "key": "x",
        "cross_chain": True,
    }
    calls = [
        ("where_did_liquidity_go", {"platform": "ethereum", "address": UNI, **padding}),
        ("largest_removals", {"platform": "ethereum", "address": UNI, "pages": 1, **padding}),
        ("follow_maker", {"platform": "ethereum", "address": UNI, "maker": MAKER, **padding}),
    ]
    for i, (name, args) in enumerate(calls):
        reply = handle(rpc("tools/call", {"name": name, "arguments": args}, mid=i))
        assert "error" not in reply and reply["result"]["isError"] is False, reply
        assert "attacker.example" not in json.dumps(reply)
        assert "canary-value" not in json.dumps(reply)
    assert seen, "the tools did make calls"
    assert all(
        r["endpoint"].startswith(("/v1/dex/", "/v2/cryptocurrency/", "/v4/dex/")) for r in seen
    )
    assert all("attacker" not in json.dumps(r["params"]) for r in seen)
    assert forwarding.api_key() is None  # nothing a tool received became a credential


def _sink(handler_cls, path):
    """The real handler with the socket taken away: do_GET runs as deployed, the status and
    headers are recorded, the body is captured. The base constructor is not chained on
    purpose — it would read a request off a socket."""

    class Sink(handler_cls):
        def __init__(self):  # noqa: D107
            self.path = path
            self.wfile = io.BytesIO()
            self.status = None
            self.headers_sent = {}

        def send_response(self, code):
            self.status = code

        def send_header(self, k, v):
            self.headers_sent[k] = v

        def end_headers(self):
            pass

    return Sink()


def _get(path, handler_cls=lookup.handler):
    h = _sink(handler_cls, path)
    h.do_GET()
    return h.status, h.headers_sent, json.loads(h.wfile.getvalue() or b"null")


@pytest.mark.parametrize(
    "query",
    [
        "address=../../etc/passwd",
        "address=0x1f98",
        "address=" + UNI + "&platform=ethereum;id",
        "address=" + UNI + "&platform=" + "a" * 40,
        "platform=ethereum",
        "address=http://attacker.example/" + UNI[2:],
    ],
)
def test_the_hosted_proxy_refuses_anything_but_a_chain_slug_and_an_evm_address(
    monkeypatch, no_network, query
):
    """Nothing a browser puts in the query string reaches CoinMarketCap unless it is exactly
    a lowercase slug and a 40-hex address — a 400 with the reason, and no call is made."""
    status, headers, body = _get("/api/lookup?" + query)
    assert status == 400, body
    assert "error" in body
    assert headers["Access-Control-Allow-Origin"] == "*"


def test_the_hosted_proxy_follows_the_same_chain_only_and_holds_no_secret(monkeypatch):
    """One visitor is a handful of calls on a shared IP, never a cross-chain fan-out; and the
    payload states the credential position (none) so a page cannot claim what it does not have."""
    lookup.CACHE.clear()
    seen = []
    routes = scenario(trigger_rows=[HERO_REMOVE], maker_rows=[HERO_ADD, HERO_REMOVE])
    client_kwargs = {}

    def make_client(**kw):
        client_kwargs.update(kw)
        return FakeClient(routes, on_call=lambda r, b: seen.append(r))

    monkeypatch.setattr(lookup.forwarding, "Client", make_client)
    status, _, body = _get(f"/api/lookup?platform=ethereum&address={UNI[:2] + UNI[2:].upper()}")
    assert status == 200
    # a2a r01: the function has a wall clock (vercel.json maxDuration 60 s) — it retries once,
    # not the CLI's 3× (105 s of backoff), and says what budget it answered under
    assert client_kwargs["retries"] == lookup.PROXY_RETRIES == 1
    assert body["budget_s"] == lookup.BUDGET_S < 60
    assert body["verdict"]["kind"] == "MIGRATION"
    chains = {r["params"].get("platform") or r["params"].get("network_slug") for r in seen}
    assert chains == {"ethereum"}  # quotes/latest names the chain network_slug; nothing else
    assert not any(r["endpoint"] == "/v1/dex/search" for r in seen)  # no sibling-chain resolution
    assert not any(r["endpoint"] == "/v2/cryptocurrency/info" for r in seen)
    assert body["credits_used"] == 0
    assert body["auth"].startswith("none")
    assert "same-chain" in body["scope"]
    assert body["cached"] is False
    # the second visitor inside 60 s is served from memory — the shared IP is not spent twice
    n = len(seen)
    status, _, again = _get(f"/api/lookup?platform=ethereum&address={UNI}")
    assert status == 200 and again["cached"] is True and len(seen) == n
    # a throttled answer is returned as a throttle with a retry hint, and is NOT cached: the next
    # visitor gets a fresh attempt, not a minute of someone else's 429
    lookup.CACHE.clear()

    def throttled(*a, **kw):
        raise forwarding.Throttled("HTTP 429 (error 1022): You've reached the limit")

    monkeypatch.setattr(lookup.forwarding, "investigate", throttled)
    status, _, body = _get(f"/api/lookup?platform=ethereum&address={UNI}")
    assert status == 200 and "1022" in body["throttled"] and body["retry_s"] == 60
    assert not lookup.CACHE
    # a refusal (nothing qualified) is an answer, with the reason
    monkeypatch.setattr(
        lookup.forwarding,
        "investigate",
        lambda *a, **kw: (_ for _ in ()).throw(
            forwarding.NoCandidate("no removal ≥ $100,000 in 100 rows")
        ),
    )
    status, _, body = _get(f"/api/lookup?platform=ethereum&address={UNI}")
    assert status == 200 and body["refused"].startswith("no removal")
    # a2a r01: `auth` is read from the environment at call time, like the client's key is — the
    # payload can never say "none" while a stray key in the deployment is billing; the value
    # names the variable, never the key
    for var in forwarding.KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert lookup.auth_position().startswith("none")
    monkeypatch.setenv("CMC_API_KEY", "0123456789abcdef0123456789abcdef")
    assert lookup.auth_position().startswith("keyed — CMC_API_KEY")
    assert "0123456789abcdef" not in lookup.auth_position()
    monkeypatch.delenv("CMC_API_KEY")
    # the per-instance cache is capped: the 257th distinct address evicts the oldest entry
    lookup.CACHE.clear()
    for i in range(lookup.CACHE_MAX):
        lookup.CACHE[("ethereum", f"0x{i:040x}")] = (time.time() + 60, {"i": i})
    monkeypatch.setattr(
        lookup.forwarding,
        "investigate",
        lambda *a, **kw: (_ for _ in ()).throw(forwarding.NoCandidate("none")),
    )
    _get(f"/api/lookup?platform=ethereum&address={UNI}")
    assert len(lookup.CACHE) == lookup.CACHE_MAX and ("ethereum", f"0x{0:040x}") not in lookup.CACHE
    lookup.CACHE.clear()
    # the only verbs are GET and the CORS preflight
    h = _sink(lookup.handler, "/api/lookup")
    h.do_OPTIONS()
    assert h.status == 204 and h.headers_sent["Access-Control-Allow-Methods"] == "GET, OPTIONS"


def test_the_health_check_reports_the_credential_position_and_never_the_credential(monkeypatch):
    """/api/health is how a reader checks the deployment holds no key. It must flip to true the
    moment one is exported — and print the variable's existence, never its value."""
    d = health.status()
    assert d["ok"] is True and d["keyless"] is True and d["key_exported"] is False
    assert d["engine"] == "scripts/forwarding.py" and d["rule"]["min_usd"] == forwarding.MIN_USD
    monkeypatch.setenv("CMC_API_KEY", "canary-value-that-must-never-be-echoed")
    d = health.status()
    assert d["key_exported"] is True
    assert "canary-value" not in json.dumps(d)
    status, headers, body = _get("/api/health", health.handler)
    assert status == 200 and body["key_exported"] is True
    assert headers["Cache-Control"] == "no-store"  # a health answer is never served stale
    assert headers["Access-Control-Allow-Origin"] == "*"
    # a local copy of site/ calls the functions cross-origin, so both must answer the preflight
    h = _sink(health.handler, "/api/health")
    h.do_OPTIONS()
    assert h.status == 204 and h.headers_sent["Access-Control-Allow-Origin"] == "*"
    assert h.headers_sent["Access-Control-Allow-Methods"] == "GET, OPTIONS"


def test_the_health_check_answers_even_when_the_engine_or_the_proofs_are_missing(
    monkeypatch, tmp_path, capsys
):
    """A health answer that crashes is no health answer: a broken import is reported as
    ok:false with the reason, and a missing receipt is reported as null, never as a 500."""
    monkeypatch.setattr(health, "ROOT", str(tmp_path))
    d = health.status()
    assert d["ok"] is True and d["hero"] is None and d["base_rate"] is None and d["tests"] is None
    monkeypatch.setitem(sys.modules, "forwarding", None)  # `import forwarding` now raises
    d = health.status()
    assert d["ok"] is False and d["engine_error"].startswith("ModuleNotFoundError")
    assert "engine" not in d
    # the proxy's request log is one line per request, on stderr
    lookup.handler.log_message(_sink(lookup.handler, "/api/lookup"), "%s %s", "GET", "/api/lookup")
    assert capsys.readouterr().err == "lookup GET /api/lookup\n"
