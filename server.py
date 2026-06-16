#!/usr/bin/env python3
"""
inject.today personal dashboard — local server.

Serves the static dashboard (index.html) and proxies requests to the
inject.today API. The proxy is required because the public API sits behind
Cloudflare + has no CORS headers, so a browser cannot call it directly.
Requests are made server-side with a browser User-Agent and cached briefly.

Usage:
    python server.py            # serve on http://localhost:8723
    python server.py 9000       # serve on a custom port

No third-party dependencies — Python 3.8+ standard library only.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_BASE = "https://inject.today/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CACHE_TTL = 60  # seconds

# Whitelist of API paths the proxy is allowed to reach (prefix match for the
# parameterised review/update routes).
ALLOWED = (
    "versions/current",
    "versions/future",
    "versions/previous",
    "cheats",
    "cheats/reviews",
    "cheats/updates",
)

HERE = Path(__file__).resolve().parent
_cache = {}  # path -> (expires_at, status, body_bytes)


def _allowed(path: str) -> bool:
    if path in ALLOWED:
        return True
    # parameterised: cheats/reviews/<id>, cheats/updates/<id>
    return path.startswith("cheats/reviews/") or path.startswith("cheats/updates/")


def fetch_api(path: str):
    """Fetch path from the upstream API, with a small TTL cache."""
    now = time.time()
    hit = _cache.get(path)
    if hit and hit[0] > now:
        return hit[1], hit[2]

    url = f"{API_BASE}/{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        body = json.dumps({"error": e.code, "message": e.reason}).encode()
        status = e.code
    except Exception as e:  # network/timeout
        body = json.dumps({"error": 502, "message": str(e)}).encode()
        status = 502

    _cache[path] = (now + CACHE_TTL, status, body)
    return status, body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/" or path == "/index.html":
            f = HERE / "index.html"
            if f.exists():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"index.html not found", "text/plain")
            return

        if path.startswith("/proxy/"):
            api_path = path[len("/proxy/"):].strip("/")
            if not _allowed(api_path):
                self._send(403, json.dumps(
                    {"error": 403, "message": "path not allowed"}).encode(),
                    "application/json")
                return
            status, body = fetch_api(api_path)
            self._send(status, body, "application/json")
            return

        self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8723
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"ERROR: could not start on port {port}: {e}")
        print(f"Port {port} is probably already in use by an old server.")
        print(f"Try another port:  python server.py {port + 1}")
        print(f"...then open http://localhost:{port + 1}/")
        sys.exit(1)
    url = f"http://localhost:{port}/"
    print(f"inject.today dashboard running at {url}")
    print("Open that URL in your browser. Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        srv.shutdown()


if __name__ == "__main__":
    main()
