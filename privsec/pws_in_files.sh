#!/usr/bin/env bash
#
# scan the home folder for plaintext copies of every rbw pw
# hard dependency: jq, rg, rbw
# soft dependency: pdftotext (extract text from PDFs to scan)
# soft dependency: 7z (7z/7za/7zr) + qpdf (test leaked pws against
#                  password-protected zip/7z/rar archives + encrypted PDFs)
# Env overrides:
#   MAX_PDF_MB     max PDF size (MB) to text-extract, default 25
#   PDF_TIMEOUT    per-PDF extraction timeout (seconds), default 10
#   MAX_TEXT_SIZE  max size for plain-text files, default 200M (rg syntax)
#   MIN_PW_LEN     skip vault passwords shorter than this, default 6
#                  (short passwords/PINs produce mostly noise matches)
#   NO_SYNC        set to 1 to skip "rbw sync" before reading the vault
#   SHOW_FULL      set to 1 to print full plaintext passwords instead of
#                  masked (first+last char only)
#   CHECK_ARCHIVES set to 0 to skip testing leaked passwords against
#                  password-protected zip/7z/rar archives and encrypted
#                  PDFs found on disk (default 1; needs 7z / qpdf)
#   OFFER_REMOVE   set to 0 to skip the interactive "delete this file?"
#                  prompt shown after results (default 1; auto-skipped
#                  when not running in a terminal). Per-file answer is
#                  y/N/a/x  (a = yes to ALL remaining, x = keep ALL remaining)
#
# Claude CLI (~/.claude) and Gemini CLI (~/.gemini) local history/session
# directories are always included in the scan in addition to ROOT, since
# passwords pasted into an agent session are a common leak vector. Only
# locally-stored history is scanned; nothing is fetched from any cloud
# account tied to those tools.
#
# In addition to plain-text matches, each vault password is also searched
# for as a base64-encoded copy and as a character-reversed copy, since both
# are common ways a password ends up "hidden in plain sight" in a file.
#
# Output format:
#   PATH -> MASKED_PASSWORD[ [base64|reversed]]  (vault entry: name1, name2)

set -uo pipefail

ROOT="${1:-$HOME}"
JOBS="$(nproc)"
MAX_PDF_MB="${MAX_PDF_MB:-50}"
MAX_PDF_BYTES=$((MAX_PDF_MB * 1024 * 1024))
PDF_TIMEOUT="${PDF_TIMEOUT:-10}"
MAX_TEXT_SIZE="${MAX_TEXT_SIZE:-200M}"
MIN_PW_LEN="${MIN_PW_LEN:-6}"
NO_SYNC="${NO_SYNC:-0}"
SHOW_FULL="${SHOW_FULL:-0}"
CHECK_ARCHIVES="${CHECK_ARCHIVES:-1}"
OFFER_REMOVE="${OFFER_REMOVE:-1}"

for bin in rbw jq rg; do
  command -v "$bin" >/dev/null 2>&1 || { echo "$bin is required but not found in PATH" >&2; exit 1; }
done

HAVE_PDFTOTEXT=0
command -v pdftotext >/dev/null 2>&1 && HAVE_PDFTOTEXT=1

HAVE_7Z=0
SEVENZ_BIN=""
for b in 7z 7zz 7za 7zr; do
  if command -v "$b" >/dev/null 2>&1; then
    HAVE_7Z=1
    SEVENZ_BIN="$b"
    break
  fi
done

HAVE_QPDF=0
command -v qpdf >/dev/null 2>&1 && HAVE_QPDF=1

HAVE_BASE64=0
command -v base64 >/dev/null 2>&1 && HAVE_BASE64=1

HAVE_REV=0
command -v rev >/dev/null 2>&1 && HAVE_REV=1

# TODO change plaintext files into keeping them in memory
WORKDIR="$(mktemp -d)"
chmod 700 "$WORKDIR" #TODO is it temporary or permanent?
cleanup() {
  if command -v shred >/dev/null 2>&1; then
    find "$WORKDIR" -type f -exec shred -u -- {} + 2>/dev/null
  fi
  rm -rf -- "$WORKDIR"
}
trap cleanup EXIT INT TERM

MAP_FILE="$WORKDIR/password_map.tsv"       # password<TAB>entry-name
EXPANDED_MAP="$WORKDIR/expanded_map.tsv"   # variant<TAB>entry-name<TAB>encoding
PATTERNS_FILE="$WORKDIR/patterns.txt"      # unique variant strings, one per line
RAW_ENTRIES="$WORKDIR/raw_entries.tsv"     # entry-name<TAB>password
RAW_MATCHES="$WORKDIR/raw_matches.tsv"     # path<TAB>password
LEAKED_PATTERNS="$WORKDIR/leaked.txt"       # unique passwords actually found leaked
ARCHIVE_MATCHES="$WORKDIR/archive_matches.tsv" # path<TAB>password
FORMAT_AWK="$WORKDIR/format.awk"

# get unique pws
echo "Checking rbw vault status..." >&2
if ! rbw unlocked >/dev/null 2>&1; then
  echo "Vault is locked — check for a pinentry/terminal prompt to unlock it." >&2
  rbw unlock || { echo "Failed to unlock vault" >&2; exit 1; }
fi

if [ "$NO_SYNC" -ne 1 ]; then
  echo "Syncing vault..." >&2
  rbw sync || echo "Warning: rbw sync failed" >&2 # TODO fallback mechanism or error out?
fi

echo "Reading vault entries..." >&2
mapfile -t LOGIN_IDS < <(rbw list --raw | jq -r '.[] | select(.type == "Login") | .id')

if [ "${#LOGIN_IDS[@]}" -eq 0 ]; then
  echo "No login entries found in vault." >&2
  exit 0
fi

fetch_entry() {
  local id="$1"
  rbw get --raw "$id" 2>/dev/null | jq -r '[.name, (.data.password // "")] | @tsv' 2>/dev/null
}
export -f fetch_entry

printf '%s\n' "${LOGIN_IDS[@]}" | xargs -P "$JOBS" -n 1 bash -c 'fetch_entry "$0"' > "$RAW_ENTRIES"

awk -F'\t' -v minlen="$MIN_PW_LEN" \
  'NF >= 2 && $2 != "" && length($2) >= minlen { print $2 "\t" $1 }' \
  "$RAW_ENTRIES" > "$MAP_FILE"

TOTAL_WITH_PW=$(awk -F'\t' 'NF >= 2 && $2 != ""' "$RAW_ENTRIES" | wc -l)
SKIPPED_SHORT=$((TOTAL_WITH_PW - $(wc -l < "$MAP_FILE")))

if [ ! -s "$MAP_FILE" ]; then
  echo "No entries with non-empty, >= ${MIN_PW_LEN} char) password found in vault." >&2
  exit 0
fi

PW_COUNT=$(cut -f1 "$MAP_FILE" | sort -u | wc -l)
echo "Loaded $PW_COUNT unique password(s) from ${#LOGIN_IDS[@]} login entries (skipped $SKIPPED_SHORT short/blank)." >&2

# Build variant map: plain + base64 + reversed copy of each password, all
# pointing back to the same vault entry name, so a hidden/obfuscated copy
# still gets reported and attributed correctly.
awk -F'\t' '{ print $1 "\t" $2 "\tplain" }' "$MAP_FILE" > "$EXPANDED_MAP"
while IFS=$'\t' read -r pw name; do
  [ -n "$pw" ] || continue
  if [ "$HAVE_BASE64" -eq 1 ]; then
    b64="$(printf '%s' "$pw" | base64 -w0 2>/dev/null)"
    [ -n "$b64" ] && [ "$b64" != "$pw" ] && printf '%s\t%s\tbase64\n' "$b64" "$name" >> "$EXPANDED_MAP"
  fi
  if [ "$HAVE_REV" -eq 1 ]; then
    revpw="$(rev <<< "$pw")"
    [ -n "$revpw" ] && [ "$revpw" != "$pw" ] && printf '%s\t%s\treversed\n' "$revpw" "$name" >> "$EXPANDED_MAP"
  fi
done < "$MAP_FILE"

cut -f1 "$EXPANDED_MAP" | sort -u > "$PATTERNS_FILE"
VARIANT_COUNT=$(wc -l < "$PATTERNS_FILE")
variant_note=""
[ "$HAVE_BASE64" -eq 0 ] && variant_note="${variant_note}; base64 check skipped (base64 not found)"
[ "$HAVE_REV" -eq 0 ] && variant_note="${variant_note}; reversed check skipped (rev not found)"
echo "Watching for $VARIANT_COUNT variant string(s) total (plain + base64 + reversed)${variant_note}." >&2

# Always fold in local Claude CLI / Gemini CLI history dirs, even if ROOT is
# narrowed to something else — these are high-value leak vectors (pasted
# secrets in agent transcripts) that a narrower scan target would otherwise miss.
SCAN_ROOTS=("$ROOT")
is_under() { # is_under child parent
  [ "$1" = "$2" ] && return 0
  case "$1" in
    "$2"/*) return 0 ;;
    *) return 1 ;;
  esac
}
for extra in "$HOME/.claude" "$HOME/.gemini"; do
  [ -d "$extra" ] || continue
  is_under "$extra" "$ROOT" && continue
  SCAN_ROOTS+=("$extra")
done

# TODO this is nonexhaustive and I am not fully sure they never contain user pws accidentally
EXCLUDE_DIRS=(
  .git .cache node_modules .venv venv __pycache__
  .npm .cargo .rustup .steam .wine .var Steam .mozilla/firefox/*/storage
  .config/google-chrome .config/chromium .config/BraveSoftware
  Library/Caches
  go/pkg/mod vendor site-packages .yarn .pnpm-store Pods .gradle .m2
  .stack-work .cabal
  .cache/rbw .local/share/rbw .config/rbw
)

# detect plainscans using nullbyte
RG_EXCLUDE_GLOBS=()
for d in "${EXCLUDE_DIRS[@]}"; do
  RG_EXCLUDE_GLOBS+=(--glob "!**/$d/**")
done

# get folder/file basenames
FIND_PRUNE=()
for d in "${EXCLUDE_DIRS[@]}"; do
  [ "${#FIND_PRUNE[@]}" -gt 0 ] && FIND_PRUNE+=(-o)
  FIND_PRUNE+=(-name "${d##*/}")
done

pdf_note="; PDFs skipped (pdftotext not found)"
[ "$HAVE_PDFTOTEXT" -eq 1 ] && pdf_note=" + PDFs (<= ${MAX_PDF_MB}MB, ${PDF_TIMEOUT}s timeout/file)"
echo "Scanning ${SCAN_ROOTS[*]} using $JOBS parallel workers for stray passwords in text files${pdf_note}..." >&2

# plain text scan
text_matches() {
  rg --json --hidden \
    --threads "$JOBS" \
    --max-filesize "$MAX_TEXT_SIZE" \
    "${RG_EXCLUDE_GLOBS[@]}" \
    -o -F -f "$PATTERNS_FILE" \
    "${SCAN_ROOTS[@]}" 2>/dev/null |
  jq -r 'select(.type == "match") | .data.path.text as $p | .data.submatches[].match.text as $m | [$p, $m] | @tsv'
}

# --- Stage B: PDFs, extracted to text in parallel across all cores ---------
process_pdf() {
  local f="$1"
  local size
  size=$(stat -c%s -- "$f" 2>/dev/null) || return 0
  if [ "$size" -gt "$MAX_PDF_BYTES" ]; then
    return 0
  fi
  timeout "$PDF_TIMEOUT" pdftotext -q -- "$f" - 2>/dev/null |
    rg -o -F -f "$PATTERNS_FILE" 2>/dev/null |
    while IFS= read -r pw; do
      printf '%s\t%s\n' "$f" "$pw"
    done
}
export -f process_pdf
export PATTERNS_FILE MAX_PDF_BYTES PDF_TIMEOUT

pdf_matches() {
  [ "$HAVE_PDFTOTEXT" -eq 1 ] || return 0
  rg --files -0 --threads "$JOBS" --glob '*.pdf' \
    "${RG_EXCLUDE_GLOBS[@]}" \
    "${SCAN_ROOTS[@]}" 2>/dev/null |
  xargs -0 -P "$JOBS" -n 1 bash -c 'process_pdf "$0"'
}

# password as directory or file name
name_matches() {
  find "${SCAN_ROOTS[@]}" -mindepth 1 \( "${FIND_PRUNE[@]}" \) -prune -o -print 2>/dev/null |
  awk -F'/' -v patterns="$PATTERNS_FILE" '
    BEGIN {
      while ((getline line < patterns) > 0) if (line != "") pats[++np] = line
      close(patterns)
    }
    {
      base = $NF
      for (i = 1; i <= np; i++) {
        if (index(base, pats[i]) > 0) print $0 "\t" pats[i]
      }
    }'
}

{
  text_matches
  pdf_matches
  name_matches
} > "$RAW_MATCHES"

if [ ! -s "$RAW_MATCHES" ]; then
  echo "No stray passwords found." >&2
  exit 0
fi

# Shared formatter: dedupe path+pw pairs, mask password, attach vault entry name(s)
cat > "$FORMAT_AWK" <<'AWK'
BEGIN {
  FS = "\t"
  while ((getline line < mapfile) > 0) {
    n = split(line, f, "\t")
    if (n < 2) continue
    pw = f[1]; name = f[2]; enc = (n >= 3 ? f[3] : "plain")
    if (!(pw in names)) {
      names[pw] = name
    } else if (index(names[pw], name) == 0) {
      names[pw] = names[pw] ", " name
    }
    if (!(pw in encs)) encs[pw] = enc
  }
  close(mapfile)
}
{
  path = $1
  pw = $2
  key = path SUBSEP pw
  if (key in seen) next
  seen[key] = 1

  if (show_full == "1") {
    display = pw
  } else {
    n = length(pw)
    if (n <= 2) {
      display = ""
      for (i = 0; i < n; i++) display = display "*"
    } else {
      mid = ""
      for (i = 0; i < n - 2; i++) mid = mid "*"
      display = substr(pw, 1, 1) mid substr(pw, n, 1)
    }
  }

  tag = (pw in encs && encs[pw] != "plain") ? " [" encs[pw] "]" : ""
  entry = (pw in names) ? names[pw] : "?"
  print path "\t" display tag "\t" entry
}
AWK

format_report() {
  local matches_file="$1"
  awk -v mapfile="$EXPANDED_MAP" -v show_full="$SHOW_FULL" -f "$FORMAT_AWK" "$matches_file" |
  sort -u -t "$(printf '\t')" -k1,1 -k2,2 |
  awk -F'\t' '{ print $1 " -> " $2 "  (vault entry: " $3 ")" }'
}

# Dedupe, match to vault entry
format_report "$RAW_MATCHES"

# --- Stage C: do any leaked passwords also unlock a protected archive/PDF? --
cut -f2 "$RAW_MATCHES" | sort -u > "$LEAKED_PATTERNS"

if [ "$CHECK_ARCHIVES" -eq 1 ] && [ -s "$LEAKED_PATTERNS" ]; then
  if [ "$HAVE_7Z" -eq 0 ] && [ "$HAVE_QPDF" -eq 0 ]; then
    echo "" >&2
    echo "Skipping archive/PDF password checks (neither 7z nor qpdf found)." >&2
  else
    echo "" >&2
    echo "Checking whether leaked passwords unlock any protected archive/PDF..." >&2

    BOGUS_PW="$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
    [ -n "$BOGUS_PW" ] || BOGUS_PW="xR7-not-a-real-password-9021"

    export SEVENZ_BIN BOGUS_PW LEAKED_PATTERNS

    check_archive() {
      local f="$1"
      # bogus password succeeding means the archive isn't password-protected at all
      "$SEVENZ_BIN" t -p"$BOGUS_PW" -- "$f" >/dev/null 2>&1 </dev/null && return 0
      while IFS= read -r pw; do
        [ -n "$pw" ] || continue
        if "$SEVENZ_BIN" t -p"$pw" -- "$f" >/dev/null 2>&1 </dev/null; then
          printf '%s\t%s\n' "$f" "$pw"
          return 0
        fi
      done < "$LEAKED_PATTERNS"
    }
    export -f check_archive

    check_pdf() {
      local f="$1"
      qpdf --is-encrypted -- "$f" >/dev/null 2>&1 </dev/null || return 0
      while IFS= read -r pw; do
        [ -n "$pw" ] || continue
        if qpdf --check --password="$pw" -- "$f" >/dev/null 2>&1 </dev/null; then
          printf '%s\t%s\n' "$f" "$pw"
          return 0
        fi
      done < "$LEAKED_PATTERNS"
    }
    export -f check_pdf

    {
      if [ "$HAVE_7Z" -eq 1 ]; then
        rg --files -0 --threads "$JOBS" \
          --glob '*.zip' --glob '*.7z' --glob '*.rar' \
          "${RG_EXCLUDE_GLOBS[@]}" "${SCAN_ROOTS[@]}" 2>/dev/null |
        xargs -0 -P "$JOBS" -n 1 bash -c 'check_archive "$0"'
      fi
      if [ "$HAVE_QPDF" -eq 1 ]; then
        rg --files -0 --threads "$JOBS" --glob '*.pdf' \
          "${RG_EXCLUDE_GLOBS[@]}" "${SCAN_ROOTS[@]}" 2>/dev/null |
        xargs -0 -P "$JOBS" -n 1 bash -c 'check_pdf "$0"'
      fi
    } > "$ARCHIVE_MATCHES"

    if [ -s "$ARCHIVE_MATCHES" ]; then
      echo "Leaked password(s) also unlock these protected files:" >&2
      format_report "$ARCHIVE_MATCHES"
    else
      echo "None of the leaked passwords unlock any protected archive/PDF found." >&2
    fi
  fi
fi

# --- Stage D: offer to delete the plaintext-leak files (not the archives above) --
do_delete() {
  local p="$1"
  rm -rf -- "$p" && echo "Deleted $p" >&2 || echo "Failed to delete $p" >&2
}

if [ "$OFFER_REMOVE" -eq 1 ] && [ -t 0 ] && [ -t 1 ]; then
  echo "" >&2
  echo "Note: a = yes to ALL remaining, x = keep (no) ALL remaining" >&2
  mode=""
  while IFS= read -r path <&3; do
    [ -e "$path" ] || continue
    case "$mode" in
      a) reply="y" ;;
      x) reply="n" ;;
      *) read -r -p "Delete '$path'? [y/N/a/x] " reply ;;
    esac
    case "$reply" in
      [Aa]*)
        mode="a"
        do_delete "$path"
        ;;
      [Xx]*)
        mode="x"
        echo "Kept $path" >&2
        ;;
      [Yy]*)
        do_delete "$path"
        ;;
      *)
        echo "Kept $path" >&2
        ;;
    esac
  done 3< <(cut -f1 "$RAW_MATCHES" | sort -u)
elif [ "$OFFER_REMOVE" -eq 1 ]; then
  echo "" >&2
  echo "(not running in a terminal — skipping delete prompts; set OFFER_REMOVE=0 to silence this)" >&2
fi
