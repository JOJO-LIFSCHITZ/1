#!/usr/bin/env python3
"""
download_zim.py - downloads a big file from Google Drive (handles the "can't scan for viruses" page).

Usage:
    python download_zim.py <drive-file-id-or-url> <output-file>
"""
import os
import re
import subprocess
import sys
import time

import requests


def extract_id(s):
    m = re.search(r"[?&]id=([\w-]+)", s) or re.search(r"/d/([\w-]+)", s)
    return m.group(1) if m else s


def is_zim(path):
    try:
        with open(path, "rb") as f:
            return f.read(3) == b"ZIM"
    except OSError:
        return False


def try_gdown(fid, out):
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gdown"], check=True)
        r = subprocess.run([sys.executable, "-m", "gdown", "https://drive.google.com/uc?id=%s" % fid, "-O", out])
        return r.returncode == 0 and os.path.exists(out)
    except Exception as ex:
        print("gdown error:", ex)
        return False


def try_requests(fid, out):
    """Direct download through drive.usercontent.google.com with the confirm flag."""
    s = requests.Session()
    url = "https://drive.usercontent.google.com/download"
    params = {"id": fid, "export": "download", "confirm": "t"}
    r = s.get(url, params=params, stream=True, timeout=60)
    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype:
        # the warning page: pull the hidden form fields (uuid, at, ...) and ask again
        page = r.text
        fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
        if not fields:
            print("Drive returned an HTML page without a download form (no permission / quota exceeded?)")
            print(page[:500])
            return False
        fields.update({"id": fid, "export": "download", "confirm": "t"})
        r = s.get(url, params=fields, stream=True, timeout=60)
        if "text/html" in r.headers.get("Content-Type", ""):
            print("Still an HTML page - quota exceeded or file is not shared publicly")
            return False
    r.raise_for_status()
    total = int(r.headers.get("Content-Length") or 0)
    done, last = 0, time.time()
    with open(out, "wb") as f:
        for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 15:
                    last = time.time()
                    print("downloaded %.0f MB%s" % (done / 1e6, " / %.0f MB" % (total / 1e6) if total else ""), flush=True)
    return True


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    fid = extract_id(sys.argv[1])
    out = sys.argv[2]
    for attempt in range(1, 4):
        print("Attempt %d/3 (file id %s)" % (attempt, fid), flush=True)
        for fn in (try_gdown, try_requests):
            if os.path.exists(out):
                os.remove(out)
            try:
                ok = fn(fid, out)
            except Exception as ex:
                print("%s failed: %s" % (fn.__name__, ex))
                ok = False
            if ok and os.path.exists(out) and is_zim(out):
                print("OK: %s  %.1f MB" % (out, os.path.getsize(out) / 1e6))
                return
            if ok:
                print("%s downloaded something that is not a ZIM file" % fn.__name__)
        time.sleep(20)
    print("ERROR: could not download the file. Check that the Drive file is shared as 'Anyone with the link' "
          "and that the download quota was not exceeded (try 'Make a copy' in your own Drive).")
    sys.exit(1)


if __name__ == "__main__":
    main()
