#!/usr/bin/env python3
"""Download a file through a real (headless) Chrome/Chromium. Usage: chrome_download.py URL OUTFILE
Works when the URL starts a download directly, or when it is a web page that contains a link to a .zim file."""
import sys
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def main():
    url, out = sys.argv[1], sys.argv[2]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True, user_agent=UA)
        page = ctx.new_page()
        page.set_default_timeout(120000)
        dl = None

        # 1) the URL itself may start a download
        try:
            with page.expect_download(timeout=60000) as di:
                try:
                    page.goto(url, wait_until="commit")
                except Exception as ex:      # "Download is starting" is expected here
                    print("goto:", str(ex)[:120])
            dl = di.value
        except Exception as ex:
            print("no direct download:", str(ex)[:120])

        # 2) otherwise it is a web page: find a link to the .zim and click it
        if dl is None:
            try:
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception:
                pass
            print("page title:", page.title())
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.href, (e.innerText||'').trim()])")
            cand = [l for l in links if ".zim" in l[0].lower()]
            if not cand:
                cand = [l for l in links if any(w in l[1] for w in ("הורד", "הורדה", "Download", "download"))]
            print("candidate links:", cand[:5])
            if not cand:
                print("ERROR: no download link found on the page")
                sys.exit(1)
            target = cand[0][0]
            try:
                with page.expect_download(timeout=120000) as di:
                    try:
                        page.goto(target, wait_until="commit")
                    except Exception as ex:
                        print("goto:", str(ex)[:120])
                dl = di.value
            except Exception:
                with page.expect_download(timeout=120000) as di:
                    page.click('a[href="%s"]' % cand[0][0].replace('"', '\\"'))
                dl = di.value

        print("downloading:", dl.suggested_filename)
        dl.save_as(out)
        browser.close()
    print("saved", out)


if __name__ == "__main__":
    main()
