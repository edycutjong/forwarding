"""The fetch layer: keyless by default, backoff on the anonymous tier, cursor walk that cannot spin.

Each regression test is named for the defect it pins and the date it was seen on the live API.
"""

import io
import json
import urllib.error
import urllib.request

import pytest
from conftest import UNI, FakeClient, envelope, lc, row

import forwarding
from forwarding import Client, walk


class Resp:
    def __init__(self, body, status=200):
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://x", code, "err", {}, io.BytesIO(json.dumps(body).encode())
    )


THROTTLE_429 = {
    "status": {"error_code": 1022, "error_message": "You've reached the limit for anonymous access"}
}
BUSY_500 = {"status": {"error_code": 500, "error_message": "The system is busy"}}


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(forwarding.time, "sleep", lambda s: None)


def test_default_path_sends_no_key_and_uses_the_public_surface(monkeypatch, no_sleep):
    seen = {}

    def fake_open(req, timeout=0):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp({"data": {"lcs": []}, "status": {"credit_count": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    c = Client(spacing=0)
    body = c.get("/v1/dex/token", platform="ethereum", address=UNI)
    assert seen["url"].startswith(forwarding.BASE + "/v1/dex/token?")
    assert "/public-api/" in seen["url"]
    assert not any(k.lower() == "x-cmc_pro_api_key" for k in seen["headers"])
    assert "_err" not in body
    assert c.credits == 0  # the envelope says credit_count 1; keyless means nothing is billed
    assert c.calls[0]["credit_count"] == 1 and c.calls[0]["status"] == 200


@pytest.mark.parametrize("var", forwarding.KEY_VARS)
def test_an_exported_key_is_the_escape_hatch_and_announces_itself(monkeypatch, no_sleep, var):
    seen = {}

    def fake_open(req, timeout=0):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp({"data": {}, "status": {"credit_count": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setenv(var, "secret-key")
    c = Client(spacing=0)
    c.get("/v1/dex/token", platform="ethereum", address=UNI)
    assert seen["url"].startswith(forwarding.BASE_KEYED + "/v1/dex/token?")
    assert seen["headers"].get("X-cmc_pro_api_key") == "secret-key"
    assert forwarding.api_key_var() == var
    assert c.credits == 1
    assert "secret-key" not in json.dumps(c.calls)  # the receipt never carries the key


def test_a_blank_key_variable_does_not_switch_the_path(monkeypatch):
    monkeypatch.setenv("CMC_API_KEY", "   ")
    assert forwarding.api_key() is None
    assert forwarding.api_key_var() is None


def test_the_429_and_the_500_are_both_retried_and_the_row_then_lands(monkeypatch, no_sleep):
    """Live 2026-09-07 (elephant) and 2026-09-19: the anonymous tier says 'slow down' both ways."""
    answers = [
        http_error(429, THROTTLE_429),
        http_error(500, BUSY_500),
        Resp({"data": {"lcs": []}}),
    ]

    def fake_open(req, timeout=0):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    c = Client(spacing=0)
    body = c.get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI)
    assert "_err" not in body
    assert len(c.calls) == 1 and c.calls[0]["status"] == 200


def test_a_400_is_permanent_and_is_not_retried(monkeypatch, no_sleep):
    """token-liquidity/query, 2026-09-18: 400 'Parameter error' on every variant — retrying a
    400 only spends the reader's backoff on a call that can never succeed."""
    calls = []

    def fake_open(req, timeout=0):
        calls.append(1)
        raise http_error(400, {"status": {"error_code": 400, "error_message": "Parameter error"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    c = Client(spacing=0)
    body = c.get("/v1/dex/token-liquidity/query", platform="ethereum", address=UNI)
    assert body["_err"].startswith("HTTP 400 (error 400): Parameter error")
    assert body["_throttled"] is False
    assert len(calls) == 1
    assert c.calls[0]["status"] == 400 and c.calls[0]["error"] == body["_err"]


def test_an_exhausted_throttle_is_returned_as_throttled_with_cmc_own_message(monkeypatch, no_sleep):
    def fake_open(req, timeout=0):
        raise http_error(429, THROTTLE_429)

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    c = Client(spacing=0, retries=2)
    body = c.get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI)
    assert body["_throttled"] is True
    assert body["_err"] == "HTTP 429 (error 1022): You've reached the limit for anonymous access"


def test_a_dropped_connection_is_congestion_and_is_retried(monkeypatch, no_sleep):
    """Live 2026-09-08 (elephant, page 3 of 8): RemoteDisconnected mid-run ended with a traceback.
    It is how the anonymous tier behaves under load without a status code."""
    import http.client

    answers = [http.client.RemoteDisconnected("closed"), Resp({"data": {"lcs": []}})]

    def fake_open(req, timeout=0):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    body = Client(spacing=0).get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI)
    assert "_err" not in body


def test_an_unreachable_host_is_not_reported_as_a_rate_limit(monkeypatch, no_sleep):
    def fake_open(req, timeout=0):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    body = Client(spacing=0).get("/v1/dex/token", platform="ethereum", address=UNI)
    assert body["_throttled"] is False
    assert "URLError" in body["_err"]


def test_malformed_json_is_not_retried_because_retrying_cannot_fix_it(monkeypatch, no_sleep):
    calls = []

    def fake_open(req, timeout=0):
        calls.append(1)
        return Resp(b"<html>Human Verification</html>")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    body = Client(spacing=0).get("/v1/dex/token", platform="ethereum", address=UNI)
    assert body["_err"].startswith("malformed JSON")
    assert len(calls) == 1


def test_every_call_is_receipted_with_the_hash_of_the_exact_bytes(monkeypatch, no_sleep):
    import hashlib

    raw = json.dumps({"data": {"lcs": [row("add", 1.0)]}, "status": {"credit_count": 1}}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(raw))
    c = Client(spacing=0, keep_bodies=True)
    c.get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI, limit=100)
    r = c.calls[0]
    assert r["sha256"] == hashlib.sha256(raw).hexdigest()
    assert r["params"] == {"platform": "ethereum", "address": UNI, "limit": "100"}
    assert r["endpoint"] == "/v1/dex/liquidity-change/list"
    assert r["utc"].endswith("Z") and r["ms"] >= 0
    assert c.bodies[r["sha256"]] == json.loads(raw)


def test_describe_http_error_never_dumps_a_truncated_body():
    e = http_error(
        500, {"status": {"error_code": 500, "error_message": "The system is busy, " * 20}}
    )
    text = forwarding.describe_http_error(e)
    assert text.startswith("HTTP 500 (error 500): The system is busy")
    assert len(text) < 200
    e = urllib.error.HTTPError("https://x", 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))
    assert forwarding.describe_http_error(e) == "HTTP 502"


# ── walk(): the cursor ────────────────────────────────────────────────────────────────────────


def test_the_cursor_is_read_from_the_envelope_and_the_walk_stops_when_it_is_null():
    """2026-09-07 (elephant): reading the cursor off the last row never advanced the page."""
    p1 = [row("add", 1.0, ts=3000, lgid="1"), row("add", 1.0, ts=2000, lgid="2")]
    p2 = [row("add", 1.0, ts=1000, lgid="3")]
    c = FakeClient(
        [
            (lc(page=None), envelope(p1, last_id="CURSOR-1")),
            (lc(page="CURSOR-1"), envelope(p2, last_id=None)),
        ]
    )
    rows, meta = walk(c, {"platform": "ethereum", "address": UNI}, pages=5)
    assert [r["lgid"] for r in rows] == ["1", "2", "3"]
    assert meta["pages"] == 2 and meta["exhausted"] is True and meta["complete"] is True
    assert c.calls[1]["params"]["lastId"] == "CURSOR-1"


def test_a_page_that_adds_nothing_new_is_a_stall_and_the_walk_stops():
    same = [row("add", 1.0, ts=3000, lgid="1")]
    c = FakeClient([(lc(), envelope(same, last_id="AGAIN"))])
    rows, meta = walk(c, {"platform": "ethereum", "address": UNI}, pages=10)
    assert len(rows) == 1
    assert meta["stalled"] is True and meta["pages"] == 2


def test_rows_are_keyed_by_txn_and_log_index_together():
    """One txn emits several rows; log indexes repeat across txns. Neither alone is an identity."""
    a = row("add", 1.0, ts=3000, txn="0xaa", lgid="1")
    b = row("add", 1.0, ts=3000, txn="0xaa", lgid="2")
    c_ = row("add", 1.0, ts=3000, txn="0xbb", lgid="1")
    c = FakeClient([(lc(), envelope([a, b, c_, a], last_id=None))])
    rows, _ = walk(c, {"platform": "ethereum", "address": UNI}, pages=1)
    assert len(rows) == 3


def test_a_throttle_after_page_one_keeps_page_one_and_says_the_walk_is_incomplete():
    p1 = [row("add", 1.0, ts=3000, lgid="1")]
    c = FakeClient(
        [
            (lc(page=None), envelope(p1, last_id="C1")),
            (lc(page="C1"), forwarding_throttled()),
        ]
    )
    rows, meta = walk(c, {"platform": "ethereum", "address": UNI}, pages=3)
    assert len(rows) == 1
    assert meta["error"] and meta["throttled"] is True and meta["complete"] is False


def forwarding_throttled():
    return {"_err": "HTTP 429 (error 1022): limit", "_throttled": True, "_status": 429}


def test_until_stops_the_walk_once_a_page_reaches_past_the_window():
    p1 = [row("add", 1.0, ts=3000, lgid="1")]
    p2 = [row("add", 1.0, ts=100, lgid="2")]
    p3 = [row("add", 1.0, ts=50, lgid="3")]
    c = FakeClient(
        [
            (lc(page=None), envelope(p1, last_id="C1")),
            (lc(page="C1"), envelope(p2, last_id="C2")),
            (lc(page="C2"), envelope(p3, last_id="C3")),
        ]
    )
    rows, meta = walk(
        c,
        {"platform": "ethereum", "address": UNI},
        pages=5,
        until=lambda page: min(int(r["ts"]) for r in page) < 500,
    )
    assert len(rows) == 2 and meta["stopped"] is True and meta["pages"] == 2


def test_the_page_size_is_the_endpoint_cap_and_is_always_sent():
    c = FakeClient([(lc(), envelope([], last_id=None))])
    walk(c, {"platform": "ethereum", "address": UNI}, pages=1)
    assert c.calls[0]["params"]["limit"] == "100" and forwarding.PAGE == 100


def test_the_envelope_liquidity_and_pool_count_ride_along_in_meta():
    c = FakeClient([(lc(), envelope([], last_id=None, tlu=42063378.87, lpc="10"))])
    _, meta = walk(c, {"platform": "ethereum", "address": UNI}, pages=1)
    assert meta["tlu"] == 42063378.87 and meta["lpc"] == "10"
