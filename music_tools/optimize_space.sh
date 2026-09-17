#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOBS=$(nproc)

# ─────────────────────────────────────────────────────────────────
# 1. FLAC → Opus 128k  (parallel — opus encoder is single-threaded
#                        per file so parallelism helps a lot here)
# ─────────────────────────────────────────────────────────────────

convert_flac_to_opus() {
    flac="$1"
    opus="${flac%.flac}.opus"
    name="$(basename "$flac")"

    if [[ -f "$opus" ]]; then
        echo "[OPUS SKIP] $name"
        return 0
    fi

    if ffmpeg -nostdin -i "$flac" -c:a libopus -b:a 128k -vn "$opus" -loglevel error; then
        if [[ -f "$opus" && -s "$opus" ]]; then
            rm "$flac"
            echo "[OPUS DONE] $name"
        else
            rm -f "$opus"
            echo "[OPUS FAIL] output missing or empty, keeping original: $name"
            return 1
        fi
    else
        rm -f "$opus"
        echo "[OPUS FAIL] ffmpeg error, keeping original: $name"
        return 1
    fi
}
export -f convert_flac_to_opus

flac_count=$(find "$BASE_DIR" -name "*.flac" | wc -l)
echo "── FLAC → Opus ── ($flac_count files, $JOBS parallel jobs)"
find "$BASE_DIR" -name "*.flac" -print0 \
    | sort -z \
    | xargs -0 -P "$JOBS" -I{} bash -c 'convert_flac_to_opus "$@"' _ {}
echo ""

# ─────────────────────────────────────────────────────────────────
# 2. WAV → FLAC  (lossless compression — preserves full quality)
#    Runs after FLAC→Opus so these stay as FLAC, not Opus.
# ─────────────────────────────────────────────────────────────────

convert_wav_to_flac() {
    wav="$1"
    flac="${wav%.wav}.flac"
    name="$(basename "$wav")"

    if [[ -f "$flac" ]]; then
        echo "[WAV SKIP] $name"
        return 0
    fi

    if ffmpeg -nostdin -i "$wav" -c:a flac "$flac" -loglevel error; then
        if [[ -f "$flac" && -s "$flac" ]]; then
            rm "$wav"
            echo "[WAV DONE] $name"
        else
            rm -f "$flac"
            echo "[WAV FAIL] output missing or empty, keeping original: $name"
            return 1
        fi
    else
        rm -f "$flac"
        echo "[WAV FAIL] ffmpeg error, keeping original: $name"
        return 1
    fi
}
export -f convert_wav_to_flac

wav_count=$(find "$BASE_DIR" -name "*.wav" | wc -l)
echo "── WAV → FLAC ── ($wav_count files, $JOBS parallel jobs)"
find "$BASE_DIR" -name "*.wav" -print0 \
    | sort -z \
    | xargs -0 -P "$JOBS" -I{} bash -c 'convert_wav_to_flac "$@"' _ {}
echo ""

echo "All done."
