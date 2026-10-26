#!/usr/bin/env python3
"""
Talmudic Encyclopedia (Otzarya Unofficial) txt -> plain offline HTML files.
Based on the Kiwix/Wikipedia offline HTML generator.
"""
import argparse
import html as htmllib
import os
import posixpath
import re
import sqlite3
import sys
import time
import zipfile

# ----------------------------------------------------------------------------------------------
# globals and constants
# ----------------------------------------------------------------------------------------------
FINAL_LETTERS = str.maketrans("ךםןףץ", "כמנפצ")
NIKUD_RE = re.compile(r"[\u0591-\u05C7]")
HEB_LETTERS = "אבגדהוזחטיכלמנסעפצקרשת"
FOLDER_NAMES = list(HEB_LETTERS) + ["אחר"]
INDEX_NAME = "000_אינדקס"
LINK_RE = re.compile(r'<a data-i="(\d+)">(.*?)</a>', re.S)

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def norm_key(t):
    return NIKUD_RE.sub("", t).translate(FINAL_LETTERS).lower()

def folder_of(t):
    for ch in norm_key(t):
        if ch.isalnum():
            return HEB_LETTERS.index(ch) if ch in HEB_LETTERS else 22
    return 22

STATIC_CSS = (
    ":root{color-scheme:light dark}"
    "body{font-family:system-ui,Arial,sans-serif;line-height:1.7;margin:0 8px 40px;overflow-wrap:anywhere}"
    "#bar{position:sticky;top:0;z-index:5;background:#202124;padding:6px 2px;white-space:nowrap;overflow-x:auto}"
    "#bar a{color:#8ab4f8;text-decoration:none;display:inline-block;padding:5px 13px;margin-left:5px;border:1px solid #5f6368;border-radius:6px}"
    "h1{font-size:1.45em;border-bottom:1px solid #999;margin:.8em 0 .4em}"
    "h2{font-size:1.25em;border-bottom:1px solid #999;margin-top:1.3em}h3{font-size:1.1em}h4{font-size:1.05em}"
    "a{color:#0645ad}table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid #999;padding:3px 6px}"
    ".bar a{display:inline-block;padding:4px 9px;margin:2px;border:1px solid #999;border-radius:5px;text-decoration:none}"
    ".t div{padding:3px 0}"
    ".art{content-visibility:auto;contain-intrinsic-size:auto 1500px;border-bottom:2px solid #888;padding-bottom:6px}"
    ".pm{color:#fff;background:#1a73e8;border-radius:4px;padding:0 6px;font-size:.8em;margin-left:6px}"
    "@media (prefers-color-scheme:dark){body{background:#121212;color:#e4e4e4}a{color:#8ab4f8}}"
)

BAR_HTML = ('<div id="bar"><a href="#top">&#8593;</a><a href="#toc">התחלה</a><a href="#p25">25%</a>'
            '<a href="#p50">50%</a><a href="#p75">75%</a><a href="#p100">100%</a></div>')

BAD_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f\u200e\u200f\u202a-\u202e]')

def fn_piece(t, maxlen=35):
    t = BAD_FN.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > maxlen:
        t = t[:maxlen].rstrip(" .")
    return t or "_"

def static_head(title):
    return ('<!doctype html>\n<html lang="he" dir="rtl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body>\n%s' % (htmllib.escape(title), STATIC_CSS, BAR_HTML))

def gkey(k, plen):
    k = re.sub(r"^[\W_]+", "", k)
    return k[:plen] or "?"

def letter_groups(keys):
    out, last = [], None
    for pos, k in enumerate(keys):
        c = k.upper()
        if c != last:
            out.append((c, pos))
            last = c
    return out

def emit_list(out, entries, plen):
    esc = htmllib.escape
    n = len(entries)
    groups = letter_groups([gkey(e[0], plen) for e in entries])
    out.write('<div class="bar">%s</div>' % "".join(
        '<a href="#g%d">%s</a>' % (gi, esc(c)) for gi, (c, _) in enumerate(groups)))
    pp = {}
    for pct in (25, 50, 75, 100):
        pos = min(n - 1, max(0, -(-n * pct // 100) - 1))
        pp.setdefault(pos, []).append(pct)
    out.write('<div class="t" id="toc">')
    gi = 0
    for pos, e in enumerate(entries):
        if gi < len(groups) and groups[gi][1] == pos:
            out.write('<h2 id="g%d">%s</h2>' % (gi, esc(groups[gi][0])))
            gi += 1
        marks = "".join('<span id="p%d"></span><b class="pm">%d%%</b>' % (p, p) for p in pp.get(pos, []))
        out.write("<div>%s%s</div>" % (marks, e[1]))
    out.write("</div>")

def build_db(args):
    dbpath = os.path.join(args.workdir, "cleaned.db")
    if os.path.exists(dbpath):
        os.remove(dbpath)
    db = sqlite3.connect(dbpath)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, h TEXT)")
    
    import glob
    # Find txt files only in the root of the input folder
    txt_files = glob.glob(os.path.join(args.input, "*.txt"))
    
    arts_dict = {}
    global_art_idx = 0
    
    for txt_path in txt_files:
        base_name = os.path.basename(txt_path)
        log(f"Processing {base_name} ...")
        
        # בניית נתיב לקובץ ההערות
        fn_path = os.path.join(args.input, "הערות על אנציקלופדיה", "הערות על " + base_name)
        footnotes = {}
        if os.path.exists(fn_path):
            with open(fn_path, "r", encoding="utf-8") as fn_f:
                fn_text = fn_f.read()
                # חלוקה להערות נפרדות לפי מספור בתחילת שורה
                lines = re.split(r'\n(?=\s*(?:@\d+\s*)?\d+[.)\]]?\s)', fn_text)
                for line in lines:
                    line = line.strip()
                    if not line: continue
                    m = re.match(r'^(?:@\d+\s*)?(\d+)[.)\]]?\s*(.*)', line, re.S)
                    if m:
                        num, content = m.groups()
                        footnotes[num] = content.strip()
                        
        with open(txt_path, "r", encoding="utf-8") as t_f:
            text = t_f.read()
            
        # פיצול לערכים לפי הכותרת <h2>
        parts = re.split(r'<h2[^>]*>(.*?)</h2>', text)
        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            body = parts[i+1].strip()
            title = re.sub(r'<[^>]+>', '', title).strip() # ניקוי השם מ-HTML
            
            # המרת ירידות שורות לתגיות HTML כדי למנוע הפיכה לגוש אחד (תוך התחשבות בכותרות)
            body = body.replace('\n', '<br>\n')
            body = re.sub(r'(</h[1-6]>)<br>\n', r'\1\n', body)
            body = re.sub(r'<br>\n(<h[1-6]>)', r'\n\1', body)

            article_fns = []
            curr_idx = global_art_idx
            
            # פונקציה להחלפת הערות השוליים בטקסט ולשליפתן לזיכרון
            def repl_sup(m):
                sup_attrs = m.group(1)
                num = m.group(2).strip()
                fn_txt = footnotes.get(num, "הערה חסרה בקובץ המקור")
                # רק אם עוד לא הוספנו לרשימה באותו ערך
                if not any(f[0] == num for f in article_fns):
                    article_fns.append((num, fn_txt))
                # יצירת קישור לחיץ
                return f'<sup{sup_attrs} id="a{curr_idx}_ref_{num}"><a style="text-decoration:none;" href="#a{curr_idx}_fn_{num}">{num}</a></sup>'
            
            body = re.sub(r'<sup([^>]*)>(.*?)</sup>', repl_sup, body)
            
            # אם יש הערות בערך, נוסיף אותן יפה בסוף הערך
            if article_fns:
                body += '\n<hr style="margin-top:2em; border-top:1px solid #ccc;">\n<div class="footnotes" style="font-size:0.9em; color:#bbb;">\n'
                for num, txt in article_fns:
                    body += f'<div id="a{curr_idx}_fn_{num}" style="margin-bottom:0.5em;"><a style="text-decoration:none;" href="#a{curr_idx}_ref_{num}">^{num}</a> {txt}</div>\n'
                body += '</div>\n'
                
            # טיפול בכפילויות של שמות ערכים למקרה נדיר
            if title in arts_dict:
                suffix = 2
                while f"{title} ({suffix})" in arts_dict:
                    suffix += 1
                title = f"{title} ({suffix})"
                
            arts_dict[title] = body
            global_art_idx += 1
            
    log(f"Extracted {len(arts_dict)} articles successfully.")
    
    # מיון וסידור בדיוק כמו בויקיפדיה (לפי אותיות ושמות הערכים)
    order = sorted(arts_dict.keys(), key=lambda t: (folder_of(t), norm_key(t), t))
    titles = order
    N = len(titles)
    
    # שמירה לדאטהבייס
    rows = [(i, arts_dict[t]) for i, t in enumerate(titles)]
    db.executemany("INSERT INTO a VALUES (?,?)", rows)
    db.commit()
    
    return db, titles, N

def build_static(args, db, titles, N):
    import bisect
    esc = htmllib.escape
    part_limit = int(args.part_mb * 1024 * 1024)
    fol = [folder_of(t) for t in titles]

    parts, cur = [], None
    for aid, ln in db.execute("SELECT id, length(CAST(h AS BLOB)) FROM a ORDER BY id"):
        cost = ln + len(titles[aid].encode("utf-8")) * 2 + 320
        if cur is None or cur["f"] != fol[aid] or (cur["size"] + cost > part_limit and cur["hi"] > cur["lo"]):
            cur = {"f": fol[aid], "lo": aid, "hi": aid, "size": 20000}
            parts.append(cur)
        cur["hi"] = aid + 1
        cur["size"] += cost
        
    per_folder = {}
    for p in parts:
        per_folder.setdefault(p["f"], []).append(p)
    width = max(3, max(len(str(len(v))) for v in per_folder.values()))
    for f, plist in per_folder.items():
        for n, p in enumerate(plist, 1):
            p["n"] = n
            p["dir"] = "%02d_%s" % (f + 1, FOLDER_NAMES[f])
            p["name"] = "%0*d_%s - %s.html" % (width, n, fn_piece(titles[p["lo"]]), fn_piece(titles[p["hi"] - 1]))
    los = [p["lo"] for p in parts]
    
    def pidx(aid):
        return bisect.bisect_right(los, aid) - 1

    items = []
    by_f = {}
    for j in range(N):
        p = parts[pidx(j)]
        by_f.setdefault(p["f"], []).append((norm_key(titles[j]), "%s <b>[%0*d]</b>" % (esc(titles[j]), width, p["n"])))
        
    chunk_limit = max(part_limit - 150000, part_limit // 2)
    for f in sorted(by_f):
        entries = by_f[f]
        entries.sort(key=lambda e: e[0])
        chunks, cur_c, size = [], [], 0
        for e in entries:
            cur_c.append(e)
            size += len(e[1].encode("utf-8")) + 20
            if size >= chunk_limit:
                chunks.append(cur_c)
                cur_c, size = [], 0
        if cur_c:
            chunks.append(cur_c)
        inames = [INDEX_NAME + ".html"] if len(chunks) == 1 else ["%s %d.html" % (INDEX_NAME, i + 1) for i in range(len(chunks))]
        table = "".join('<div>%0*d &#8594; %s</div>' % (width, p["n"], esc(p["name"][:-5])) for p in per_folder.get(f, []))
        d = "%02d_%s" % (f + 1, FOLDER_NAMES[f])
        os.makedirs(os.path.join(args.outdir, d), exist_ok=True)
        for ci, chunk in enumerate(chunks):
            path = os.path.join(args.outdir, d, inames[ci])
            with open(path, "w", encoding="utf-8") as out:
                out.write(static_head("%s - אינדקס %s" % (args.title, FOLDER_NAMES[f])))
                out.write('<h1 id="top">%s - אינדקס אות %s%s</h1>' % (esc(args.title), esc(FOLDER_NAMES[f]),
                          (" (חלק %d מתוך %d)" % (ci + 1, len(chunks))) if len(chunks) > 1 else ""))
                out.write('<p>חפש כאן שם ערך. ליד כל שם מופיע <b>מספר הקובץ</b>.</p>')
                out.write('<details><summary>רשימת הקבצים בתיקייה</summary><div class="t">%s</div></details>' % table)
                emit_list(out, chunk, 2 if f < 22 else 1)
                out.write("</body></html>\n")
            items.append((path, d + "/" + inames[ci]))
            
    for i, p in enumerate(parts):
        f, lo, hi = p["f"], p["lo"], p["hi"]
        plist = per_folder[f]
        path = os.path.join(args.outdir, p["dir"], p["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prev_p = parts[i - 1] if i > 0 and parts[i - 1]["f"] == f else None
        next_p = parts[i + 1] if i + 1 < len(parts) and parts[i + 1]["f"] == f else None

        def sub_link(m, lo=lo, hi=hi):
            t = int(m.group(1))
            return '<a href="#a%d">%s</a>' % (t, m.group(2)) if lo <= t < hi else m.group(2)

        entries = [(norm_key(titles[j]), '<a href="#a%d">%s</a>' % (j, esc(titles[j]))) for j in range(lo, hi)]
        with open(path, "w", encoding="utf-8") as out:
            out.write(static_head("%s - %s %d: %s - %s" % (args.title, FOLDER_NAMES[f], p["n"], titles[lo], titles[hi - 1])))
            out.write('<h1 id="top">%s - אות %s - קובץ %d מתוך %d</h1>' % (esc(args.title), esc(FOLDER_NAMES[f]), p["n"], len(plist)))
            out.write('<p>ערכים: <b>%s</b> עד <b>%s</b> (%d ערכים)<br>%s%s'
                      'אינדקס כללי של האות: <b>%s</b> בתיקייה זו.<br>'
                      'קישור לערך שנמצא בקובץ אחר אינו פעיל - חפש את שמו באינדקס.</p>' % (
                          esc(titles[lo]), esc(titles[hi - 1]), hi - lo,
                          ("קובץ קודם: %s<br>" % esc(prev_p["name"][:-5])) if prev_p else "",
                          ("קובץ הבא: %s<br>" % esc(next_p["name"][:-5])) if next_p else "",
                          INDEX_NAME))
            emit_list(out, entries, 2 if f < 22 else 1)
            out.write("<hr>\n")
            for aid, h in db.execute("SELECT id, h FROM a WHERE id>=? AND id<? ORDER BY id", (lo, hi)):
                out.write('<section class="art" id="a%d"><h1>%s</h1>\n%s\n<p><a href="#toc">&#8593; לרשימת הכותרות</a></p></section>\n'
                          % (aid, esc(titles[aid]), LINK_RE.sub(sub_link, h)))
            out.write("</body></html>\n")
        items.append((path, p["dir"] + "/" + p["name"]))
        log(f"Wrote {p['dir']}/{p['name']} (articles {lo}-{hi - 1})")
        
    return items

def zip_outputs(args, items, t0):
    if args.zip:
        limit = args.zip_limit_mb * 1024 * 1024
        z, zc, last_c, zi = None, 0, 0, 0
        zips, dirs = [], set()
        for path, arc in items:
            if z is None or zc + last_c * 1.2 > limit:
                if z is not None:
                    z.close()
                zi += 1
                zpath = os.path.join(args.outdir, "%s-%02d.zip" % (args.base_name, zi))
                z = zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
                zips.append(zpath)
                zc = 0
            z.write(path, arcname=arc)
            last_c = z.infolist()[-1].compress_size
            zc += last_c
            os.remove(path)
            dirs.add(os.path.dirname(path))
        if z is not None:
            z.close()
        for d in dirs:
            try:
                os.rmdir(d)
            except OSError:
                pass
        if len(zips) == 1:
            final = os.path.join(args.outdir, "%s.zip" % args.base_name)
            os.replace(zips[0], final)
            zips = [final]
        for zpath in zips:
            log("Zipped %s (%.0f MB)" % (os.path.basename(zpath), os.path.getsize(zpath) / 1048576))
    log("Finished in %.1f minutes." % ((time.time() - t0) / 60))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to folder_0014 containing txt and הערות folders")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--part-mb", type=int, default=10, help="target size of one HTML file (MB)")
    ap.add_argument("--zip-limit-mb", type=int, default=1900)
    ap.add_argument("--title", default="האנציקלופדיה התלמודית")
    ap.add_argument("--zip", action="store_true", default=True, help="Pack everything into a ZIP")
    ap.add_argument("--base-name", default="Talmudic-Encyclopedia")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)

    t0 = time.time()
    log("Starting conversion...")
    
    db, titles, N = build_db(args)
    items = build_static(args, db, titles, N)
    
    db.close()
    zip_outputs(args, items, t0)

if __name__ == "__main__":
    main()
