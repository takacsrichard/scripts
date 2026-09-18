#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
FF="/home/richard/.config/mozilla/firefox/default"
CRITERIA="$SCRIPT_DIR/deletion_criteria.txt"

die() { echo "Error: $*" >&2; exit 1; }

if command -v sqlite3 &>/dev/null; then
    sql() { sqlite3 "$1" <<< "$2"; }
else
    sql() { nix-shell -p sqlite --run "sqlite3 \"$1\"" <<< "$2"; }
fi

[[ -d "$FF" ]]       || die "Firefox profile not found: $FF"
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
    echo "Firefox is currently open. It must be closed to edit its files."
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

# ── Build WHERE clauses ────────────────────────────────────────────────────────

# URL-base match (strips query string — avoids base64 false positives in params)
_url_base_cond() {
    local col="$1" out=""
    for kw in "${KEYWORDS[@]}"; do
        local esc="${kw//\'/\'\'}"
        local base="SUBSTR(${col}, 1, INSTR(${col} || '?', '?') - 1)"
        local part="(${base} LIKE '%${esc}%')"
        [[ -z "$out" ]] && out="$part" || out="$out OR $part"
    done
    echo "$out"
}

# Title / free-text match
_text_cond() {
    local col="$1" out=""
    for kw in "${KEYWORDS[@]}"; do
        local esc="${kw//\'/\'\'}"
        local part="(COALESCE(${col},'') LIKE '%${esc}%')"
        [[ -z "$out" ]] && out="$part" || out="$out OR $part"
    done
    echo "$out"
}

# Host match (for cookies — domain only, no path)
_host_cond() {
    local out=""
    for kw in "${KEYWORDS[@]}"; do
        local esc="${kw//\'/\'\'}"
        local part="(host LIKE '%${esc}%')"
        [[ -z "$out" ]] && out="$part" || out="$out OR $part"
    done
    echo "$out"
}

places_where="($(_url_base_cond url) OR $(_text_cond title))"
favicon_where="$(_url_base_cond page_url)"
host_where="$(_host_cond)"
form_where="$(_text_cond value)"
KW_GREP=$(printf '%s\n' "${KEYWORDS[@]}" | paste -sd '|')

# ── Per-section helper ─────────────────────────────────────────────────────────
ask() {
    read -rp "${1:-Delete the above?} [Y/N] " _ans
    [[ "$_ans" =~ ^[Yy]$ ]]
}

divider() { printf '\n━━━ %s ━━━\n' "$*"; }

echo ""
echo "Profile : $FF"
echo "Keywords: ${KEYWORDS[*]}"

# ══════════════════════════════════════════════════════════════════════════════
divider "1/7  places.sqlite — browsing history"
# ══════════════════════════════════════════════════════════════════════════════
DB_PLACES="$FF/places.sqlite"
matches=$(sql "$DB_PLACES" "
    SELECT url, COALESCE(title,'(no title)')
    FROM moz_places
    WHERE visit_count > 0 AND $places_where
    ORDER BY last_visit_date DESC;
")

if [[ -z "$matches" ]]; then
    echo "  Nothing found. ✓"
else
    count=0
    while IFS='|' read -r url title; do
        printf "  [%s]\n  %s\n\n" "$title" "$url"
        count=$((count + 1))
    done <<< "$matches"
    echo "  $count entries."
    if ask; then
        sql "$DB_PLACES" "
        BEGIN TRANSACTION;
        DELETE FROM moz_historyvisits
          WHERE place_id IN (SELECT id FROM moz_places WHERE $places_where);
        DELETE FROM moz_places
          WHERE $places_where AND foreign_count = 0;
        UPDATE moz_places SET visit_count = 0, last_visit_date = NULL
          WHERE $places_where AND foreign_count > 0;
        COMMIT;"
        echo "  Deleted."
    else
        echo "  Skipped."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
divider "2/7  favicons.sqlite — cached page favicon mappings"
# ══════════════════════════════════════════════════════════════════════════════
DB_FAV="$FF/favicons.sqlite"
fav_matches=$(sql "$DB_FAV" "
    SELECT page_url FROM moz_pages_w_icons
    WHERE $favicon_where
    ORDER BY page_url;
")

if [[ -z "$fav_matches" ]]; then
    echo "  Nothing found. ✓"
else
    count=0
    while IFS= read -r url; do
        printf "  %s\n" "$url"
        count=$((count + 1))
    done <<< "$fav_matches"
    echo "  $count entries."
    if ask; then
        sql "$DB_FAV" "
        BEGIN TRANSACTION;
        DELETE FROM moz_icons_to_pages
          WHERE page_id IN (SELECT id FROM moz_pages_w_icons WHERE $favicon_where);
        DELETE FROM moz_pages_w_icons WHERE $favicon_where;
        DELETE FROM moz_icons
          WHERE id NOT IN (SELECT icon_id FROM moz_icons_to_pages);
        COMMIT;"
        echo "  Deleted."
    else
        echo "  Skipped."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
divider "3/7  cookies.sqlite — stored cookies"
# ══════════════════════════════════════════════════════════════════════════════
DB_CK="$FF/cookies.sqlite"
cookie_matches=$(sql "$DB_CK" "
    SELECT host, name FROM moz_cookies
    WHERE $host_where
    ORDER BY host, name;
")

if [[ -z "$cookie_matches" ]]; then
    echo "  Nothing found. ✓"
else
    count=0
    while IFS='|' read -r host name; do
        printf "  %-35s  %s\n" "$host" "$name"
        count=$((count + 1))
    done <<< "$cookie_matches"
    echo "  $count cookies."
    if ask; then
        sql "$DB_CK" "DELETE FROM moz_cookies WHERE $host_where;"
        echo "  Deleted."
    else
        echo "  Skipped."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
divider "4/7  formhistory.sqlite — address bar / search history"
# ══════════════════════════════════════════════════════════════════════════════
DB_FORM="$FF/formhistory.sqlite"
form_matches=$(sql "$DB_FORM" "
    SELECT fieldname, value FROM moz_formhistory
    WHERE $form_where
    ORDER BY fieldname, value;
")

if [[ -z "$form_matches" ]]; then
    echo "  Nothing found. ✓"
else
    count=0
    while IFS='|' read -r field value; do
        printf "  [%-20s]  %s\n" "$field" "$value"
        count=$((count + 1))
    done <<< "$form_matches"
    echo "  $count entries."
    if ask; then
        sql "$DB_FORM" "DELETE FROM moz_formhistory WHERE $form_where;"
        echo "  Deleted."
    else
        echo "  Skipped."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
divider "5/7  storage/ — localStorage / IndexedDB directories"
# ══════════════════════════════════════════════════════════════════════════════
storage_dirs=()
while IFS= read -r dir; do
    [[ -n "$dir" ]] && storage_dirs+=("$dir")
done < <(find "$FF/storage/default" -maxdepth 1 -mindepth 1 -type d 2>/dev/null \
         | grep -iE "$KW_GREP" || true)

if [[ ${#storage_dirs[@]} -eq 0 ]]; then
    echo "  Nothing found. ✓"
else
    for d in "${storage_dirs[@]}"; do
        printf "  %s\n" "$d"
    done
    echo "  ${#storage_dirs[@]} directories."
    if ask; then
        for d in "${storage_dirs[@]}"; do
            rm -rf "$d"
        done
        echo "  Deleted."
    else
        echo "  Skipped."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
divider "6/7  sessionstore-backups/ — cached tab/session data"
# ══════════════════════════════════════════════════════════════════════════════
session_files=()
for f in "$FF/sessionstore-backups/"*.jsonlz4; do
    [[ -f "$f" ]] || continue
    if strings "$f" 2>/dev/null | grep -qiE "$KW_GREP"; then
        session_files+=("$f")
    fi
done

if [[ ${#session_files[@]} -eq 0 ]]; then
    echo "  Nothing found. ✓"
else
    for f in "${session_files[@]}"; do
        printf "  %s\n" "$f"
        strings "$f" 2>/dev/null | grep -iE "$KW_GREP" | head -5 | while IFS= read -r line; do
            printf "    → %s\n" "$line"
        done
    done
    echo ""
    echo "  ${#session_files[@]} file(s) contain matches."
    echo "  (Safe to delete — Firefox rebuilds these on next launch, you just lose tab restore.)"
    if ask; then
        for f in "${session_files[@]}"; do
            rm "$f"
        done
        echo "  Deleted."
    else
        echo "  Skipped."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
divider "7/7  places.sqlite VACUUM — purge WAL remnants"
# ══════════════════════════════════════════════════════════════════════════════
wal="$FF/places.sqlite-wal"
if [[ -f "$wal" ]] && strings "$wal" 2>/dev/null | grep -qiE "$KW_GREP"; then
    echo "  WAL file contains keyword remnants left over from the delete transaction."
    strings "$wal" 2>/dev/null | grep -iE "$KW_GREP" | head -10 | while IFS= read -r line; do
        printf "  → %s\n" "$line"
    done
    echo ""
    echo "  VACUUM rewrites places.sqlite from scratch, eliminating the WAL file."
    if ask "Run VACUUM on places.sqlite?"; then
        sql "$DB_PLACES" "VACUUM;"
        echo "  Done."
    else
        echo "  Skipped."
    fi
else
    echo "  WAL is clean. ✓"
fi

echo ""
echo "All done."
