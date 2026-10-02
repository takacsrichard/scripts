#!/usr/bin/env python3
import os
import struct
from pathlib import Path
from collections import Counter

BASE_DIR = Path.home() / "Pictures"

IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp",
    "tiff", "tif", "avif", "heic", "heif", "ico",
}


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


def read_dimensions(path):
    """Return (w, h) parsed from file header, or None. No external deps."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(30)

        # PNG: 8-byte magic, IHDR chunk starts at byte 8 (width/height at 16/20)
        if hdr[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", hdr[16:24])

        # GIF: GIF87a / GIF89a — 16-bit LE width/height at offset 6
        if hdr[:3] == b"GIF":
            return struct.unpack("<HH", hdr[6:10])

        # BMP
        if hdr[:2] == b"BM":
            w = struct.unpack("<i", hdr[18:22])[0]
            h = struct.unpack("<i", hdr[22:26])[0]
            return abs(w), abs(h)

        # WebP: RIFF????WEBP
        if hdr[:4] == b"RIFF" and hdr[8:12] == b"WEBP":
            chunk = hdr[12:16]
            with open(path, "rb") as f:
                if chunk == b"VP8 ":
                    # offset 20: bitstream frame_tag (3 bytes), then start_code + w + h
                    f.seek(23)
                    if f.read(3) == b"\x9d\x01\x2a":
                        raw = f.read(4)
                        return struct.unpack("<H", raw[:2])[0] & 0x3FFF, struct.unpack("<H", raw[2:])[0] & 0x3FFF
                elif chunk == b"VP8L":
                    # offset 20: signature 0x2F (1 byte), then packed w-1/h-1 as 14-bit fields
                    f.seek(21)
                    raw = struct.unpack("<I", f.read(4))[0]
                    return (raw & 0x3FFF) + 1, ((raw >> 14) & 0x3FFF) + 1
                elif chunk == b"VP8X":
                    # offset 20: flags (4 bytes), then canvas w-1 (3 bytes LE), h-1 (3 bytes LE)
                    f.seek(24)
                    data = f.read(6)
                    return int.from_bytes(data[0:3], "little") + 1, int.from_bytes(data[3:6], "little") + 1

        # JPEG: scan for SOF marker
        if hdr[:2] == b"\xff\xd8":
            with open(path, "rb") as f:
                f.seek(2)
                for _ in range(512):
                    byte = f.read(1)
                    if byte != b"\xff":
                        break
                    while byte == b"\xff":
                        byte = f.read(1)
                    if not byte:
                        break
                    m = byte[0]
                    if m in (0xD8, 0xD9) or (0xD0 <= m <= 0xD7):
                        continue
                    seg_len = f.read(2)
                    if len(seg_len) < 2:
                        break
                    length = struct.unpack(">H", seg_len)[0]
                    if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        if length >= 7:
                            f.read(1)  # precision
                            h, w = struct.unpack(">HH", f.read(4))
                            return w, h
                    f.seek(length - 2, 1)

    except Exception:
        pass
    return None


def res_label(w, h):
    longer = max(w, h)
    if longer >= 7680: return "8K+"
    if longer >= 3840: return "4K  (3840+)"
    if longer >= 2560: return "1440p (2560+)"
    if longer >= 1920: return "1080p (1920+)"
    if longer >= 1280: return "720p (1280+)"
    if longer >= 640:  return "SD  (640+)"
    return "< 640px"


def aspect_label(w, h):
    if h == 0:
        return "unknown"
    r = w / h
    if abs(r - 1.0) < 0.05:        return "1:1  square"
    if r >= 2.35:                   return "ultrawide (≥2.35)"
    if r > 1.0:
        if abs(r - 16 / 9)  < 0.08: return "16:9  landscape"
        if abs(r - 16 / 10) < 0.07: return "16:10 landscape"
        if abs(r - 3 / 2)   < 0.07: return "3:2   landscape"
        if abs(r - 4 / 3)   < 0.06: return "4:3   landscape"
        return "other landscape"
    if abs(r - 9 / 16) < 0.08:     return "9:16  portrait"
    if abs(r - 3 / 4)  < 0.07:     return "3:4   portrait"
    return "other portrait"


def mp_label(w, h):
    mp = w * h / 1_000_000
    if mp < 0.5:  return "< 0.5 MP"
    if mp < 1:    return "0.5–1 MP"
    if mp < 2:    return "1–2 MP"
    if mp < 4:    return "2–4 MP"
    if mp < 8:    return "4–8 MP"
    if mp < 16:   return "8–16 MP"
    if mp < 32:   return "16–32 MP"
    return "32+ MP"


ext_count    = Counter()
ext_size     = {}
folder_count = Counter()
folder_size  = {}
res_count    = Counter()
aspect_count = Counter()
mp_count     = Counter()
total_size   = 0
total_count  = 0
dims_ok      = 0

for root, dirs, files in os.walk(BASE_DIR):
    dirs.sort()
    for fname in files:
        fpath = Path(root) / fname
        ext = fpath.suffix.lstrip(".").lower() if fpath.suffix else "(none)"
        if ext not in IMAGE_EXTS:
            continue
        try:
            size = fpath.stat().st_size
        except OSError:
            continue

        total_count += 1
        total_size  += size
        ext_count[ext] += 1
        ext_size[ext] = ext_size.get(ext, 0) + size

        rel = fpath.relative_to(BASE_DIR).parts
        folder = rel[0] if len(rel) > 1 else "(root)"
        folder_count[folder] += 1
        folder_size[folder] = folder_size.get(folder, 0) + size

        if ext in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}:
            dims = read_dimensions(fpath)
            if dims:
                w, h = dims
                dims_ok += 1
                res_count[res_label(w, h)] += 1
                aspect_count[aspect_label(w, h)] += 1
                mp_count[mp_label(w, h)] += 1

if total_count == 0:
    print(f"\nNo image files found in {BASE_DIR}")
    raise SystemExit(0)

print(f"\n── Pictures: {BASE_DIR} ──────────────────────────────────────────────")
print(f"  {total_count} image(s)   {human(total_size)}")
if dims_ok:
    print(f"  Dimensions parsed : {dims_ok}/{total_count}")

print("\n── By Extension ─────────────────────────────────────────────────")
for ext, cnt in sorted(ext_count.items(), key=lambda x: -x[1]):
    size = ext_size[ext]
    pct = cnt / total_count * 100
    print(f"  .{ext:<9}  {cnt:5d}  {human(size)}  {pct:5.1f}%  {bar(pct)}")

print("\n── By Folder ────────────────────────────────────────────────────")
for folder, cnt in sorted(folder_count.items(), key=lambda x: -x[1]):
    size = folder_size[folder]
    pct = cnt / total_count * 100
    print(f"  {folder:<22}  {cnt:5d}  {human(size)}  {pct:5.1f}%  {bar(pct)}")

if res_count:
    RES_ORDER = ["8K+", "4K  (3840+)", "1440p (2560+)", "1080p (1920+)", "720p (1280+)", "SD  (640+)", "< 640px"]
    print("\n── By Resolution ────────────────────────────────────────────────")
    for label in RES_ORDER:
        cnt = res_count.get(label, 0)
        if not cnt:
            continue
        pct = cnt / dims_ok * 100
        print(f"  {label:<18}  {cnt:5d}  {pct:5.1f}%  {bar(pct)}")

if aspect_count:
    print("\n── By Aspect Ratio ──────────────────────────────────────────────")
    for label, cnt in sorted(aspect_count.items(), key=lambda x: -x[1]):
        pct = cnt / dims_ok * 100
        print(f"  {label:<22}  {cnt:5d}  {pct:5.1f}%  {bar(pct)}")

if mp_count:
    MP_ORDER = ["< 0.5 MP", "0.5–1 MP", "1–2 MP", "2–4 MP", "4–8 MP", "8–16 MP", "16–32 MP", "32+ MP"]
    print("\n── By Megapixels ────────────────────────────────────────────────")
    for label in MP_ORDER:
        cnt = mp_count.get(label, 0)
        if not cnt:
            continue
        pct = cnt / dims_ok * 100
        print(f"  {label:<12}  {cnt:5d}  {pct:5.1f}%  {bar(pct)}")

print()
