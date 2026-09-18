#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
DB="/home/richard/.config/mozilla/firefox/default/places.sqlite"
CRITERIA="$SCRIPT_DIR/deletion_criteria.txt"

die() { echo "Error: $*" >&2; exit 1; }

# Use system sqlite3 if available, otherwise pull from nixpkgs
if command -v sqlite3 &>/dev/null; then
    sql() { sqlite3 "$DB" <<< "$1"; }
else
    sql() { nix-shell -p sqlite --run "sqlite3 \"$DB\"" <<< "$1"; }
fi

[[ -f "$DB" ]]       || die "Firefox history DB not found: $DB"
[[ -f "$CRITERIA" ]] || die "Criteria file not found: $CRITERIA"

# Read keywords: strip surrounding *, whitespace; skip # comments and blank lines
mapfile -t KEYWORDS < <(
    grep -v '^\s*#' "$CRITERIA" |
    grep -v '^\s*$' |
    sed 's/^\*\+//;s/\*\+$//' |
    sed 's/^[[:space:]]*//;s/[[:space:]]*$//' |
    grep -v '^$'
)

[[ ${#KEYWORDS[@]} -gt 0 ]] || die "No keywords found in $CRITERIA"

# ── Firefox check ──────────────────────────────────────────────────────────────
if pgrep -x firefox &>/dev/null; then
    echo "Firefox is currently open. The history database is locked while it runs."
    read -rp "Kill Firefox? [Y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        kill "$(pgrep -o firefox)"
        echo "Waiting for Firefox to close..."
        sleep 2
    else
        echo "Aborted."
        exit 0
    fi
fi

# ── Build WHERE clause (SQLite LIKE is case-insensitive for ASCII by default) ──
# Match on title OR the URL base (before '?') — not the full URL with query params,
# because Google/etc embed base64 blobs in params that randomly contain keywords.
where=""
for kw in "${KEYWORDS[@]}"; do
    kw_esc="${kw//\'/\'\'}"
    url_base="SUBSTR(url, 1, INSTR(url || '?', '?') - 1)"
    part="(${url_base} LIKE '%${kw_esc}%' OR COALESCE(title,'') LIKE '%${kw_esc}%')"
    [[ -z "$where" ]] && where="$part" || where="$where OR $part"
done

# ── Preview ────────────────────────────────────────────────────────────────────
echo ""
echo "Scanning history..."
matches=$(sql "
    SELECT url, COALESCE(title,'(no title)')
    FROM moz_places
    WHERE visit_count > 0 AND ($where)
    ORDER BY last_visit_date DESC;
")

if [[ -z "$matches" ]]; then
    echo "No matching history entries found."
    exit 0
fi

echo ""
echo "I will delete these entries:"
echo "──────────────────────────────────────────────────────────────────────────"
count=0
while IFS='|' read -r url title; do
    printf "  [%s]\n  %s\n\n" "$title" "$url"
    count=$((count + 1))
done <<< "$matches"
echo "──────────────────────────────────────────────────────────────────────────"
echo "$count entries matched."
echo ""
read -rp "Proceed with deletion? [Y/N] " ans
if [[ ! "$ans" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ── Delete ────────────────────────────────────────────────────────────────────
sql "
BEGIN TRANSACTION;

-- Remove individual visit records for matched URLs
DELETE FROM moz_historyvisits
  WHERE place_id IN (SELECT id FROM moz_places WHERE $where);

-- Remove the place entry entirely if it's not also a bookmark
DELETE FROM moz_places
  WHERE ($where) AND foreign_count = 0;

-- For bookmarked pages, just zero out history metadata (keep the bookmark)
UPDATE moz_places
  SET visit_count = 0, last_visit_date = NULL
  WHERE ($where) AND foreign_count > 0;

COMMIT;
"

echo "Done. $count entries deleted."
