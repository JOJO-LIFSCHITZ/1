
#!/usr/bin/env python3
"""
Download the Hamichlol ZIM archive using Chromium and Playwright.

Usage:
    python chrome_download.py URL OUTFILE
"""

import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = (
    "https://dev.hamichlol.org.il/w/upload/michlol/"
    "hamichlol_he_zimnew_novid_2026-03.zim"
)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

EXPECTED_NAME = "hamichlol_he_zimnew_novid_2026-03.zim"
ZIM_MAGIC = bytes.fromhex("5a494d04")


def verify_zim(path):
    """Check that the file exists, is non-empty, and has a ZIM signature."""
    path = Path(path)

    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("The downloaded file is missing or empty.")

    with path.open("rb") as f:
        magic = f.read(4)

    if magic != ZIM_MAGIC:
        with path.open("rb") as f:
            sample = f.read(300).lower()

        if b"captcha" in sample or b"cloudflare" in sample:
            raise RuntimeError(
                "The server returned an anti-bot page, not a ZIM file."
            )

        if b"<html" in sample or b"<!doctype html" in sample:
            raise RuntimeError(
                "The server returned HTML instead of the ZIM archive."
            )

        raise RuntimeError(
            f"Invalid ZIM signature: {magic.hex()}. "
            "The server may have returned an error page."
        )

    print(f"Verified ZIM: {path.stat().st_size:,} bytes")
    return True


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else URL
    out = Path(sys.argv[2] if len(sys.argv) > 2 else EXPECTED_NAME)
    out.parent.mkdir(parents=True, exist_ok=True)

    partial = out.with_name(out.name + ".part")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            accept_downloads=True,
            user_agent=UA,
            locale="he-IL",
            ignore_https_errors=False,
        )

        page = context.new_page()
        page.set_default_timeout(120_000)

        last_error = None

        try:
            for attempt in range(1, 4):
                print(f"Attempt {attempt}/3: opening ZIM URL")

                try:
                    with page.expect_download(timeout=90_000) as info:
                        try:
                            response = page.goto(
                                url,
                                wait_until="commit",
                                timeout=90_000,
                            )
                            if response:
                                print("HTTP status:", response.status)
                        except Exception as exc:
                            # A download navigation may interrupt navigation.
                            print("Navigation:", str(exc)[:200])

                    download = info.value
                    print("Suggested filename:", download.suggested_filename)
                    download.save_as(str(partial))

                    verify_zim(partial)
                    os.replace(partial, out)

                    print("SUCCESS:", out.resolve())
                    return

                except PlaywrightTimeoutError as exc:
                    last_error = exc
                    print("No browser download event:", str(exc)[:200])

                except Exception as exc:
                    last_error = exc
                    print("Attempt failed:", str(exc)[:300])

                # Try a normal browser navigation again on the next attempt.
                time.sleep(5)

            print("Browser download did not succeed.")
            print("Last error:", str(last_error)[:500])
            print(
                "If the server requires a CAPTCHA or blocks automated clients, "
                "this script cannot guarantee a download."
            )
            sys.exit(1)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
