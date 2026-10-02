#!/usr/bin/env bash
#
# check-hibp.sh — check every unique password in your rbw-managed Bitwarden
# vault against the Pwned Passwords k-anonymity range API
# (api.pwnedpasswords.com). Only the first 5 hex chars of the SHA-1 hash
# ever leave this machine — the full password and full hash never do, and
# no API key is required.
#
# Only vault entries whose password turns up in a breach are printed.
# Entries with no match are not shown.
#
# Requires: rbw (https://github.com/doy/rbw), logged in; jq; curl; sha1sum.
# rbw will prompt you (pinentry/terminal) for your master password if the
# vault is currently locked.
#
# Usage:
#   scripts/check-hibp.sh [--show]
#
# Flags:
#   --show   print the actual plaintext pwned password alongside each hit,
#            instead of just the breach count and vault entry name(s).
#
# Env overrides:
#   PWNED_PW_JOBS   parallel workers for the range-API lookups, default 4.
#   MIN_PW_LEN      skip vault passwords shorter than this, default 6.
#   NO_SYNC         set to 1 to skip "rbw sync" before reading the vault.
#
# Output format (only for passwords found in a breach):
#   PWNED — seen in N breach(es)  (vault entry: name1, name2)
#   PWNED — seen in N breach(es)  (vault entry: name1, name2)  password: hunter2   [with --show]

set -uo pipefail

SHOW=0
for arg in "$@"; do
  case "$arg" in
    --show) SHOW=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

JOBS_DEFAULT="$(nproc)"
PWNED_PW_JOBS="${PWNED_PW_JOBS:-4}"
MIN_PW_LEN="${MIN_PW_LEN:-6}"
NO_SYNC="${NO_SYNC:-0}"

for bin in rbw jq curl sha1sum; do
  command -v "$bin" >/dev/null 2>&1 || { echo "$bin is required but not found in PATH" >&2; exit 1; }
done

# ---------------------------------------------------------------------------
# Working directory for intermediate files. These hold plaintext passwords,
# so keep them private and scrub them on exit.
# ---------------------------------------------------------------------------
WORKDIR="$(mktemp -d)"
chmod 700 "$WORKDIR"
cleanup() {
  if command -v shred >/dev/null 2>&1; then
    find "$WORKDIR" -type f -exec shred -u -- {} + 2>/dev/null
  fi
  rm -rf -- "$WORKDIR"
}
trap cleanup EXIT INT TERM

RAW_ENTRIES="$WORKDIR/raw_entries.tsv"   # name<TAB>password
PW_MAP="$WORKDIR/pw_map.tsv"             # password<TAB>entry-names

# ---------------------------------------------------------------------------
# Stage 0: pull unique passwords out of the vault via rbw.
# ---------------------------------------------------------------------------
echo "Checking rbw vault status..." >&2
if ! rbw unlocked >/dev/null 2>&1; then
  echo "Vault is locked — check for a pinentry/terminal prompt to unlock it." >&2
  rbw unlock || { echo "Failed to unlock vault" >&2; exit 1; }
fi

if [ "$NO_SYNC" -ne 1 ]; then
  echo "Syncing vault..." >&2
  rbw sync || echo "Warning: rbw sync failed, continuing with local cache" >&2
fi

echo "Reading vault entries..." >&2
mapfile -t LOGIN_IDS < <(rbw list --raw | jq -r '.[] | select(.type == "Login") | .id')

if [ "${#LOGIN_IDS[@]}" -eq 0 ]; then
  echo "No login entries found in vault." >&2
  exit 0
fi

fetch_entry() {
  local id="$1"
  rbw get --raw "$id" 2>/dev/null |
    jq -r '[.name, (.data.password // "")] | @tsv' 2>/dev/null
}
export -f fetch_entry

printf '%s\n' "${LOGIN_IDS[@]}" | xargs -P "$JOBS_DEFAULT" -n 1 bash -c 'fetch_entry "$0"' > "$RAW_ENTRIES"

# password<TAB>entry-name, deduped/joined per unique password
awk -F'\t' -v minlen="$MIN_PW_LEN" '
  NF >= 2 && $2 != "" && length($2) >= minlen {
    pw = $2; name = $1
    if (!(pw in names)) names[pw] = name
    else if (index(names[pw], name) == 0) names[pw] = names[pw] ", " name
  }
  END { for (pw in names) print pw "\t" names[pw] }
' "$RAW_ENTRIES" > "$PW_MAP"

PW_COUNT=$(wc -l < "$PW_MAP")
echo "Loaded $PW_COUNT unique password(s) from ${#LOGIN_IDS[@]} login entries." >&2

if [ ! -s "$PW_MAP" ]; then
  echo "No usable passwords found in vault." >&2
  exit 0
fi

# ---------------------------------------------------------------------------
# Passwords, via the free k-anonymity Pwned Passwords range API.
# ---------------------------------------------------------------------------
echo "Checking $PW_COUNT password(s) against Pwned Passwords (api.pwnedpasswords.com)..." >&2

check_password() {
  local pw="$1" entries="$2"
  local hash prefix suffix response count
  hash=$(printf '%s' "$pw" | sha1sum | cut -d' ' -f1 | tr 'a-f' 'A-F')
  prefix="${hash:0:5}"
  suffix="${hash:5}"
  response=$(curl -s --fail --max-time 10 "https://api.pwnedpasswords.com/range/$prefix" 2>/dev/null) || {
    printf '%s\t%s\tERROR\n' "$pw" "$entries"
    return
  }
  count=$(printf '%s' "$response" | tr -d '\r' | awk -F: -v s="$suffix" 'toupper($1) == s { print $2; found=1 } END { if (!found) print 0 }')
  printf '%s\t%s\t%s\n' "$pw" "$entries" "$count"
}
export -f check_password

awk -F'\t' '{ print $1 "\t" $2 }' "$PW_MAP" |
  xargs -P "$PWNED_PW_JOBS" -I{} -d '\n' bash -c '
    IFS=$(printf "\t") read -r pw entries <<< "{}"
    check_password "$pw" "$entries"
  ' |
while IFS=$'\t' read -r pw entries count; do
  if [ "$count" = "ERROR" ]; then
    echo "LOOKUP FAILED  (vault entry: $entries)" >&2
  elif [ "$count" -gt 0 ] 2>/dev/null; then
    if [ "$SHOW" -eq 1 ]; then
      echo "PWNED — seen in $count breach(es)  (vault entry: $entries)  password: $pw"
    else
      echo "PWNED — seen in $count breach(es)  (vault entry: $entries)"
    fi
  fi
done
