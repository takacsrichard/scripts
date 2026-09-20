#!/usr/bin/env python3

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path.home() / "Music"
JOBS = os.cpu_count()


def convert_flac_to_opus(flac: Path) -> str:
    opus = flac.with_suffix(".opus")
    name = flac.name

    if opus.exists():
        return f"[OPUS SKIP] {name}"

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(flac), "-c:a", "libopus", "-b:a", "128k", "-vn", str(opus), "-loglevel", "error"],
        capture_output=True,
    )

    if result.returncode == 0 and opus.exists() and opus.stat().st_size > 0:
        flac.unlink()
        return f"[OPUS DONE] {name}"

    opus.unlink(missing_ok=True)
    reason = "output missing or empty" if result.returncode == 0 else "ffmpeg error"
    return f"[OPUS FAIL] {reason}, keeping original: {name}"


def convert_wav_to_flac(wav: Path) -> str:
    flac = wav.with_suffix(".flac")
    name = wav.name

    if flac.exists():
        return f"[WAV SKIP] {name}"

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(wav), "-c:a", "flac", str(flac), "-loglevel", "error"],
        capture_output=True,
    )

    if result.returncode == 0 and flac.exists() and flac.stat().st_size > 0:
        wav.unlink()
        return f"[WAV DONE] {name}"

    flac.unlink(missing_ok=True)
    reason = "output missing or empty" if result.returncode == 0 else "ffmpeg error"
    return f"[WAV FAIL] {reason}, keeping original: {name}"


def run_parallel(files, fn, label):
    print(f"── {label} ── ({len(files)} files, {JOBS} parallel jobs)")
    with ThreadPoolExecutor(max_workers=JOBS) as executor:
        futures = {executor.submit(fn, f): f for f in files}
        for future in as_completed(futures):
            print(future.result())
    print()


# FLAC → Opus first so pre-existing FLACs get compressed to lossy Opus.
# WAV → FLAC runs after; those new FLACs are kept lossless (not re-encoded).
flac_files = sorted(BASE_DIR.rglob("*.flac"))
run_parallel(flac_files, convert_flac_to_opus, "FLAC → Opus")

wav_files = sorted(BASE_DIR.rglob("*.wav"))
run_parallel(wav_files, convert_wav_to_flac, "WAV → FLAC")

print("All done.")
