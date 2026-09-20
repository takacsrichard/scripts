#!/usr/bin/env python3
import os
import re
import zipfile
from pathlib import Path
from collections import Counter
from xml.etree import ElementTree as ET

BASE_DIR = Path.home() / "Books"

EBOOK_EXTS = {"epub", "mobi", "azw", "azw3", "fb2", "djvu", "pdf", "cbz", "cbr", "lit", "lrf"}

DC_NS = "http://purl.org/dc/elements/1.1/"


def human(b):
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:7.1f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:7.1f} MB"
    if b >= 1024:
        return f"{b / 1024:7.1f} KB"
    return f"{b:7d}  B"


def bar(pct):
    return "█" * int(pct / 4)


def parse_epub(path):
    """Extract title, authors, language from EPUB OPF metadata."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            if "META-INF/container.xml" not in z.namelist():
                return {}
            container = z.read("META-INF/container.xml").decode("utf-8", errors="replace")
            m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', container, re.I)
            if not m:
                return {}
            opf_path = m.group(1)
            if opf_path not in z.namelist():
                return {}
            opf = z.read(opf_path).decode("utf-8", errors="replace")
            root = ET.fromstring(opf)

            def dc(tag):
                return [
                    el.text.strip()
                    for el in root.findall(f".//{{{DC_NS}}}{tag}")
                    if el.text and el.text.strip()
                ]

            titles    = dc("title")
            authors   = dc("creator")
            languages = dc("language")
            return {
                "title":    titles[0] if titles else None,
                "authors":  authors,
                "language": languages[0].lower() if languages else None,
            }
    except Exception:
        return {}


def parse_pdf(path):
    """Extract title, author, language, and approx page count from PDF."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if f.read(4) != b"%PDF":
                return {}
            f.seek(0)
            head = f.read(min(32768, size))
            f.seek(max(0, size - 32768))
            tail = f.read()
        text = (head + tail).decode("latin-1", errors="replace")

        def extract_str(key):
            m = re.search(r'/' + re.escape(key) + r'\s*\(([^)\\]*(?:\\.[^)\\]*)*)\)', text)
            return m.group(1).strip() if m else None

        title  = extract_str("Title")
        author = extract_str("Author")
        lang   = extract_str("Lang") or extract_str("Language")

        # Highest /Count value belongs to the page tree root
        counts = re.findall(r'/Count\s+(\d+)', text)
        pages  = max((int(c) for c in counts), default=None)

        meta = {}
        if title:  meta["title"]    = title
        if author: meta["authors"]  = [author]
        if lang:   meta["language"] = lang.lower()
        if pages:  meta["pages"]    = pages
        return meta
    except Exception:
        return {}


def parse_filename(path):
    """Guess author/title from the common `author - title.ext` convention."""
    stem = path.stem.replace("_", " ")
    # Strip common download-site suffixes
    stem = re.sub(r'[-_ ]+(libgen|annas?[\s_]archive|duplicate|anna).*$', "", stem, flags=re.I).strip()
    parts = re.split(r'\s*-\s*', stem, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip().title(), parts[1].strip().title()
    return None, stem.title()


def size_label(b):
    mb = b / 1_048_576
    if mb < 1:   return "< 1 MB"
    if mb < 5:   return "1–5 MB"
    if mb < 15:  return "5–15 MB"
    if mb < 50:  return "15–50 MB"
    if mb < 100: return "50–100 MB"
    return "100+ MB"


def page_label(n):
    if n < 100:  return "< 100 p"
    if n < 200:  return "100–200 p"
    if n < 400:  return "200–400 p"
    if n < 600:  return "400–600 p"
    if n < 1000: return "600–1000 p"
    return "1000+ p"


ext_count    = Counter()
ext_size     = {}
lang_count   = Counter()
size_count   = Counter()
page_count   = Counter()
author_count = Counter()
total_count  = 0
total_size   = 0
meta_ok      = 0

for root, dirs, files in os.walk(BASE_DIR):
    dirs.sort()
    for fname in files:
        fpath = Path(root) / fname
        ext = fpath.suffix.lstrip(".").lower() if fpath.suffix else "(none)"
        if ext not in EBOOK_EXTS:
            continue
        try:
            size = fpath.stat().st_size
        except OSError:
            continue

        total_count += 1
        total_size  += size
        ext_count[ext] += 1
        ext_size[ext] = ext_size.get(ext, 0) + size
        size_count[size_label(size)] += 1

        meta = {}
        if ext == "epub":
            meta = parse_epub(fpath)
        elif ext == "pdf":
            meta = parse_pdf(fpath)

        # Fall back to filename parsing for author when metadata is absent
        if not meta.get("authors"):
            fa, _ = parse_filename(fpath)
            if fa:
                meta.setdefault("authors", [fa])

        if meta:
            meta_ok += 1

        for author in meta.get("authors", []):
            if author:
                author_count[author] += 1

        lang = meta.get("language")
        if lang:
            lang_count[lang] += 1

        pages = meta.get("pages")
        if pages:
            page_count[page_label(pages)] += 1


if total_count == 0:
    print(f"\nNo ebook files found in {BASE_DIR}")
    raise SystemExit(0)

print(f"\n── Books: {BASE_DIR} ────────────────────────────────────────────────")
print(f"  {total_count} ebook(s)   {human(total_size)}")
if meta_ok:
    print(f"  Metadata available : {meta_ok}/{total_count}")

print("\n── By Format ────────────────────────────────────────────────────")
for ext, cnt in sorted(ext_count.items(), key=lambda x: -x[1]):
    size = ext_size[ext]
    pct  = cnt / total_count * 100
    print(f"  .{ext:<9}  {cnt:5d}  {human(size)}  {pct:5.1f}%  {bar(pct)}")

if lang_count:
    total_lang = sum(lang_count.values())
    print("\n── By Language ──────────────────────────────────────────────────")
    for lang, cnt in sorted(lang_count.items(), key=lambda x: -x[1]):
        pct = cnt / total_lang * 100
        print(f"  {lang:<18}  {cnt:5d}  {pct:5.1f}%  {bar(pct)}")

if author_count:
    print("\n── Authors ──────────────────────────────────────────────────────")
    for author, cnt in sorted(author_count.items(), key=lambda x: (-x[1], x[0]))[:30]:
        suffix = f"  ({cnt} books)" if cnt > 1 else ""
        print(f"  {author}{suffix}")

print("\n── By File Size ─────────────────────────────────────────────────")
SIZE_ORDER = ["< 1 MB", "1–5 MB", "5–15 MB", "15–50 MB", "50–100 MB", "100+ MB"]
for label in SIZE_ORDER:
    cnt = size_count.get(label, 0)
    if not cnt:
        continue
    pct = cnt / total_count * 100
    print(f"  {label:<14}  {cnt:5d}  {pct:5.1f}%  {bar(pct)}")

if page_count:
    total_pages = sum(page_count.values())
    PAGE_ORDER  = ["< 100 p", "100–200 p", "200–400 p", "400–600 p", "600–1000 p", "1000+ p"]
    print("\n── By Page Count (PDF) ──────────────────────────────────────────")
    for label in PAGE_ORDER:
        cnt = page_count.get(label, 0)
        if not cnt:
            continue
        pct = cnt / total_pages * 100
        print(f"  {label:<14}  {cnt:5d}  {pct:5.1f}%  {bar(pct)}")

print()
