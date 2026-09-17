#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GDRIVE_DEST="gdrive:audiofiles"
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

echo "── Collecting .opus and .mp3 files"

dupes=0
count=0
while IFS= read -r -d '' file; do
    name="$(basename "$file")"
    dest="$STAGING/$name"
    if [[ -e "$dest" ]]; then
        echo "[DUPE] '$name' already staged from another folder — skipping: $file"
        ((dupes++))
        continue
    fi
    ln -sf "$file" "$dest"
    ((count++))
done < <(find "$BASE_DIR" -type f \( -name "*.opus" -o -name "*.mp3" \) \
    -not -path "*/transcoded_downloads/*" -print0 | sort -z)

echo "── Staged $count files ($dupes duplicates skipped)"
[[ $dupes -gt 0 ]] && echo "    (duplicate = same filename found in multiple subfolders)"
echo ""
echo "── Uploading → $GDRIVE_DEST"

rclone copy "$STAGING" "$GDRIVE_DEST" \
    --copy-links \
    --progress \
    --transfers 4 \
    --checkers 8 \
    --retries 10 \
    --low-level-retries 20 \
    --timeout 5m \
    --contimeout 1m \
    --stats 5s

echo "── Done"
