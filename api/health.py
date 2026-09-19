"""GET /api/health — proves the deployment is alive and says how old the receipts are."""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def status():
    out = {
        "ok": True,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "keyless": True,
        "python": sys.version.split()[0],
    }
    try:
        import forwarding

        out["engine"] = "scripts/forwarding.py"
        out["rule"] = {
            "min_usd": forwarding.MIN_USD,
            "min_share": forwarding.MIN_SHARE,
            "full": forwarding.FULL,
            "partial_min": forwarding.PARTIAL_MIN,
            "window_h": [forwarding.W_BACK_H, forwarding.W_FWD_H],
        }
        out["key_exported"] = forwarding.api_key_var() is not None
    except Exception as e:  # the health check must answer even if the import breaks
        out["ok"] = False
        out["engine_error"] = f"{type(e).__name__}: {e}"
    for name in ("hero", "base_rate", "tests"):
        try:
            with open(os.path.join(ROOT, "docs", "proof", f"{name}.json")) as fh:
                d = json.load(fh)
            out[name] = {k: d[k] for k in ("captured_utc", "n", "offline", "live") if k in d}
        except (OSError, ValueError):
            out[name] = None
    return out


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):  # the CORS preflight, for a page served from another host (GitHub Pages)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        body = json.dumps(status()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
