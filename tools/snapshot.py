#!/usr/bin/env python3
"""
Snapshot the inject.today API into static JSON files under data/.

The GitHub Pages site reads these files directly (no backend). This script is
run both locally (to seed the first snapshot) and by GitHub Actions on a
schedule to refresh the data.

Mirrors the proxy path layout so the frontend can fetch `data/<path>.json`:
    data/cheats.json
    data/versions/current.json   (+ future, previous)
    data/cheats/reviews/<id>.json
    data/cheats/updates/<id>.json

No third-party dependencies — Python 3.8+ standard library only.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

API_BASE = "https://inject.today/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def fetch(path, tries=3):
    """GET <API_BASE>/<path> and return parsed JSON, with simple retries."""
    url = f"{API_BASE}/{path}"
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {path}: {last}")


def write_json(rel_path, obj):
    out = DATA / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main():
    DATA.mkdir(exist_ok=True)

    # 1) versions
    for ch in ("current", "future", "previous"):
        try:
            write_json(f"versions/{ch}.json", fetch(f"versions/{ch}"))
            print(f"  versions/{ch} ok")
        except Exception as e:  # noqa: BLE001
            print(f"  versions/{ch} FAILED: {e}")

    # 2) cheats (the main list) — required; abort if it fails so we keep the
    #    previous good snapshot instead of committing garbage.
    cheats = fetch("cheats")
    cheats.pop("undefined", None)
    write_json("cheats.json", cheats)
    print(f"  cheats ok ({len(cheats)} entries)")

    # 3) per-cheat reviews + updates
    ids = sorted({c.get("Identifier") for c in cheats.values() if c.get("Identifier")})
    ok = fail = 0
    for i, cid in enumerate(ids, 1):
        for kind in ("reviews", "updates"):
            try:
                write_json(f"cheats/{kind}/{cid}.json", fetch(f"cheats/{kind}/{cid}"))
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  cheats/{kind}/{cid} FAILED: {e}")
        time.sleep(0.15)  # be polite
        if i % 10 == 0:
            print(f"  ...{i}/{len(ids)} cheats")
    print(f"done: {ok} detail files written, {fail} failed")

    # stamp
    write_json("meta.json", {"count": len(cheats), "ids": len(ids)})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"SNAPSHOT FAILED: {e}")
        sys.exit(1)
