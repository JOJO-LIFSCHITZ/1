#!/usr/bin/env python3
"""
Beta: builds test HTML files with fake Hebrew articles, using exactly the same viewer/format as wiki_he_to_html.py:
  * Beta-<N>MB.html            viewer files (JavaScript viewer) - only if --sizes is given
  * Beta-Static-<N>MB.html     plain HTML, NO JavaScript needed (default: 30, 50 and 100 MB) - same format as the real output
Every viewer file shows a line at the top: "JavaScript: works" (if it says "not working" the app has JavaScript off).
Purpose: check on the phone (HTML viewer app) which file size opens, how long it takes to load,
and that search / browse / links / back / font size work.

Run:  python3 make_beta_files.py            (needs: pip install lxml, and wiki_he_to_html.py in the same folder)
Out:  output_beta/Beta-30MB.html, Beta-50MB.html, Beta-70MB.html
"""
import argparse
import datetime
import os
import random
import re
import time

import wiki_he_to_html as W

LETTERS = "אבגדהוזחטיכלמנסעפצקרשת"
SIZES_MB = []            # JavaScript viewer files: none by default (the app blocks JavaScript)
STATIC_MB = [30, 50, 100]
BANNER = ('<div id="js" style="background:#fff3cd;color:#000;padding:4px 8px;font-size:14px">JavaScript: לא עובד (אם השורה הזו נשארת כך, האפליקציה חוסמת JavaScript)</div>'
          '<script>document.getElementById("js").textContent="JavaScript: עובד ✓";document.getElementById("js").style.background="#d4edda";</script>')


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def make_vocab(rnd, n=6000):
    return ["".join(rnd.choice(LETTERS) for _ in range(rnd.randint(2, 7))) for _ in range(n)]


def make_article(rnd, vocab, i, total_hint):
    def para(nw):
        return " ".join(rnd.choice(vocab) for _ in range(nw))

    h = ["<p><b>ערך בדיקה %06d</b> %s.</p>" % (i, para(40))]
    for s in range(rnd.randint(2, 5)):
        h.append("<h2>%s</h2>" % para(3))
        for _ in range(rnd.randint(1, 3)):
            # a link to a nearby article (usually same file) and sometimes to a far one (other file)
            t = (i + rnd.randint(1, 50)) if rnd.random() < 0.8 else rnd.randint(0, max(1, total_hint) - 1)
            h.append('<p>%s <a data-i="%d">ערך בדיקה %06d</a> %s.</p>' % (para(60), t, t, para(50)))
        if rnd.random() < 0.3:
            h.append("<table><tr><th>%s</th><th>%s</th></tr><tr><td>%s</td><td>%s</td></tr></table>"
                     % (para(1), para(1), para(4), para(4)))
    h.append("<ul>%s</ul>" % "".join("<li>%s</li>" % para(6) for _ in range(rnd.randint(2, 6))))
    return "".join(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="output_beta")
    ap.add_argument("--sizes", default=",".join(map(str, SIZES_MB)), help="viewer files, comma separated MB (decimals ok)")
    ap.add_argument("--static-sizes", default=",".join(map(str, STATIC_MB)), help="plain-HTML files, comma separated MB (empty = none)")
    ap.add_argument("--block-kb", type=int, default=512)
    args = ap.parse_args()
    sizes = [float(x) for x in args.sizes.split(",") if x.strip()]
    os.makedirs(args.outdir, exist_ok=True)

    rnd = random.Random(12345)
    vocab = make_vocab(rnd)
    block_bytes = args.block_kb * 1024
    # rough guess of how many articles will exist, only used to aim some links at "other files"
    total_hint = int(sum(sizes) * 280)

    # ---- build the packed blocks of every file
    files = []          # dicts: lo, hi, bs, blobs(list of bytes), bytes
    next_id = 0
    t0 = time.time()
    for mb in sizes:
        target = int(mb * 1000 * 1000)
        f = {"mb": mb, "lo": next_id, "bs": [], "blobs": [], "bytes": 0}
        buf, size, start = [], 0, next_id
        while True:
            est_index = (next_id - f["lo"]) * 30 * 0.8        # titles: ~30 bytes each, compressed
            if f["bytes"] + est_index + len(W.HEAD) * 2 + len(W.VIEWER_JS) * 2 >= target:
                break
            if not buf:
                start = next_id
            h = make_article(rnd, vocab, next_id, total_hint)
            buf.append(h)
            next_id += 1
            size += len(h.encode("utf-8"))
            if size >= block_bytes:
                _, cnt, b64 = W.compress_block((start, len(buf), "\x01".join(buf).encode("utf-8")))
                f["bs"].append(start)
                f["blobs"].append(b64)
                f["bytes"] += len(b64) + 64
                buf, size = [], 0
        if buf:
            _, cnt, b64 = W.compress_block((start, len(buf), "\x01".join(buf).encode("utf-8")))
            f["bs"].append(start)
            f["blobs"].append(b64)
            f["bytes"] += len(b64) + 64
        f["hi"] = next_id
        files.append(f)
        log("file %dMB: %d articles, %.1f MB of blocks" % (mb, f["hi"] - f["lo"], f["bytes"] / 1e6))

    total = next_id
    titles = ["ערך בדיקה %06d" % i for i in range(total)]
    names = ["Beta-%gMB.html" % f["mb"] for f in files]
    plist = [{"lo": f["lo"], "hi": f["hi"], "name": names[i], "a": titles[f["lo"]], "z": titles[f["hi"] - 1]}
             for i, f in enumerate(files)]
    built = datetime.date.today().isoformat()

    # ---- write the files
    for i, f in enumerate(files):
        tt = W.deflate_b64("\n".join(titles[f["lo"]:f["hi"]]))
        rr = W.deflate_b64("")
        title = "בדיקת בטא %gMB" % f["mb"]
        head = W.HEAD.replace("@@TITLE@@", title).replace("@@TT@@", tt).replace("@@RR@@", rr).replace('<div id="bar">', BANNER + '<div id="bar">', 1).encode("utf-8")
        meta = {"v": 2, "part": i, "parts": plist, "bs": f["bs"], "zim": "fake-test-data", "built": built,
                "title": title, "total": total}
        path = os.path.join(args.outdir, names[i])
        with open(path, "wb") as out:
            out.write(head)
            for bn, blob in enumerate(f["blobs"]):
                out.write(b'<script type="text/plain" id="b%d">' % bn)
                out.write(blob)
                out.write(b"</script>\n")
            out.write(W.make_tail(meta))
        log("wrote %s  %.1f MB" % (path, os.path.getsize(path) / 1e6))
    # ---- plain static HTML files (no JavaScript at all)
    link_re = re.compile(r'<a data-i="(\d+)">(.*?)</a>', re.S)
    for smb in [float(x) for x in args.static_sizes.split(",") if x.strip()]:
        target = int(smb * 1000 * 1000)
        arts, sizeb, n = [], 0, 0
        while sizeb < target - 200000:
            h = make_article(rnd, vocab, n, 1)
            arts.append(h)
            sizeb += len(h.encode("utf-8")) + 200
            n += 1
        toc = "".join('<div><a href="#a%d">ערך בדיקה %06d</a></div>' % (i, i) for i in range(n))
        path = os.path.join(args.outdir, "Beta-Static-%gMB.html" % smb)
        with open(path, "w", encoding="utf-8") as out:
            out.write(W.static_head("בדיקה - HTML רגיל %gMB" % smb))
            out.write('<h1 id="top">HTML רגיל ללא JavaScript - %d ערכים</h1><div id="toc">%s</div><hr>' % (n, toc))
            for i, h in enumerate(arts):
                h = link_re.sub(lambda m: ('<a href="#a%s">%s</a>' % (m.group(1), m.group(2)))
                                if int(m.group(1)) < n else m.group(2), h)
                out.write('<h1 id="a%d">ערך בדיקה %06d</h1>%s<p><a href="#top">&#8593; לראש הקובץ (אינדקס)</a></p><hr>\n' % (i, i, h))
            out.write("</body></html>\n")
        log("wrote %s  %.1f MB  (%d articles)" % (path, os.path.getsize(path) / 1e6, n))
    log("done in %.0fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
