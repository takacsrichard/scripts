#!/usr/bin/env bash
# Description:      Groups all backed-up files by their immediate parent directory,
#                   showing file count and total size per folder (top 40), with excludes.conf applied.
# In restic natively: NO — restic has no per-folder grouping or breakdown command.
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
| awk -F'\t' '{
    path = $2
    n = split(path, p, "/")
    dir = ""
    for (i = 2; i < n; i++) dir = dir "/" p[i]
    size[dir] += $1
    count[dir]++
  } END {
    for (d in size)
      printf "%5d files  %10.2f MiB  %s\n", count[d], size[d]/1048576, d
  }' \
| sort -rn \
| head -40
