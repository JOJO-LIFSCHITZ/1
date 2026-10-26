#!/usr/bin/env python3
"""
Convert the Entire Otzaria Library -> plain offline HTML files.
Every book gets a folder, divided into parts if large, with a sticky index.
Maintains the original folder hierarchy and creates a global index.
"""
import argparse
import html as htmllib
import os
import posixpath
import re
import time
import zipfile
import urllib.request
import ssl
from concurrent.futures import ProcessPoolExecutor, as_completed

# ----------------------------------------------------------------------------------------------
# globals and constants
# ----------------------------------------------------------------------------------------------
BAD_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f\u200e\u200f\u202a-\u202e]')

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def fn_piece(t, maxlen=60):
    t = BAD_FN.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > maxlen:
        t = t[:maxlen].rstrip(" .")
    return t or "_"

STATIC_CSS = (
    ":root{color-scheme:light dark}"
    "body{font-family:system-ui,Arial,sans-serif;line-height:1.7;margin:0 8px 40px;overflow-wrap:anywhere}"
    "#bar{position:sticky;top:0;z-index:5;background:#202124;padding:6px 2px;white-space:nowrap;overflow-x:auto}"
    "#bar a{color:#8ab4f8;text-decoration:none;display:inline-block;padding:5px 13px;margin-left:5px;border:1px solid #5f6368;border-radius:6px}"
    "h1{font-size:1.55em;border-bottom:2px solid #666;margin:.8em 0 .4em; color:#0645ad;}"
    "h2{font-size:1.35em;border-bottom:1px solid #999;margin-top:1.3em}h3{font-size:1.2em}h4{font-size:1.1em}"
    "a{color:#0645ad}table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid #999;padding:3px 6px}"
    ".bar a{display:inline-block;padding:4px 9px;margin:2px;border:1px solid #999;border-radius:5px;text-decoration:none}"
    ".t div{padding:3px 0}"
    ".art{content-visibility:auto;contain-intrinsic-size:auto 1500px;border-bottom:2px solid #888;padding-bottom:15px; margin-bottom:15px;}"
    "details {background:#f1f3f4; padding:10px; margin-bottom:10px; border-radius:5px;}"
    "summary {font-weight:bold; cursor:pointer; font-size:1.1em;}"
    ".pm{color:#fff;background:#1a73e8;border-radius:4px;padding:0 6px;font-size:.8em;margin-left:6px}"
    "@media (prefers-color-scheme:dark){body{background:#121212;color:#e4e4e4}h1{color:#8ab4f8}a{color:#8ab4f8}details{background:#2a2a2a;}}"
)

BAR_HTML = ('<div id="bar"><a href="#top">&#8593;</a><a href="#toc">לכותרות</a><a href="#p25">25%</a>'
            '<a href="#p50">50%</a><a href="#p75">75%</a><a href="#p100">100%</a></div>')

def static_head(title):
    return ('<!doctype html>\n<html lang="he" dir="rtl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body>\n%s' % (htmllib.escape(title), STATIC_CSS, BAR_HTML))

def emit_sequential_list(out, entries):
    n = len(entries)
    if n == 0: return
    pp = {}
    for pct in (25, 50, 75, 100):
        pos = min(n - 1, max(0, -(-n * pct // 100) - 1))
        pp.setdefault(pos, []).append(pct)
    out.write('<div class="t" id="toc">\n')
    for pos, (title, link_html) in enumerate(entries):
        marks = "".join(f'<span id="p{p}"></span><b class="pm">{p}%</b>' for p in pp.get(pos, []))
        out.write(f"<div>{marks}{link_html}</div>\n")
    out.write("</div>\n")

def find_library_root(base_dir):
    for root, dirs, files in os.walk(base_dir):
        if any(d in dirs for d in ['תנ"ך', 'משנה', 'תלמוד בבלי', 'הלכה']):
            return root
    return base_dir

# ----------------------------------------------------------------------------------------------
# Processing a single book
# ----------------------------------------------------------------------------------------------
def process_book_task(args):
    txt_path, base_dir, out_dir, part_limit_bytes = args
    
    rel_path = os.path.relpath(txt_path, base_dir)
    book_rel_dir = os.path.splitext(rel_path)[0]
    book_name = os.path.basename(book_rel_dir)
    
    book_out_dir = os.path.join(out_dir, book_rel_dir)
    os.makedirs(book_out_dir, exist_ok=True)
    
    try:
        with open(txt_path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except Exception:
        return None
        
    # Split by any HTML heading tag
    parts = re.split(r'^(<h[1-6][^>]*>.*?</h[1-6]>)\s*$', text, flags=re.MULTILINE | re.IGNORECASE)
    
    sections = []
    if parts[0].strip():
        sections.append(("תחילת הספר", parts[0].strip()))
        
    for i in range(1, len(parts), 2):
        header_html = parts[i].strip()
        content = parts[i+1].strip()
        clean_title = re.sub(r'<[^>]+>', '', header_html).strip() or "חלק"
        sections.append((clean_title, header_html + "\n" + content))
        
    chunks, current_chunk, current_size = [], [], 0
    for sec_i, (title, body) in enumerate(sections):
        body_html = body.replace('\n', '<br>\n')
        body_html = re.sub(r'(</h[1-6]>)<br>\n', r'\1\n', body_html)
        body_html = re.sub(r'<br>\n(<h[1-6]>)', r'\n\1', body_html)
        
        section_html = f'<section class="art" id="a{sec_i}">\n{body_html}\n<p><br><a href="#toc">&#8593; חזרה לכותרות החלק הזה</a></p></section>\n'
        cost = len(section_html.encode("utf-8"))
        
        if current_size + cost > part_limit_bytes and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
            
        current_chunk.append((sec_i, title, section_html))
        current_size += cost
        
    if current_chunk:
        chunks.append(current_chunk)
        
    files_created = []
    index_entries = []
    width = max(2, len(str(len(chunks))))
    
    for ci, chunk in enumerate(chunks, 1):
        file_name = f"{ci:0{width}d}_{fn_piece(book_name)}.html"
        file_entries = []
        for sec_i, title, section_html in chunk:
            file_entries.append((title, f'<a href="#a{sec_i}">{htmllib.escape(title)}</a>'))
            index_entries.append((title, f'<a href="{file_name}#a{sec_i}">{htmllib.escape(title)}</a>'))
            
        chunk_path = os.path.join(book_out_dir, file_name)
        with open(chunk_path, "w", encoding="utf-8") as out:
            out.write(static_head(f"{book_name} - קובץ {ci}"))
            out.write(f'<h1 id="top">{htmllib.escape(book_name)} - קובץ {ci} מתוך {len(chunks)}</h1>\n')
            out.write(f'<p><a href="000_אינדקס.html">חזור לאינדקס הספר</a></p>\n')
            emit_sequential_list(out, file_entries)
            out.write("<hr>\n")
            for sec_i, title, section_html in chunk:
                out.write(section_html)
            out.write("</body></html>\n")
            
        files_created.append((chunk_path, posixpath.join(book_rel_dir.replace('\\', '/'), file_name)))
        
    index_path = os.path.join(book_out_dir, "000_אינדקס.html")
    with open(index_path, "w", encoding="utf-8") as out:
        out.write(static_head(f"אינדקס: {book_name}"))
        out.write(f'<h1 id="top">אינדקס הספר: {htmllib.escape(book_name)}</h1>\n')
        out.write(f'<p><a href="../000_ספריית_אוצריא.html" style="font-weight:bold; color:red;">&#8594; חזרה לספרייה הראשית</a></p>\n')
        out.write(f'<p>הספר מחולק ל-{len(chunks)} קבצים. להלן רשימת הכותרות:</p>\n')
        emit_sequential_list(out, index_entries)
        out.write("</body></html>\n")
        
    index_rel_path = posixpath.join(book_rel_dir.replace('\\', '/'), "000_אינדקס.html")
    files_created.append((index_path, index_rel_path))
    
    return book_rel_dir, book_name, index_rel_path, files_created

def process_book_task_wrapper(args):
    try:
        return process_book_task(args)
    except Exception:
        return None

# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="", help="Path to Otzaria library (empty to auto-download)")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--part-mb", type=int, default=7, help="target size of one HTML file (MB)")
    ap.add_argument("--zip-limit-mb", type=int, default=1800)
    ap.add_argument("--zip", action="store_true", default=True)
    ap.add_argument("--base-name", default="Otzaria-Offline")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    
    input_dir = args.input
    if not input_dir:
        input_dir = os.path.join(args.workdir, "library")
        if not os.path.exists(input_dir) or not any(os.scandir(input_dir)):
            os.makedirs(input_dir, exist_ok=True)
            ssl._create_default_https_context = ssl._create_unverified_context
            url = "https://github.com/Otzaria/otzaria-library/releases/latest/download/otzaria_latest.zip"
            zip_path = os.path.join(args.workdir, "otzaria_latest.zip")
            log(f"Downloading library from {url} ... (this takes time, ~800MB)")
            urllib.request.urlretrieve(url, zip_path)
            log("Download complete. Extracting...")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(input_dir)
            log("Extraction complete.")
            os.remove(zip_path)
        else:
            log(f"Found existing library in {input_dir}")
            
    input_dir = find_library_root(input_dir)
    log(f"Library root identified at: {input_dir}")
    
    tasks = []
    for root, dirs, files in os.walk(input_dir):
        for file in files:
            if file.lower().endswith(".txt"):
                tasks.append((os.path.join(root, file), input_dir, args.outdir, args.part_mb * 1024 * 1024))
                
    log(f"Found {len(tasks)} books. Starting parallel processing...")
    
    books_processed = []
    all_created_files = []
    
    with ProcessPoolExecutor() as executor:
        for result in executor.map(process_book_task_wrapper, tasks):
            if result:
                book_rel_dir, book_name, index_rel_path, created_files = result
                books_processed.append((book_rel_dir, book_name, index_rel_path))
                all_created_files.extend(created_files)
                if len(books_processed) % 200 == 0:
                    log(f"Processed {len(books_processed)} / {len(tasks)} books...")
                    
    log(f"Finished processing all {len(books_processed)} books. Building main index...")
    
    # Build Main Index
    tree = {}
    for book_rel_dir, book_name, index_rel_path in books_processed:
        category = os.path.dirname(book_rel_dir).replace('\\', '/')
        if category == "": category = "כללי"
        tree.setdefault(category, []).append((book_name, index_rel_path))
        
    main_index_path = os.path.join(args.outdir, "000_ספריית_אוצריא.html")
    with open(main_index_path, "w", encoding="utf-8") as out:
        out.write(static_head("ספריית אוצריא - אינדקס ראשי"))
        out.write('<h1 id="top">ספריית אוצריא - אינדקס ראשי</h1>\n')
        out.write(f'<p>הספרייה כוללת <b>{len(books_processed)}</b> ספרים. לחץ על קטגוריה כדי לפתוח אותה:</p>\n')
        
        for cat in sorted(tree.keys()):
            out.write(f'<details><summary>{htmllib.escape(cat)} ({len(tree[cat])} ספרים)</summary>\n<ul>\n')
            for book_name, index_rel_path in sorted(tree[cat], key=lambda x: x[0]):
                out.write(f'<li><a href="{index_rel_path}">{htmllib.escape(book_name)}</a></li>\n')
            out.write('</ul></details>\n')
        out.write("</body></html>\n")
        
    if not args.zip:
        log(f"Done in {(time.time() - t0)/60:.1f} min. Files are in {args.outdir}")
        return
        
    log("Zipping all files...")
    limit = args.zip_limit_mb * 1024 * 1024
    zc, zi, z = 0, 1, None
    
    def get_zpath(idx):
        if idx == 1: return os.path.join(args.outdir, f"{args.base_name}.zip")
        return os.path.join(args.outdir, f"{args.base_name}-{idx:02d}.zip")
        
    zpath = get_zpath(zi)
    z = zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
    z.write(main_index_path, arcname="000_ספריית_אוצריא.html")
    os.remove(main_index_path)
    
    for path, arcname in all_created_files:
        if zc > limit:
            z.close()
            log(f"Zipped {os.path.basename(zpath)}")
            zi += 1
            zpath = get_zpath(zi)
            z = zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
            zc = 0
        z.write(path, arcname=arcname)
        zc += os.path.getsize(path)
        os.remove(path)
        
    z.close()
    log(f"Zipped {os.path.basename(zpath)}")
    log(f"Finished completely in {(time.time() - t0)/60:.1f} minutes!")

if __name__ == "__main__":
    main()
