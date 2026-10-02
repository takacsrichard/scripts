#!/usr/bin/env bash
# Description:      Lists the top 30 biggest individual files across all snapshots,
#                   deduplicated by path, with excludes.conf filters applied.
# In restic natively: NO — restic stats only gives aggregate totals, not per-file rankings.
# Native equivalent: none
REPO="/run/media/richard/Expansion/backups/restic_repo"
source "$(dirname "$0")/excludes.conf"

for id in $(restic -r "$REPO" snapshots --json | jq -r '.[].id'); do
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
    "\(.size)\t\(.path)"
  ' \
| sort -t$'\t' -u -k2,2 \
| sort -rn \
| head -30 \
| awk -F'\t' '{printf "%10.2f MiB  %s\n", $1/1048576, $2}'
