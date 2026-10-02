#!/usr/bin/env python3
import re
import sys
import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".vob",
    ".ogv", ".rm", ".rmvb", ".divx", ".xvid", ".mxf", ".f4v",
}

def resolution_label(_width, height):
    h = height
    if h <= 240:  return "240p"
    if h <= 360:  return "360p"
    if h <= 480:  return "480p"
    if h <= 720:  return "720p"
    if h <= 1080: return "1080p"
    if h <= 1440: return "1440p"
    if h <= 2160: return "4K"
    return "8K+"

def duration_label(seconds):
    if seconds < 120:   return "< 2 min"
    if seconds < 300:   return "< 5 min"
    if seconds < 900:   return "< 15 min"
    if seconds < 3600:  return "< 1 hour"
    return "> 1 hour"

def bitrate_label(bps):
    mbps = bps / 1_000_000
    if mbps < 1:     return "< 1 Mbps"
    if mbps < 5:     return "1–5 Mbps"
    if mbps < 10:    return "5–10 Mbps"
    if mbps < 20:    return "10–20 Mbps"
    if mbps < 50:    return "20–50 Mbps"
    if mbps < 100:   return "50–100 Mbps"
    return "100+ Mbps"

def fps_label(fps):
    return f"{round(fps)}fps"

def probe(path):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return json.loads(result.stdout)
    except Exception:
        return None

def parse_fps(r_frame_rate):
    try:
        num, den = r_frame_rate.split("/")
        return float(num) / float(den)
    except Exception:
        return None

def ext_counts(videos):
    return Counter(f.suffix.lower() for f in videos)

def top_words(videos, n=5):
    STOPWORDS = {
        "the","a","an","and","or","of","in","to","for","is","it",
        "at","by","on","as","be","do","my","no","so","up","vs","s",
    }
    word_in_file = Counter()
    for f in videos:
        stem = f.stem.lower()
        words = {w for w in re.split(r'[^a-z0-9]+', stem) if len(w) >= 2} - STOPWORDS
        for w in words:
            word_in_file[w] += 1
    return word_in_file.most_common(n)

def print_table(title, counter, total):
    print(f"\n{'─' * 44}")
    print(f"  {title}")
    print(f"{'─' * 44}")
    print(f"  {'Value':<26} {'Count':>5}  {'%':>6}")
    print(f"  {'─'*26} {'─'*5}  {'─'*6}")
    for label, count in sorted(counter.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"  {label:<26} {count:>5}  {pct:>5.1f}%")
    print(f"{'─' * 44}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze.py <path>")
        sys.exit(1)

    path = Path(sys.argv[1]).expanduser().resolve()
    if not path.is_dir():
        print(f"Not a directory: {path}")
        sys.exit(1)

    videos = [
        f for f in path.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not videos:
        print(f"No video files found in {path}")
        sys.exit(0)

    workers = min(8, len(videos))
    print(f"\nScanning {len(videos)} video(s) in {path} (parallel workers: {workers}) ...")

    res_counter  = Counter()
    fps_counter  = Counter()
    bits_counter = Counter()
    dur_counter  = Counter()
    failed = 0
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, f): f for f in videos}
        for future in as_completed(futures):
            done += 1
            print(f"  [{done}/{len(videos)}] {futures[future].name}", end="\r", flush=True)

            data = future.result()
            if not data:
                failed += 1
                continue

            video_stream = next(
                (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
                None,
            )
            fmt = data.get("format", {})

            if not video_stream:
                failed += 1
                continue

            w = video_stream.get("width")
            h = video_stream.get("height")
            if w and h:
                res_counter[resolution_label(w, h)] += 1

            rfr = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
            if rfr:
                fps = parse_fps(rfr)
                if fps and fps > 0:
                    fps_counter[fps_label(fps)] += 1

            br = (
                video_stream.get("bit_rate")
                or fmt.get("bit_rate")
            )
            if br:
                try:
                    bits_counter[bitrate_label(int(br))] += 1
                except ValueError:
                    pass

            dur = fmt.get("duration")
            if dur:
                try:
                    dur_counter[duration_label(float(dur))] += 1
                except ValueError:
                    pass

    print()  # newline after progress line
    analyzed = len(videos) - failed
    print(f"Successfully analyzed: {analyzed}/{len(videos)}")
    if failed:
        print(f"  ({failed} file(s) could not be read — is ffprobe installed?)")

    if not analyzed:
        sys.exit(0)

    print_table("RESOLUTION",     res_counter,        analyzed)
    print_table("FRAME RATE",     fps_counter,        analyzed)
    print_table("BITRATE",        bits_counter,       analyzed)
    print_table("DURATION",       dur_counter,        analyzed)
    print_table("FILE EXTENSION", ext_counts(videos), len(videos))

    total = len(videos)
    words = top_words(videos)
    print(f"\n{'─' * 44}")
    print(f"  TOP 5 WORDS IN FILENAMES")
    print(f"{'─' * 44}")
    print(f"  {'Word':<26} {'Files':>5}  {'%':>6}")
    print(f"  {'─'*26} {'─'*5}  {'─'*6}")
    for word, count in words:
        pct = count / total * 100
        print(f"  {word:<26} {count:>5}  {pct:>5.1f}%")
    print(f"{'─' * 44}")
    print()

if __name__ == "__main__":
    main()
