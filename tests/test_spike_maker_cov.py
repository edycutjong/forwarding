"""The day-1 spike: every call recorded verbatim, retries on the anonymous tier, verdict on disk."""

import io
import json
import runpy
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import spike_maker
from conftest import UNI, envelope, row

SCRIPT = Path(spike_maker.__file__)


class Resp:
    def __init__(self, body, status=200):
        self._raw = json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, text="nope"):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(text.encode()))


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(spike_maker.time, "sleep", lambda s: None)


@pytest.fixture
def fresh_calls(monkeypatch):
    monkeypatch.setattr(spike_maker, "CALLS", [])
    return spike_maker.CALLS


@pytest.fixture
def out_file(monkeypatch, tmp_path):
    path = tmp_path / "docs" / "proof" / "spike_maker.json"
    monkeypatch.setattr(spike_maker, "OUT", path)
    return path


@pytest.fixture
def routed(monkeypatch, fresh_calls):
    """Replace the module's `get` with a router keyed on (path, platform, has maker)."""

    def install(responses):
        seen = []

        def fake_get(path, **params):
            seen.append((path, params))
            key = (path, params.get("platform"), "maker" in params)
            st, body = responses[key]
            fresh_calls.append({"endpoint": path, "params": params, "status": st})
            return st, body

        monkeypatch.setattr(spike_maker, "get", fake_get)
        return seen

    return install


def search_envelope(tks):
    return {"data": {"tks": tks}, "status": {"credit_count": 1}}


def full_scenario(page_rows, maker_rows, tks, slug_status=200):
    responses = {
        ("/v1/dex/liquidity-change/list", "ethereum", False): (200, envelope(page_rows)),
        ("/v1/dex/liquidity-change/list", "ethereum", True): (200, envelope(maker_rows)),
        ("/v1/dex/search", None, False): (200, search_envelope(tks)),
    }
    for slug, _ in spike_maker.UNI_ELSEWHERE:
        body = envelope([row("add", 1.0)]) if slug_status == 200 else {"_err": "no"}
        responses[("/v1/dex/liquidity-change/list", slug, False)] = (slug_status, body)
    return responses


def test_a_successful_get_records_status_hash_and_credit_count(monkeypatch, fresh_calls):
    seen = {}

    def fake_open(req, timeout=0):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp(envelope([]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    st, body = spike_maker.get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI)
    assert st == 200
    assert body["data"]["lcs"] == []
    assert seen["url"].startswith(spike_maker.BASE + "/v1/dex/liquidity-change/list?")
    assert "address=" + UNI in seen["url"]
    assert not any(k.lower() == "x-cmc_pro_api_key" for k in seen["headers"])
    call = fresh_calls[0]
    assert call["status"] == 200 and call["credit_count"] == 1
    assert len(call["sha256"]) == 64 and call["utc"].endswith("Z")


def test_a_body_without_a_status_object_records_no_credit_count(monkeypatch, fresh_calls):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp({"data": {}}))
    spike_maker.get("/v1/dex/search", q=UNI)
    assert fresh_calls[0]["credit_count"] is None


def test_a_429_then_a_500_are_retried_with_doubling_backoff(monkeypatch, fresh_calls):
    waits = []
    answers = iter([http_error(429), http_error(500), Resp(envelope([]))])

    def fake_open(req, timeout=0):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(spike_maker.time, "sleep", waits.append)
    st, body = spike_maker.get("/v1/dex/liquidity-change/list", platform="ethereum", address=UNI)
    assert st == 200 and "_err" not in body
    assert waits == [15, 30]
    assert len(fresh_calls) == 1


def test_four_throttles_in_a_row_give_up_with_status_zero(monkeypatch, fresh_calls, no_sleep):
    def fake_open(req, timeout=0):
        raise http_error(429)

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    st, body = spike_maker.get("/v1/dex/search", q=UNI)
    assert (st, body) == (0, {"_err": "gave up"})
    assert fresh_calls == []


def test_a_client_error_is_recorded_once_and_not_retried(monkeypatch, fresh_calls, no_sleep):
    n = {"calls": 0}

    def fake_open(req, timeout=0):
        n["calls"] += 1
        raise http_error(404, "x" * 500)

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    st, body = spike_maker.get("/v1/dex/search", q=UNI)
    assert st == 404
    assert body == {"_err": "x" * 300}
    assert n["calls"] == 1
    assert fresh_calls == [
        {"endpoint": "/v1/dex/search", "params": {"q": UNI}, "status": 404, "body": "x" * 300}
    ]


def test_main_answers_all_four_questions_and_writes_the_verdict(routed, out_file, no_sleep, capsys):
    page = [
        row("remove", -100.0, m="0x" + "a" * 40),
        row("remove", -2500.0, m="0x" + "b" * 40),
        row("add", 300.0, m="0x" + "c" * 40, en=None, f="0xfac"),
        row("add", 50.0, m=None, en="Uniswap v2 (Ethereum)", eid=1069),
    ]
    maker_rows = [row("remove", -2500.0, m="0x" + "b" * 40), row("add", 10.0, m="0x" + "b" * 40)]
    tks = [{"addr": UNI.upper(), "cid": 7083}, {"addr": "0xother", "cid": 1}]
    seen = routed(full_scenario(page, maker_rows, tks))
    spike_maker.main()
    out = capsys.readouterr().out
    saved = json.loads(out_file.read_text())

    q1 = saved["q1_maker_is_eoa"]
    assert q1["rows"] == 4 and q1["distinct_makers"] == 4
    assert q1["uniswap_v3_rows"] == 2 and q1["uniswap_v3_distinct_makers"] == 2
    assert q1["position_manager_appears_as_maker"] is False
    assert q1["makers_per_venue"] == {
        "Uniswap v3 (Ethereum)": 2,
        "factory 0xfac": 1,
        "Uniswap v2 (Ethereum)": 1,
    }
    q2 = saved["q2_maker_filter"]
    assert q2["maker"] == "0x" + "b" * 40 and q2["rows"] == 2
    assert q2["all_rows_carry_that_maker"] is True
    assert seen[1][1]["maker"] == "0x" + "b" * 40
    assert saved["q3_platform_slugs"]["bsc"] == {
        "status": 200,
        "rows": 1,
        "address": spike_maker.UNI_ELSEWHERE[0][1],
    }
    q4 = saved["q4_search_by_address"]
    assert q4["exact_address_hit"] is True and q4["cid"] == 7083 and q4["rows"] == 2
    assert saved["verdict"] == {
        "maker_is_eoa": True,
        "maker_filter_works_keyless": True,
        "cross_chain_slugs_ok": ["bsc", "arbitrum", "polygon", "unichain"],
        "search_resolves_address": True,
        "all_makers_look_like_evm_addresses": False,
    }
    assert saved["key_sent"] is False
    assert len(saved["calls"]) == 7
    assert "4 rows · 4 distinct makers · Uniswap v3: 2 rows from 2 makers" in out
    assert "cid 7083" in out
    assert "wrote docs/proof/spike_maker.json · 7 calls · no key sent" in out


def test_without_removes_the_first_row_is_the_target_and_failed_slugs_carry_no_rows(
    routed, out_file, no_sleep, capsys
):
    npm = spike_maker.NPM.upper()
    page = [row("add", 10.0, m=npm), row("add", 20.0, m=npm)]
    seen = routed(full_scenario(page, [], [{"addr": "0xother", "cid": 1}], slug_status=429))
    spike_maker.main()
    out = capsys.readouterr().out
    saved = json.loads(out_file.read_text())

    assert seen[1][1]["maker"] == npm
    assert saved["q1_maker_is_eoa"]["position_manager_appears_as_maker"] is True
    assert saved["q1_maker_is_eoa"]["uniswap_v3_distinct_makers"] == 1
    assert saved["q2_maker_filter"]["rows"] == 0
    assert saved["q2_maker_filter"]["all_rows_carry_that_maker"] is True
    assert all(
        v["rows"] is None and v["status"] == 429 for v in saved["q3_platform_slugs"].values()
    )
    assert saved["q4_search_by_address"] == {
        "status": 200,
        "rows": 1,
        "exact_address_hit": False,
        "cid": None,
        "response": search_envelope([{"addr": "0xother", "cid": 1}]),
    }
    assert saved["verdict"]["maker_is_eoa"] is False
    assert saved["verdict"]["maker_filter_works_keyless"] is False
    assert saved["verdict"]["cross_chain_slugs_ok"] == []
    assert saved["verdict"]["search_resolves_address"] is False
    assert saved["verdict"]["all_makers_look_like_evm_addresses"] is False
    assert "bsc       status 429 rows None" in out
    assert "exact hit: False, cid None" in out


def test_a_null_data_envelope_is_treated_as_an_empty_page(routed, out_file, no_sleep):
    responses = full_scenario([row("remove", -1.0)], [], [])
    responses[("/v1/dex/liquidity-change/list", "ethereum", True)] = (200, {"data": None})
    responses[("/v1/dex/search", None, False)] = (200, {"data": None})
    routed(responses)
    spike_maker.main()
    saved = json.loads(out_file.read_text())
    assert saved["q2_maker_filter"]["rows"] == 0
    assert saved["q4_search_by_address"]["rows"] == 0
    assert saved["verdict"]["all_makers_look_like_evm_addresses"] is True


def test_running_the_script_as_main_runs_the_spike_end_to_end(monkeypatch, tmp_path, capsys):
    written = {}

    def fake_open(req, timeout=0):
        parsed = urllib.parse.urlparse(req.full_url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path.endswith("/v1/dex/search"):
            return Resp(search_envelope([{"addr": UNI, "cid": 7083}]))
        if "maker" in q:
            return Resp(envelope([row("remove", -5.0)]))
        if q["platform"] != "ethereum":
            raise http_error(404)
        return Resp(envelope([row("remove", -5.0), row("add", 5.0, m="0x" + "d" * 40)]))

    def fake_write(self, text):
        written[self.name] = text

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(Path, "write_text", fake_write)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", [str(SCRIPT)])
    ns = runpy.run_path(str(SCRIPT), run_name="__main__")
    saved = json.loads(written["spike_maker.json"])
    assert ns["OUT"].name == "spike_maker.json"
    assert saved["verdict"]["maker_is_eoa"] is True
    assert saved["verdict"]["cross_chain_slugs_ok"] == []
    assert saved["verdict"]["search_resolves_address"] is True
    assert len(saved["calls"]) == 7
    assert "no key sent" in capsys.readouterr().out
