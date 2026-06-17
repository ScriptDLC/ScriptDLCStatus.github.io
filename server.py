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

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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


# ---------------------------------------------------------------------------
# Roblox deployment downloader (a small, local reimplementation of rdd.latte.to)
#
# Given a version hash we fetch the package manifest from Roblox's CDN, download
# every package, lay them out into the correct folder structure and zip the lot
# into a single archive that the dashboard then serves to the browser. This is
# what lets the "Download" button work on-site instead of redirecting to RDD.
# ---------------------------------------------------------------------------
RBX_BASE = "https://setup.rbxcdn.com/"

# package name -> destination sub-folder inside the build (Windows player).
PLAYER_EXTRACT = {
    "RobloxApp.zip": "",
    "redist.zip": "",
    "shaders.zip": "shaders/",
    "ssl.zip": "ssl/",
    "WebView2.zip": "",
    "WebView2RuntimeInstaller.zip": "WebView2RuntimeInstaller/",
    "content-avatar.zip": "content/avatar/",
    "content-configs.zip": "content/configs/",
    "content-fonts.zip": "content/fonts/",
    "content-sky.zip": "content/sky/",
    "content-sounds.zip": "content/sounds/",
    "content-textures2.zip": "content/textures/",
    "content-models.zip": "content/models/",
    "content-textures3.zip": "PlatformContent/pc/textures/",
    "content-terrain.zip": "PlatformContent/pc/terrain/",
    "content-platform-fonts.zip": "PlatformContent/pc/fonts/",
    "content-platform-dictionaries.zip": "PlatformContent/pc/shared_compression_dictionaries/",
    "content-api-docs.zip": "content/api_docs/",
    "content-qt_translations.zip": "content/qt_translations/",
    "content-scripts.zip": "content/scripts/",
    "extracontent-luapackages.zip": "ExtraContent/LuaPackages/",
    "extracontent-translations.zip": "ExtraContent/translations/",
    "extracontent-models.zip": "ExtraContent/models/",
    "extracontent-textures.zip": "ExtraContent/textures/",
    "extracontent-places.zip": "ExtraContent/places/",
}

APP_SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Settings>\n"
    "\t<ContentFolder>content</ContentFolder>\n"
    "\t<BaseUrl>http://www.roblox.com</BaseUrl>\n"
    "</Settings>\n"
)

JOBS = {}  # token -> (zip_path, download_name); consumed once by /rdd/file


def _http_bytes(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _safe_extract(zf, dest):
    """Extract a zip into dest, guarding against path-traversal entries."""
    dest = os.path.abspath(dest)
    for m in zf.infolist():
        target = os.path.abspath(os.path.join(dest, m.filename))
        if target != dest and not target.startswith(dest + os.sep):
            continue  # skip anything that would escape dest
        if m.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(m) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def build_windows(version, log):
    """Assemble a full Windows player deployment and return (zip_path, name, size)."""
    log(f"Fetching manifest: {version}-rbxPkgManifest.txt", 1)
    text = _http_bytes(f"{RBX_BASE}{version}-rbxPkgManifest.txt", 60).decode("utf-8", "replace")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or lines[0] != "v0":
        raise RuntimeError("Manifest not found / bad format — version may not exist")

    pkgs = []
    for i in range(1, len(lines), 4):  # groups of 4: name, md5, packed, unpacked
        if i + 2 < len(lines):
            pkgs.append((lines[i], int(lines[i + 2])))
    if not pkgs:
        raise RuntimeError("Manifest contained no packages")
    total = sum(s for _, s in pkgs) or 1
    log(f"Manifest OK — {len(pkgs)} packages, ~{total // (1024*1024)} MB to fetch", 3)

    work = tempfile.mkdtemp(prefix="rdd_")
    root = os.path.join(work, "out")
    os.makedirs(root)
    done = 0
    try:
        for idx, (name, size) in enumerate(pkgs, 1):
            pct = 3 + int(done / total * 82)
            log(f"[{idx}/{len(pkgs)}] {name}  ({size // 1024} KB)", pct)
            data = _http_bytes(f"{RBX_BASE}{version}-{name}")
            done += size
            if name.lower().endswith(".zip"):
                sub = PLAYER_EXTRACT.get(name)
                if sub is None:
                    sub = ""
                    log(f"    note: unmapped package, extracting to root", None)
                dest = os.path.join(root, *[p for p in sub.split("/") if p]) if sub else root
                os.makedirs(dest, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    _safe_extract(zf, dest)
            else:
                with open(os.path.join(root, name), "wb") as f:
                    f.write(data)

        log("Writing AppSettings.xml", 87)
        with open(os.path.join(root, "AppSettings.xml"), "w", encoding="utf-8") as f:
            f.write(APP_SETTINGS)

        log("Compressing everything into a single .zip (this can take a minute)…", 90)
        fd, zpath = tempfile.mkstemp(prefix="rbx_", suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for base, _, files in os.walk(root):
                for fn in files:
                    full = os.path.join(base, fn)
                    zf.write(full, os.path.relpath(full, root))
        size = os.path.getsize(zpath)
        log(f"Archive ready — {size // (1024*1024)} MB", 99)
        return zpath, f"{version}-Windows.zip", size
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_mac(version, log):
    """Mac player ships as one zip; download it and hand it over verbatim."""
    name = f"{version}-RobloxPlayer.zip"
    log(f"Downloading {name} …", 5)
    data = _http_bytes(f"{RBX_BASE}{name}")
    log(f"Downloaded {len(data) // (1024*1024)} MB", 92)
    fd, zpath = tempfile.mkstemp(prefix="rbx_", suffix=".zip")
    os.close(fd)
    with open(zpath, "wb") as f:
        f.write(data)
    log("Ready", 99)
    return zpath, name, len(data)


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

    def _sse(self, event, payload):
        self.wfile.write(
            ("event: " + event + "\ndata: " + json.dumps(payload) + "\n\n").encode())
        self.wfile.flush()

    def _rdd_build(self, qs):
        """Stream the build as Server-Sent Events so the page can show a console."""
        platform = qs.get("platform", ["WindowsPlayer"])[0]
        version = qs.get("version", [""])[0].strip()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def log(msg, pct=None):
            data = {"msg": msg}
            if pct is not None:
                data["pct"] = pct
            self._sse("log", data)

        try:
            if not re.match(r"^[A-Za-z0-9._-]+$", version):
                raise RuntimeError("Invalid version string")
            builder = build_mac if platform == "MacPlayer" else build_windows
            zpath, name, size = builder(version, log)
            token = uuid.uuid4().hex
            JOBS[token] = (zpath, name)
            self._sse("done", {"token": token, "name": name, "size": size})
        except Exception as e:
            try:
                self._sse("fail", {"msg": str(e)})
            except Exception:
                pass

    def _rdd_file(self, qs):
        info = JOBS.pop(qs.get("token", [""])[0], None)
        if not info or not os.path.exists(info[0]):
            self._send(404, b"download expired or not found", "text/plain")
            return
        zpath, name = info
        size = os.path.getsize(zpath)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if self.command != "HEAD":
                with open(zpath, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 256 * 1024)
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Build/download endpoints are GET-driven (EventSource + attachment link).
        if path == "/rdd/build":
            if self.command == "HEAD":
                self._send(200, b"", "text/plain")
            else:
                self._rdd_build(qs)
            return
        if path == "/rdd/file":
            self._rdd_file(qs)
            return

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
