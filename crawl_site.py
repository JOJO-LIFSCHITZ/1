
#!/usr/bin/env python3
"""Crawl a website and save every discovered page as a separate,
self-contained HTML file. Internal links are rewritten for offline use.
JavaScript is executed by Chromium through SingleFile.
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, urldefrag, unquote, quote

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True)
ap.add_argument("--chrome", required=True)
ap.add_argument("--out", required=True)
ap.add_argument(
    "--max-pages", type=int, default=0,
    help="Maximum pages; 0 means unlimited"
)
ap.add_argument("--include", default="")
ap.add_argument("--block-images", default="false")
ap.add_argument("--workers", type=int, default=3)
args = ap.parse_args()

if args.max_pages < 0:
    ap.error("--max-pages must be 0 or greater")
if args.workers < 1:
    ap.error("--workers must be at least 1")

BLOCK_IMAGES = args.block_images.strip().lower() == "true"
INCLUDE = re.compile(args.include) if args.include.strip() else None

SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|ico|bmp|pdf|zip|7z|rar|gz|tar|"
    r"mp3|m4a|ogg|wav|mp4|webm|mkv|avi|css|js|json|xml|txt|rss|"
    r"woff2?|ttf|apk|exe|dmg)$",
    re.I,
)

LINK_RE = re.compile(
    r"<a\b[^>]*?[\s\"']href\s*=\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))",
    re.I | re.S,
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def key(url):
    """Canonical page key. Must match the viewer's URL normalization."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(p.path).rstrip("/") or "/"
    return host + path + ("?" + p.query if p.query else "")


START = urldefrag(args.url)[0]
START_HOST = key(START).split("/", 1)[0]


def wanted(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    if key(url).split("/", 1)[0] != START_HOST:
        return False
    if SKIP_EXT.search(p.path):
        return False
    if INCLUDE and not INCLUDE.search(url):
        return False
    return True


def extract_links(page_html, base_url):
    found = []
    for match in LINK_RE.finditer(page_html):
        href = html.unescape(
            (match.group(1) or match.group(2) or match.group(3) or "").strip()
        )
        if not href or href.startswith(
            ("#", "mailto:", "javascript:", "tel:", "data:", "blob:")
        ):
            continue

        full = urldefrag(urljoin(base_url, href))[0]
        if wanted(full):
            found.append(full)
    return found


def save_page(url):
    # A fresh, empty output directory avoids SingleFile's "(1).html" behavior.
    base_cmd = [
        "single-file", url, "{out}",
        f"--browser-executable-path={args.chrome}",
        '--browser-args=["--no-sandbox"]',
        "--block-videos",
        "--block-audios",
        "--remove-unused-styles",
        "--remove-alternative-fonts",
        "--compress-CSS",
    ]

    if BLOCK_IMAGES:
        base_cmd.append("--block-images")

    last_error = ""
    for attempt in range(1, 3):
        tmpdir = tempfile.mkdtemp(prefix="singlefile_")
        tmp = os.path.join(tmpdir, "page.html")
        cmd = [part.replace("{out}", tmp) for part in base_cmd]

        try:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=240
                )
                last_error = (
                    f"rc={result.returncode} | {result.stderr} | {result.stdout}"
                )[-1000:]
            except subprocess.TimeoutExpired:
                last_error = "SingleFile timed out after 240 seconds"

            for name in os.listdir(tmpdir):
                path = os.path.join(tmpdir, name)
                if not os.path.isfile(path) or os.path.getsize(path) == 0:
                    continue

                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                if "<html" in content.lower():
                    return url, content

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        print(
            f"  Attempt {attempt}/2 failed: {url} | {last_error}",
            flush=True,
        )

        if attempt == 1:
            time.sleep(2)

    return url, None


def safe_filename(url, title):
    """Stable unique name based on page title and URL."""
    title = html.unescape(title or "").strip()
    title = re.sub(r"<[^>]+>", "", title)
    title = re.sub(r"\s+", " ", title)
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)
    slug = slug.strip(" ._")[:90] or "page"
    digest = hashlib.sha256(key(url).encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.html"


def rewrite_links(document, base_url, filename_by_key):
    """Rewrite saved internal pages to local HTML files."""
    pattern = re.compile(
        r"""(?P<prefix>\bhref\s*=\s*)
            (?P<quote>["'])
            (?P<value>.*?)
            (?P=quote)""",
        re.I | re.S | re.X,
    )

    def replace(match):
        raw = html.unescape(match.group("value").strip())

        if not raw or raw.startswith(
            ("#", "mailto:", "javascript:", "tel:", "data:", "blob:")
        ):
            return match.group(0)

        try:
            absolute = urljoin(base_url, raw)
            parsed = urlparse(absolute)
        except Exception:
            return match.group(0)

        if parsed.scheme not in ("http", "https"):
            return match.group(0)

        target_key = key(urldefrag(absolute)[0])
        target_file = filename_by_key.get(target_key)

        if not target_file:
            # Keep the original link if its target was not saved.
            return match.group(0)

        local = quote(target_file)
        if parsed.fragment:
            local += "#" + quote(unquote(parsed.fragment), safe="!$&'()*+,;=:@/?-._~")
        return (
            match.group("prefix")
            + match.group("quote")
            + local
            + match.group("quote")
        )

    return pattern.sub(replace, document)


# -------------------------------------------------------------------
# Crawl. A limit of 0 means continue until the discovered queue is empty.
# -------------------------------------------------------------------

pages = {}
seen = {key(START)}
queue = [START]
failed = []
max_batch = max(1, args.workers * 4)

while queue and (args.max_pages == 0 or len(pages) < args.max_pages):
    room = (
        max_batch
        if args.max_pages == 0
        else min(max_batch, args.max_pages - len(pages))
    )
    batch, queue = queue[:room], queue[room:]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(save_page, batch))

    for url, content in results:
        if content is None:
            failed.append(url)
            print(f"FAILED {url}", flush=True)
            continue

        match = TITLE_RE.search(content)
        title = html.unescape(match.group(1)).strip() if match else url
        title = re.sub(r"\s+", " ", title) or url

        pages[key(url)] = {"u": url, "t": title, "h": content}

        links = extract_links(content, url)
        new = 0

        for link in links:
            k = key(link)
            if k not in seen:
                seen.add(k)
                queue.append(link)
                new += 1

        print(
            f"OK [{len(pages)}] {url} "
            f"(links: {len(links)}, new: {new}, queued: {len(queue)})",
            flush=True,
        )

if not pages:
    print("No pages were saved.")
    sys.exit(1)

# -------------------------------------------------------------------
# Save each page separately and rewrite links between saved pages.
# -------------------------------------------------------------------

os.makedirs(args.out, exist_ok=True)

filename_by_key = {}
used_names = set()

for k, page in pages.items():
    filename = safe_filename(page["u"], page["t"])

    # Guard against even an extremely unlikely filename collision.
    if filename in used_names:
        digest = hashlib.sha256(k.encode("utf-8")).hexdigest()
        filename = filename[:-5] + "-" + digest[12:20] + ".html"

    used_names.add(filename)
    filename_by_key[k] = filename

for k, page in pages.items():
    filename = filename_by_key[k]
    content = rewrite_links(page["h"], page["u"], filename_by_key)
    path = os.path.join(args.out, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

print()
print(f"Saved pages: {len(pages)}")
print(f"Failed pages: {len(failed)}")
print(f"Remaining queued pages: {len(queue)}")
print(f"Output directory: {os.path.abspath(args.out)}")

if failed:
    with open(os.path.join(args.out, "failed_urls.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(failed) + "\n")
    print("A list of failed URLs was saved as failed_urls.txt.")

if queue:
    with open(os.path.join(args.out, "not_processed_urls.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(queue) + "\n")
    print("WARNING: some discovered pages were not processed.")
