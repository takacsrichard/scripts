#!/usr/bin/env bash
# Description:      Searches for files matching a keyword across all snapshots, with excludes.conf applied.
#                   Supports case-insensitive search via -i flag. Shows file sizes.
# In restic natively: YES — restic find covers this almost entirely. Only difference is
#                     native find cannot apply the custom excludes.conf patterns.
# Native equivalent: restic find "*keyword*" -r <REPO>
#                    restic find --ignore-case "*keyword*" -r <REPO>   (case-insensitive)
# Usage: ./search_snapshots.sh <keyword>
#        ./search_snapshots.sh -i <keyword>   (case-insensitive)

REPO="/run/media/richard/Expansion/backups/restic_repo"
source "$(dirname "$0")/excludes.conf"

CASE_FLAG=""
if [[ "$1" == "-i" ]]; then
  CASE_FLAG="-i"
  shift
fi

if [[ -z "$1" ]]; then
  echo "Usage: $0 [-i] <keyword>" >&2
  exit 1
fi

KEYWORD="$1"

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
| grep $CASE_FLAG -- "$KEYWORD" \
| awk -F'\t' '{printf "%10.2f MiB  %s\n", $1/1048576, $2}' \
| sort -rn
