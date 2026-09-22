"""GET /api/lookup?platform=ethereum&address=0x… — the same investigation, behind a CORS header.

CoinMarketCap sends no Access-Control-Allow-Origin, so the landing page's live box cannot call
it directly; this function runs forwarding.investigate() on the page's behalf. It holds no
secret — every endpoint is on the keyless /public-api surface — and it follows the wallet on
the SAME CHAIN ONLY, to keep one visitor to ~6 calls on a per-IP anonymous tier that every
visitor shares. The CLI is the cross-chain surface, and every reader has their own quota there.

One adjudicator: this imports scripts/forwarding.py rather than re-implementing the rule.
"""

import json
import os
import re
import sys
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import forwarding  # noqa: E402

CACHE: dict[tuple[str, str], tuple[float, dict]] = {}  # per function instance, 60 s
CACHE_S = 60
CACHE_MAX = 256  # a warm instance must not grow with every distinct address ever asked
SPACING_S = 0.4  # faster than the CLI's 2 s: one visitor, six calls, one shared IP
# The CLI retries 3× (15 + 30 + 60 s of backoff) because it has no wall clock. This function has
# one: vercel.json maxDuration is 60 s, so it retries once (15 s) and then answers "throttled"
# as JSON — the platform must never be the one to end the request.
PROXY_RETRIES = 1
BUDGET_S = 45
EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
SLUG = re.compile(r"^[a-z0-9-]{2,32}$")


def auth_position():
    """What the client will actually send — read from the environment at call time, like the
    client does, so the payload can never claim keyless while a key is billing. Names the
    variable, never the value."""
    var = forwarding.escape_hatch_var()
    if var is None:
        return "none — keyless /public-api, no key exists in this deployment"
    return f"keyed — {var} is set in this deployment (the CLI's escape hatch); credits bill"


def lookup(platform, address):
    key = (platform, address.lower())
    hit = CACHE.get(key)
    if hit and hit[0] > time.time():
        payload = dict(hit[1])
        payload["cached"] = True
        return payload
    started = time.time()
    c = forwarding.Client(spacing=SPACING_S, retries=PROXY_RETRIES)
    try:
        v = forwarding.investigate(platform, address, cross_chain=False, client=c, trigger_pages=1)
        payload = {
            "verdict": asdict(v),
            "headline": v.headline(),
            "scope": (
                "same-chain follow over the newest 100 rows — run the CLI for 300 rows "
                "and the cross-chain version"
            ),
        }
    except forwarding.NoCandidate as e:
        payload = {"refused": str(e)}
    except forwarding.Throttled as e:
        payload = {"throttled": str(e), "retry_s": 60}
    payload.update(
        {
            "calls": c.calls,
            "credits_used": c.credits,
            "wall_clock_s": round(time.time() - started, 1),
            "budget_s": BUDGET_S,
            "auth": auth_position(),
            "cached": False,
        }
    )
    if "throttled" not in payload:
        if len(CACHE) >= CACHE_MAX:
            CACHE.pop(next(iter(CACHE)))
        CACHE[key] = (time.time() + CACHE_S, payload)
    return payload


class handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        platform = (q.get("platform") or ["ethereum"])[0].strip().lower()
        address = (q.get("address") or [""])[0].strip()
        if not SLUG.match(platform):
            return self._send(400, {"error": "platform must be a chain slug such as ethereum"})
        if not EVM.match(address):
            return self._send(400, {"error": "address must be a 0x… EVM contract address"})
        payload = lookup(platform, address)
        return self._send(200, payload)

    def log_message(self, fmt, *args):  # keep the function log to one line per request
        sys.stderr.write("lookup " + (fmt % args) + "\n")
