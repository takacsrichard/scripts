#!/usr/bin/env bash
# Description:      Counts unique files and total size across all snapshots, with excludes.conf applied.
# In restic natively: PARTIALLY — restic stats gives similar numbers but cannot apply custom exclude patterns.
# Native equivalent: restic stats --mode files-by-contents -r <REPO>
REPO="/run/media/richard/Expansion/backups/restic_repo"
source "$(dirname "$0")/excludes.conf"

for id in $(restic -r "$REPO" snapshots --json | jq -r '.[].id'); do
  echo "Scanning $id..." >&2
  restic -r "$REPO" ls --json "$id" 2>/dev/null
done \
| grep '^{' \
| jq -r --arg excl "$JQ_EXCL_CASE" --arg iexcl "$JQ_EXCL_ICASE" '
    select(.type == "file") |
    select((
      (.path | test($excl))
      or
      (.path | test($iexcl; "i"))
      or
      (.path | split("/") | any(startswith(".")))
    ) | not) |
    "\(.path)\t\(.size)"
  ' \
| sort -t$'\t' -u -k1,1 \
| awk -F'\t' '{sum+=$2; count++} END {printf "%d unique files, %.2f GiB\n", count, sum/1073741824}'
