#!/usr/bin/env python3

import os
from pathlib import Path

BASE_DIR = Path.home() / "Music"

AUDIO_EXTS = {"mp3", "flac", "opus", "wav", "aac", "ogg", "m4a", "wma", "ape", "wv", "alac"}
VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "webm", "m4v", "wmv", "flv", "ts", "mts"}


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


def sorted_desc(d):
    return sorted(d.items(), key=lambda x: x[1], reverse=True)


ext_size = {}
folder_size = {}
type_size = {"audio": 0, "video": 0, "other": 0}
total = 0

for root, dirs, files in os.walk(BASE_DIR):
    dirs[:] = [d for d in dirs if d != "transcoded_downloads"]
    for fname in files:
        fpath = Path(root) / fname
        try:
            size = fpath.stat().st_size
        except OSError:
            continue

        total += size

        ext = fpath.suffix.lstrip(".").lower() if fpath.suffix else "(none)"
        ext_size[ext] = ext_size.get(ext, 0) + size

        rel_parts = fpath.relative_to(BASE_DIR).parts
        folder = rel_parts[0] if len(rel_parts) > 1 else "(root)"
        folder_size[folder] = folder_size.get(folder, 0) + size

        if ext in AUDIO_EXTS:
            type_size["audio"] += size
        elif ext in VIDEO_EXTS:
            type_size["video"] += size
        else:
            type_size["other"] += size

print(f"\n── Total: {human(total)} ───────────────────────────────────────────────────")

print("\n── By Extension ─────────────────────────────────────────────────")
for ext, size in sorted_desc(ext_size):
    pct = (size / total) * 100 if total else 0
    print(f"  .{ext:<9}  {human(size)}  {pct:5.1f}%  {bar(pct)}")

print("\n── By Type ──────────────────────────────────────────────────────")
for t, size in sorted_desc(type_size):
    pct = (size / total) * 100 if total else 0
    print(f"  {t:<10}  {human(size)}  {pct:5.1f}%  {bar(pct)}")

print("\n── By Folder ────────────────────────────────────────────────────")
for f, size in sorted_desc(folder_size):
    pct = (size / total) * 100 if total else 0
    print(f"  {f:<20}  {human(size)}  {pct:5.1f}%  {bar(pct)}")

print()
