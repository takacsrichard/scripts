#!/usr/bin/env bash
#
# find-stray-emails.sh — scan the home folder for email addresses left lying
# around in .txt/.json/.csv/.tsv/.log/.md/.yml/.yaml/.config/.env/.pdf files,
# and print the ones that plausibly belong to the machine's owner (as opposed
# to noreply/support/legal addresses or infra subdomains like
# mail-api.proton.me).
#
# Usage:
#   scripts/find-stray-emails.sh [root-dir]
#
# Env overrides:
#   MAX_PDF_MB     max PDF size (MB) to text-extract, default 25
#   PDF_TIMEOUT    per-PDF extraction timeout (seconds), default 10
#   MAX_TEXT_SIZE  max size for plain-text files, default 200M (rg syntax)
#
# Output format:
#   PATH -> EMAIL
#
# followed by a de-duplicated list of every distinct email found.

set -uo pipefail

ROOT="${1:-$HOME}"
JOBS="$(nproc)"
MAX_PDF_MB="${MAX_PDF_MB:-25}"
MAX_PDF_BYTES=$((MAX_PDF_MB * 1024 * 1024))
PDF_TIMEOUT="${PDF_TIMEOUT:-10}"
MAX_TEXT_SIZE="${MAX_TEXT_SIZE:-200M}"

command -v rg >/dev/null 2>&1 || { echo "ripgrep (rg) is required but not found in PATH" >&2; exit 1; }

RESULTS_FILE="$(mktemp)"
trap 'rm -f -- "$RESULTS_FILE"' EXIT INT TERM

HAVE_PDFTOTEXT=0
command -v pdftotext >/dev/null 2>&1 && HAVE_PDFTOTEXT=1

# Broad email regex; the interesting filtering happens in the shared filter
# pass below rather than here, so this stays permissive.
EMAIL_RE='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

# Directories that are noisy, huge, or never contain "real" user data.
EXCLUDE_DIRS=(
  .git .cache node_modules .venv venv __pycache__
  .npm .cargo .rustup .steam .wine .var Steam .mozilla/firefox/*/storage
  .config/google-chrome .config/chromium .config/BraveSoftware
  Library/Caches
  go/pkg/mod vendor site-packages .yarn .pnpm-store Pods .gradle .m2
  .stack-work .cabal
)

# Plain-text-ish file types worth grepping directly.
INCLUDE_GLOBS=(
  '*.txt' '*.json' '*.csv' '*.tsv' '*.log' '*.md' '*.yml' '*.yaml' '*.config'
  '.env' '.env.*' '*.env'
)

# Specific filenames that are pure noise for this purpose: browser extension
# manifests use "id@vendor.tld" as an addon identifier, which the broad email
# regex below happily matches (e.g. addons-search-detection@mozilla.com).
EXCLUDE_FILES=(
  extensions.json extension-preferences.json extension-settings.json
  addons.json addonStartup.json.lz4
)

# rg anchors --glob patterns to the current working directory rather than
# the search-root positional argument, so anchor with **/ instead to make
# exclusions work regardless of where this script is invoked from.
RG_EXCLUDE_GLOBS=()
for d in "${EXCLUDE_DIRS[@]}"; do
  RG_EXCLUDE_GLOBS+=(--glob "!**/$d/**")
done
for f in "${EXCLUDE_FILES[@]}"; do
  RG_EXCLUDE_GLOBS+=(--glob "!**/$f")
done

RG_GLOBS=()
for g in "${INCLUDE_GLOBS[@]}"; do
  RG_GLOBS+=(--glob "$g")
done
RG_GLOBS+=("${RG_EXCLUDE_GLOBS[@]}")

# ---------------------------------------------------------------------------
# Filtering rules for "this is probably a company/software address, not the
# machine operator's own address".
# ---------------------------------------------------------------------------

# Local-part (before the @) patterns that scream "automated/organizational".
LOCAL_EXCLUDE_RE='^(no-?reply|do-?not-?reply|donotreply|mailer-?daemon|postmaster|webmaster|hostmaster|support|help|helpdesk|info|contact|sales|marketing|billing|invoice|invoices|security|abuse|legal|privacy|compliance|press|media|careers|jobs|hr|admin|administrator|root|api|notifications?|notify|alert(s)?|bounces?|feedback|newsletter|updates?|notice|service|services|team|hello|welcome|accounts?|noreply-[a-z]+|subscriptions?|unsubscribe|orders?|shipping|delivery|store|shop|ticket(s)?|helpbot|bot|system|automated|no_reply|do_not_reply)$'

# Domain/subdomain patterns that are clearly infra rather than a personal
# provider (e.g. mail-api.proton.me, notifications.github.com).
DOMAIN_EXCLUDE_RE='(^|\.)(mail-api|api|smtp|mx[0-9]*|bounce(s)?|notifications?|notify|no-?reply|donotreply|mailer|email|newsletter|marketing|list(s)?|campaign(s)?|click|links|track(ing)?|status|alerts?)\.'

# Common placeholder/example domains that show up in docs, templates, and
# disclaimers rather than real inboxes; plus non-email "@domain" syntax that
# the broad regex above can mistake for an email address (systemd unit
# specifiers like user@1000.service, weston@root.service, GCP service
# accounts, etc.).
PLACEHOLDER_DOMAIN_RE='@(example\.(com|org|net)|test\.(com|dev)|localhost|domain\.(com|tld)|yourcompany\.com|company\.com|acme\.(com|org)|sentry\.io|amazonaws\.com|googleapis\.com|gserviceaccount\.com|gstatic\.com|schema\.org|w3\.org|github\.com|githubusercontent\.com|githubactions\.com|sendgrid\.net|mailgun\.org|mailchimp\.com|npmjs\.com|apple\.com|microsoft\.com|google\.com)$|\.service$|\.timer$|\.target$|\.socket$|\.mount$'

# Well-known personal email providers — used only to prioritize output
# (marked with no special tag currently, but kept as a documented allowlist
# in case stricter mode is wanted later): gmail.com, googlemail.com,
# protonmail.com, proton.me, pm.me, outlook.com, outlook.*, hotmail.*,
# live.com, msn.com, yahoo.*, icloud.com, me.com, mac.com, aol.com,
# gmx.com, gmx.net, gmx.de, zoho.com, yandex.com, yandex.ru, mail.com,
# fastmail.com, tutanota.com, tuta.io, web.de, t-online.de, freenet.de,
# libero.it, seznam.cz, wp.pl, o2.pl, qq.com, 163.com, 126.com, naver.com,
# rediffmail.com

pdf_note="; PDFs skipped (pdftotext not found)"
[ "$HAVE_PDFTOTEXT" -eq 1 ] && pdf_note=" + PDFs (<= ${MAX_PDF_MB}MB, ${PDF_TIMEOUT}s timeout/file)"
echo "Scanning $ROOT using $JOBS parallel workers for stray emails in text files${pdf_note}..." >&2

# --- Stage A: plain-text file types, single fast multi-threaded rg pass ----
text_matches() {
  rg --no-heading --no-line-number --with-filename \
    --threads "$JOBS" \
    --max-filesize "$MAX_TEXT_SIZE" \
    "${RG_GLOBS[@]}" \
    -o -P "$EMAIL_RE" \
    "$ROOT" 2>/dev/null |
  awk -F: '{
    email = $NF
    # rebuild path in case it contained a colon
    path = $0
    sub(/:[^:]*$/, "", path)
    print path "\t" email
  }'
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
    grep -oP "$EMAIL_RE" |
    while IFS= read -r e; do
      printf '%s\t%s\n' "$f" "$e"
    done
}
export -f process_pdf
export EMAIL_RE MAX_PDF_BYTES PDF_TIMEOUT

pdf_matches() {
  [ "$HAVE_PDFTOTEXT" -eq 1 ] || return 0
  rg --files -0 --threads "$JOBS" --glob '*.pdf' \
    "${RG_EXCLUDE_GLOBS[@]}" \
    "$ROOT" 2>/dev/null |
  xargs -0 -P "$JOBS" -n 1 bash -c 'process_pdf "$0"'
}

{
  text_matches
  pdf_matches
} |
LOCAL_EXCLUDE_RE="$LOCAL_EXCLUDE_RE" DOMAIN_EXCLUDE_RE="$DOMAIN_EXCLUDE_RE" PLACEHOLDER_DOMAIN_RE="$PLACEHOLDER_DOMAIN_RE" \
awk -F'\t' '
BEGIN {
  local_re = ENVIRON["LOCAL_EXCLUDE_RE"]
  domain_re = ENVIRON["DOMAIN_EXCLUDE_RE"]
  placeholder_re = ENVIRON["PLACEHOLDER_DOMAIN_RE"]
}
{
  path = $1
  email = $2
  gsub(/^[ \t]+|[ \t]+$/, "", email)

  lower = tolower(email)
  split(lower, parts, "@")
  local = parts[1]
  domain = parts[2]

  if (domain == "") next
  if (lower ~ placeholder_re) next
  if (local ~ local_re) next
  if (domain ~ domain_re) next

  print path "\t" email
}' |
sort -u -t "$(printf '\t')" -k2,2 -k1,1 > "$RESULTS_FILE"

awk -F'\t' '{ print $1 " -> " $2 }' "$RESULTS_FILE"

if [ -s "$RESULTS_FILE" ]; then
  echo
  echo "Unique emails found:"
  cut -f2 "$RESULTS_FILE" | sort -u
fi
