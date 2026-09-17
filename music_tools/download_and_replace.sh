#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOAD_DIR="$BASE_DIR/transcoded_downloads"
GDRIVE_REMOTE="gdrive:transcoded"
DRY_RUN="${DRY_RUN:-0}"  # DRY_RUN=1 ./download_and_replace.sh to preview without changing anything

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { printf "${GREEN}[OK]${NC}   %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
fail() { printf "${RED}[FAIL]${NC} %s\n" "$*"; }
info() { printf "       %s\n" "$*"; }

get_duration() {
    ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null | head -1
}

get_resolution() {
    ffprobe -v error -select_streams v:0 \
        -show_entries stream=width,height \
        -of csv=p=0 "$1" 2>/dev/null | head -1
}

get_vcodec() {
    ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null | head -1
}

has_video() {
    local codec
    codec=$(get_vcodec "$1")
    [[ -n "$codec" ]]
}

dur_within_2s() {
    awk -v a="$1" -v b="$2" 'BEGIN { d = a - b; if (d < 0) d = -d; exit (d <= 2) ? 0 : 1 }'
}

# ─────────────────────────────────────────────────────────────────
# Step 1: Download from Google Drive
# ─────────────────────────────────────────────────────────────────

echo "── Step 1: Downloading $GDRIVE_REMOTE → $DOWNLOAD_DIR"
mkdir -p "$DOWNLOAD_DIR"

if [[ "$DRY_RUN" -eq 1 ]]; then
    info "[DRY RUN] skipping rclone download"
else
    rclone copy "$GDRIVE_REMOTE" "$DOWNLOAD_DIR" \
        --progress --transfers 4 --checkers 8 \
        --retries 10 --low-level-retries 20 \
        --timeout 5m --contimeout 1m --stats 5s
fi
echo ""

# ─────────────────────────────────────────────────────────────────
# Step 2: Spot-check each file, replace original if all checks pass
# ─────────────────────────────────────────────────────────────────

echo "── Step 2: Spot-checking and replacing originals"

pass=0; fail_count=0; skip=0

while IFS= read -r -d '' new_file; do
    rel="${new_file#"$DOWNLOAD_DIR"/}"
    stem="${rel%.*}"
    name="$(basename "$new_file")"
    echo ""
    echo "  ▸ $rel"

    # Check 1: file is not empty (min 1 MB — any real video will be larger)
    size=$(stat -c%s "$new_file" 2>/dev/null || echo 0)
    if [[ "$size" -lt 1048576 ]]; then
        fail "too small: ${size} bytes — $name"
        ((fail_count++)); continue
    fi

    # Check 2: ffprobe can find a video stream (catches broken containers)
    if ! has_video "$new_file"; then
        fail "no video stream or unreadable — $name"
        ((fail_count++)); continue
    fi

    # Check 3: codec is actually H265/HEVC (confirms transcoding ran)
    vcodec=$(get_vcodec "$new_file")
    if [[ "$vcodec" != "hevc" ]]; then
        fail "unexpected codec '$vcodec' (expected hevc) — $name"
        ((fail_count++)); continue
    fi

    # Locate the original file (may be .mkv, .mp4, etc.)
    orig_file=""
    for ext in mkv mp4 avi mov webm; do
        candidate="$BASE_DIR/$stem.$ext"
        [[ -f "$candidate" ]] && { orig_file="$candidate"; break; }
    done

    if [[ -z "$orig_file" ]]; then
        warn "no original found for '$rel' — already replaced or missing, skipping"
        ((skip++)); continue
    fi

    # Check 4: duration within 2 seconds of original
    new_dur=$(get_duration "$new_file")
    orig_dur=$(get_duration "$orig_file")

    if [[ -z "$new_dur" || -z "$orig_dur" ]]; then
        fail "could not read duration — $name"
        ((fail_count++)); continue
    fi

    if ! dur_within_2s "$new_dur" "$orig_dur"; then
        fail "duration mismatch: orig=${orig_dur}s  new=${new_dur}s — $name"
        ((fail_count++)); continue
    fi

    # Check 5: resolution unchanged
    new_res=$(get_resolution "$new_file")
    orig_res=$(get_resolution "$orig_file")

    if [[ -z "$new_res" || "$new_res" != "$orig_res" ]]; then
        fail "resolution mismatch: orig=$orig_res  new=$new_res — $name"
        ((fail_count++)); continue
    fi

    size_hr=$(numfmt --to=iec "$size")
    ok "codec=hevc  size=$size_hr  duration=${new_dur}s  resolution=${new_res}"

    # All checks passed — move transcoded file into place, delete original
    dest_dir="$(dirname "$orig_file")"
    dest="$dest_dir/$name"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "[DRY RUN] would mv  $new_file → $dest"
        info "[DRY RUN] would rm  $orig_file"
    else
        mv "$new_file" "$dest"
        [[ "$orig_file" != "$dest" ]] && rm "$orig_file"
        info "replaced $(basename "$orig_file") → $name"
    fi

    ((pass++))

done < <(find "$DOWNLOAD_DIR" -name "*.mp4" -print0 | sort -z)

echo ""
echo "── Summary ── passed: $pass  failed: $fail_count  skipped: $skip"

# Clean up the staging directory if everything succeeded
if [[ "$DRY_RUN" -eq 0 && "$fail_count" -eq 0 && "$skip" -eq 0 && "$pass" -gt 0 ]]; then
    remaining=$(find "$DOWNLOAD_DIR" -name "*.mp4" | wc -l)
    if [[ "$remaining" -eq 0 ]]; then
        rm -rf "$DOWNLOAD_DIR"
        echo "Removed empty $DOWNLOAD_DIR"
    fi
fi
