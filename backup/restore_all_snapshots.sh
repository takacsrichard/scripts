#!/usr/bin/env bash
# Description:      Restores all snapshots oldest→newest so the latest version of each file wins.
#                   Useful when files were deleted in later snapshots but existed in earlier ones.
#                   Applies excludes.conf filters. Repo is never modified.
#                   Restored files land in TARGET with original paths preserved:
#                     e.g. /home/richard/Documents/foo.pdf → TARGET/home/richard/Documents/foo.pdf
# In restic natively: PARTIALLY — for just the latest snapshot, native is sufficient.
#                     The multi-snapshot oldest→newest merge is not natively supported.
# Native equivalent: restic restore latest --target <TARGET> -r <REPO>

REPO="/run/media/richard/Expansion/backups/restic_repo"
TARGET="/run/media/richard/Expansion/backups/restored"
source "$(dirname "$0")/excludes.conf"

OVERWRITE="${1:-if-newer}"   # usage: ./restic_restore_all.sh [always|if-newer|never]

mkdir -p "$TARGET"

# Sort snapshot IDs oldest→newest so later restores overwrite with fresher files
SNAPSHOT_IDS=$(restic -r "$REPO" snapshots --json \
  | jq -r 'sort_by(.time) | .[].short_id')

TOTAL=$(echo "$SNAPSHOT_IDS" | wc -l)
N=0

for id in $SNAPSHOT_IDS; do
  N=$((N + 1))
  echo "==> [$N/$TOTAL] Restoring snapshot $id ..." >&2
  restic -r "$REPO" restore "$id" \
    --target "$TARGET" \
    --overwrite "$OVERWRITE" \
    "${RESTIC_EXCLUDE_FLAGS[@]}"
done

echo ""
echo "Done. Files restored to: $TARGET"
echo "Restic repo at $REPO is untouched."
