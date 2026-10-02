"""
Bitwarden Vault Health Analyzer — rbw Edition
===============================================

A read-only auditing tool for a live Bitwarden vault. It pulls every login
item through `rbw` (no manual export step needed), then reports on password
reuse, password strength/structure, duplicate or incomplete entries, TOTP
coverage, and (optionally) known-breach exposure — finishing with a single
overall vault health score.

Requirements
------------
    pip install pandas
    rbw (https://github.com/doy/rbw), configured and logged in against
    the Bitwarden EU cloud:
        rbw config set email you@example.com
        rbw config set base_url https://api.bitwarden.eu
        rbw config set identity_url https://identity.bitwarden.eu
        rbw register   # personal API key, needed once to avoid bot detection
        rbw login

Usage
-----
    python hygenechck.py                  # human-readable report
    python hygenechck.py --obfuscate      # mask usernames/passwords in output
    python hygenechck.py --check-pwned    # also check Have I Been Pwned
    python hygenechck.py --json           # machine-readable report on stdout

Privacy note on --check-pwned
------------------------------
Breach checking uses the "Pwned Passwords" k-anonymity API: only the first
5 characters of each password's SHA-1 hash are ever sent over the network,
never the password itself. See https://haveibeenpwned.com/API/v3#PwnedPasswords
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TOP_N = 5
DEFAULT_MIN_LENGTH = 12          # passwords shorter than this are "short"
TOP_REUSED_PASSWORDS_WITH_ENTRIES = 3
HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 10
HIBP_USER_AGENT = "rbwcheck-vault-health-analyzer"

KEYBOARD_WALKS = [
    "qwerty", "qwert", "asdfgh", "asdfg", "asdf", "zxcvbn", "zxcvb",
    "123456", "12345", "23456", "34567", "45678", "56789", "98765", "87654",
]

STRUCTURE_LABELS = {
    "passphrase":        "Passphrase           (e.g. Asd-Basd, correct-horse)",
    "standard_pattern":  "Standard pattern     (e.g. Password123!, Summer2024)",
    "random":            "Random / high-entropy (e.g. xK9#mP2$!qR7)",
    "numeric_only":      "Numeric only / PIN   (e.g. 1234, 198804)",
    "alphabetic_simple": "Simple word / name    (e.g. password, sunshine)",
    "keyboard_walk":     "Keyboard walk        (e.g. qwerty123, asdf1234)",
    "other":             "Other",
}

# Structural categories that are considered weak regardless of length.
WEAK_STRUCTURE_CATEGORIES = {"numeric_only", "keyboard_walk", "alphabetic_simple"}

# Structural categories that indicate a deliberately strong password.
STRONG_STRUCTURE_CATEGORIES = {"passphrase", "random"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class VaultEntry:
    """One Bitwarden login item, flattened to the fields this tool cares about."""
    name: str
    uri: str
    username: str
    password: str
    has_totp: bool = False


@dataclass
class VaultReport:
    """Structured result of analyzing a vault. Rendered as text or JSON."""
    meta: dict[str, Any] = field(default_factory=dict)
    overview: dict[str, Any] = field(default_factory=dict)
    top_usernames: list[dict[str, Any]] = field(default_factory=list)
    top_passwords: list[dict[str, Any]] = field(default_factory=list)
    top_password_entries: list[dict[str, Any]] = field(default_factory=list)
    top_combos: list[dict[str, Any]] = field(default_factory=list)
    structure_breakdown: list[dict[str, Any]] = field(default_factory=list)
    length_stats: dict[str, Any] = field(default_factory=dict)
    risky_entries: dict[str, Any] = field(default_factory=dict)
    duplicate_sites: list[dict[str, Any]] = field(default_factory=list)
    totp_coverage: dict[str, Any] = field(default_factory=dict)
    breach_check: dict[str, Any] = field(default_factory=dict)
    health_score: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "overview": self.overview,
            "top_usernames": self.top_usernames,
            "top_passwords": self.top_passwords,
            "top_password_entries": self.top_password_entries,
            "top_combos": self.top_combos,
            "structure_breakdown": self.structure_breakdown,
            "length_stats": self.length_stats,
            "risky_entries": self.risky_entries,
            "duplicate_sites": self.duplicate_sites,
            "totp_coverage": self.totp_coverage,
            "breach_check": self.breach_check,
            "health_score": self.health_score,
        }


# ---------------------------------------------------------------------------
# Masking (for --obfuscate)
# ---------------------------------------------------------------------------

def mask_string(value: str) -> str:
    """Mask a string to hide sensitive content, keeping only the first/last char."""
    if not isinstance(value, str) or not value:
        return ""
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]


def obfuscate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a report dict with every 'username'/'password'
    value masked, wherever it appears in the structure.

    'top_password_entries' is dropped entirely rather than masked: it lists
    the actual vault entry/site names sharing a reused password, and masking
    just the password would still leak that association.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                if key == "top_password_entries":
                    result[key] = []
                elif key in ("username", "password") and isinstance(value, str):
                    result[key] = mask_string(value)
                else:
                    result[key] = walk(value)
            return result
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(report)


# ---------------------------------------------------------------------------
# Password structure classification
# ---------------------------------------------------------------------------

def classify_password_structure(password: str) -> str:
    """Classify a password into a structural category (see STRUCTURE_LABELS)."""
    if not password:
        return "other"

    pw_lower = password.lower()

    # 1. Numeric only (PIN / date)
    if password.isdigit():
        return "numeric_only"

    # 2. Keyboard walk pattern
    if any(walk in pw_lower for walk in KEYBOARD_WALKS):
        return "keyboard_walk"

    # 3. Passphrase — separator-based (Asd-Basd, correct_horse_battery)
    sep_parts = re.split(r"[-_.\s]+", password)
    if len(sep_parts) >= 2:
        word_parts = [p for p in sep_parts if re.match(r"^[a-zA-Z]{3,}$", p)]
        if len(word_parts) >= 2 and len(word_parts) / len(sep_parts) >= 0.6:
            return "passphrase"

    # 4. Passphrase — CamelCase (AsdBasd, BlueHorsePurple)
    camel_words = re.findall(r"[A-Z][a-z]{2,}", password)
    if len(camel_words) >= 2:
        camel_coverage = sum(len(w) for w in camel_words) / len(password)
        if camel_coverage >= 0.7:
            return "passphrase"

    # 5. Standard pattern: [optional_special] Word Numbers [optional_special]
    #    e.g. Password123!, Admin2024, hello99!, Summer@2024
    if re.match(r"^[^a-zA-Z0-9]{0,2}[A-Za-z]{3,15}[0-9]{1,8}[^a-zA-Z0-9]{0,3}$", password):
        return "standard_pattern"
    if re.match(r"^[^a-zA-Z0-9]{0,2}[A-Za-z]{3,15}[^a-zA-Z0-9]{1,3}[0-9]{1,8}[^a-zA-Z0-9]{0,2}$", password):
        return "standard_pattern"

    # 6. Random / high-entropy: multiple char classes + high unique-char ratio
    char_classes = sum([
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[0-9]", password)),
        bool(re.search(r"[^a-zA-Z0-9]", password)),
    ])
    unique_ratio = len(set(password)) / len(password)

    if char_classes >= 3 and unique_ratio >= 0.55:
        return "random"
    if char_classes >= 2 and len(password) >= 14 and unique_ratio >= 0.65:
        return "random"

    # 7. Simple word (only letters, no digits or specials)
    if re.match(r"^[a-zA-Z]+$", password):
        return "alphabetic_simple"

    return "other"


def is_weak_password(password: str, category: str, min_length: int) -> bool:
    """A password is weak if its structure is inherently guessable, or it's
    simply too short to resist offline brute-forcing regardless of structure."""
    return category in WEAK_STRUCTURE_CATEGORIES or len(password) < min_length


# ---------------------------------------------------------------------------
# rbw integration
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    """Progress/status output. Kept off stdout so `--json` output stays clean
    and pipeable (e.g. `python hygenechck.py --json > report.json`)."""
    print(message, file=sys.stderr)


def _run_rbw(*args: str) -> str:
    """Run an rbw subcommand and return its stdout, raising on failure."""
    try:
        result = subprocess.run(["rbw", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("rbw is not installed or not on PATH.") from exc
    if result.returncode != 0:
        raise RuntimeError(f"rbw {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _run_rbw_interactive(*args: str, timeout: int | None = 30) -> None:
    """Run an rbw subcommand with the terminal attached (for pinentry / sync)."""
    try:
        result = subprocess.run(["rbw", *args], timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("rbw is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"rbw {' '.join(args)} timed out after {timeout}s — "
            "is rbw-agent running? Try: rbw unlock && rbw sync"
        )
    if result.returncode != 0:
        raise RuntimeError(f"rbw {' '.join(args)} failed (exit {result.returncode})")


def ensure_unlocked() -> None:
    """Unlock the vault if needed. rbw will prompt via pinentry as required."""
    if subprocess.run(["rbw", "unlocked"], capture_output=True).returncode != 0:
        log("Vault is locked — check for a pinentry prompt to unlock it.")
        _run_rbw_interactive("unlock", timeout=120)


def fetch_vault_entries(no_sync: bool = False) -> list[VaultEntry]:
    """Sync with the Bitwarden cloud and pull every login item via rbw."""
    if not no_sync:
        log("Syncing vault...")
        _run_rbw_interactive("sync", timeout=30)

    try:
        entries = json.loads(_run_rbw("list", "--raw"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Could not parse `rbw list --raw` output as JSON.") from exc

    login_ids = [e["id"] for e in entries if e.get("type") == "Login"]

    log(f"Fetching {len(login_ids)} login item(s)...")
    result: list[VaultEntry] = []
    for item_id in login_ids:
        try:
            item = json.loads(_run_rbw("get", "--raw", item_id))
        except json.JSONDecodeError:
            log(f"  Skipping item {item_id}: could not parse rbw output.")
            continue

        data = item.get("data") or {}
        result.append(VaultEntry(
            name=item.get("name", ""),
            uri=_extract_first_uri(data.get("uris") or []),
            username=data.get("username") or "",
            password=data.get("password") or "",
            has_totp=bool(data.get("totp")),
        ))
    return result


def _extract_first_uri(uris: list[Any]) -> str:
    """rbw's `uris` entries are normally `{"uri": ..., "match_type": ...}`
    objects, but tolerate plain strings too in case that ever changes."""
    if not uris:
        return ""
    first = uris[0]
    if isinstance(first, dict):
        return first.get("uri") or ""
    return first if isinstance(first, str) else ""


# ---------------------------------------------------------------------------
# Have I Been Pwned — Pwned Passwords (k-anonymity) check
# ---------------------------------------------------------------------------

def check_pwned_passwords(passwords: Iterable[str], timeout: int = HIBP_TIMEOUT_SECONDS) -> dict[str, int]:
    """Check each password against the HIBP Pwned Passwords range API.

    Only the first 5 characters of each password's SHA-1 hash are sent —
    the full password never leaves the machine. Returns a dict mapping
    password -> breach count (0 if not found). Raises on network failure
    so the caller can decide how to report a partial/failed check.
    """
    results: dict[str, int] = {}
    for password in passwords:
        sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]

        request = urllib.request.Request(
            HIBP_RANGE_URL.format(prefix=prefix),
            headers={"User-Agent": HIBP_USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")

        count = 0
        for line in body.splitlines():
            candidate_suffix, _, candidate_count = line.partition(":")
            if candidate_suffix.strip() == suffix:
                count = int(candidate_count.strip())
                break
        results[password] = count
    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def build_report(
    entries: list[VaultEntry],
    top_n: int = DEFAULT_TOP_N,
    min_length: int = DEFAULT_MIN_LENGTH,
    check_pwned: bool = False,
) -> VaultReport:
    """Analyze fetched vault entries and return a structured VaultReport."""
    report = VaultReport()
    total_logins = len(entries)

    df = pd.DataFrame([{
        "name": e.name, "uri": e.uri, "username": e.username,
        "password": e.password, "has_totp": e.has_totp,
    } for e in entries])

    if df.empty:
        report.meta = {"total_login_items": 0, "excluded_no_uri": 0, "analyzed_entries": 0}
        return report

    df = df.loc[df["uri"].notna() & (df["uri"].str.strip() != "")]
    total_entries = len(df)
    report.meta = {
        "total_login_items": total_logins,
        "excluded_no_uri": total_logins - total_entries,
        "analyzed_entries": total_entries,
    }
    if total_entries == 0:
        return report

    df = df.fillna({"username": "", "password": ""})

    # Blank username/password shouldn't count as a "reused" identity/secret —
    # it just means several unrelated entries happen to have that field empty.
    named = df.loc[df["username"] != ""]
    keyed = df.loc[df["password"] != ""]
    paired = df.loc[(df["username"] != "") & (df["password"] != "")]

    unique_usernames = named["username"].nunique()
    unique_passwords = keyed["password"].nunique()
    unique_pairs = paired.groupby(["username", "password"]).ngroups

    report.overview = {
        "unique_sites": df["name"].nunique(),
        "unique_usernames": unique_usernames,
        "unique_passwords": unique_passwords,
        "entries_with_username": len(named),
        "entries_with_password": len(keyed),
        "entries_with_both": len(paired),
        "avg_entries_per_username": (len(named) / unique_usernames) if unique_usernames else 0.0,
        "avg_entries_per_password": (len(keyed) / unique_passwords) if unique_passwords else 0.0,
        "unique_pairs_count": unique_pairs,
        "unique_pairs_ratio": (unique_pairs / len(paired)) if len(paired) else 1.0,
    }

    report.top_usernames = [
        {"username": u, "count": int(c), "pct": round(c / len(named) * 100, 2)}
        for u, c in named["username"].value_counts().head(top_n).items()
    ] if len(named) else []

    report.top_passwords = [
        {"password": p, "count": int(c), "pct": round(c / len(keyed) * 100, 2)}
        for p, c in keyed["password"].value_counts().head(top_n).items()
    ] if len(keyed) else []

    report.top_password_entries = []
    if len(keyed):
        for p, c in keyed["password"].value_counts().head(TOP_REUSED_PASSWORDS_WITH_ENTRIES).items():
            entry_names = keyed.loc[keyed["password"] == p, "name"].tolist()
            report.top_password_entries.append({
                "password": p,
                "count": int(c),
                "entries": entry_names,
            })

    report.top_combos = []
    if len(paired):
        combo_counts = (
            paired.groupby(["username", "password"])
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
            .head(top_n)
        )
        for row in combo_counts.itertuples(index=False):
            report.top_combos.append({
                "username": row.username,
                "password": row.password,
                "count": int(row.count),
                "pct": round(row.count / len(paired) * 100, 2),
            })

    # --- Password structure & length (computed over unique password values) ---
    unique_pw_values = list(keyed["password"].unique())
    total_unique_pw = len(unique_pw_values)
    structure_by_pw = {pw: classify_password_structure(pw) for pw in unique_pw_values}
    structure_counts = Counter(structure_by_pw.values())

    report.structure_breakdown = [
        {
            "category": key,
            "label": label,
            "count": structure_counts.get(key, 0),
            "pct": round(structure_counts.get(key, 0) / total_unique_pw * 100, 1) if total_unique_pw else 0.0,
        }
        for key, label in STRUCTURE_LABELS.items()
    ]

    if unique_pw_values:
        lengths = [len(pw) for pw in unique_pw_values]
        short_count = sum(1 for n in lengths if n < min_length)
        report.length_stats = {
            "min": min(lengths),
            "max": max(lengths),
            "avg": round(sum(lengths) / len(lengths), 1),
            "threshold": min_length,
            "short_count": short_count,
            "short_pct": round(short_count / total_unique_pw * 100, 1),
            "total_unique": total_unique_pw,
        }
    else:
        report.length_stats = {"total_unique": 0}

    weak_pw_values = [
        pw for pw in unique_pw_values
        if is_weak_password(pw, structure_by_pw[pw], min_length)
    ]
    strong_pw_values = [
        pw for pw in unique_pw_values if structure_by_pw[pw] in STRONG_STRUCTURE_CATEGORIES
    ]

    # --- Risky / incomplete entries ---
    same_as_username = paired[paired["username"].str.lower() == paired["password"].str.lower()]
    report.risky_entries = {
        "password_equals_username_count": len(same_as_username),
        "password_equals_username_examples": same_as_username["name"].head(top_n).tolist(),
        "blank_password_count": len(named) - len(paired),
        "blank_username_count": len(keyed) - len(paired),
        "weak_password_count": len(weak_pw_values),
        "weak_password_pct": round(len(weak_pw_values) / total_unique_pw * 100, 1) if total_unique_pw else 0.0,
    }

    # --- Duplicate site entries (same URI used by more than one item) ---
    uri_counts = df["uri"].value_counts()
    duplicated = uri_counts[uri_counts > 1].head(top_n)
    report.duplicate_sites = [
        {"uri": uri, "count": int(count)} for uri, count in duplicated.items()
    ]

    # --- TOTP (2FA) coverage ---
    with_totp = int(df["has_totp"].sum())
    report.totp_coverage = {
        "with_totp": with_totp,
        "total": total_entries,
        "pct": round(with_totp / total_entries * 100, 1) if total_entries else 0.0,
    }

    # --- Optional: Have I Been Pwned breach check ---
    breach_check: dict[str, Any] = {"performed": False}
    if check_pwned and unique_pw_values:
        log(f"Checking {len(unique_pw_values)} unique password(s) against Have I Been Pwned...")
        try:
            breach_counts = check_pwned_passwords(unique_pw_values)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            breach_check = {"performed": False, "error": str(exc)}
        else:
            breached = {pw: n for pw, n in breach_counts.items() if n > 0}
            top_breached = sorted(breached.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            breach_check = {
                "performed": True,
                "unique_checked": len(unique_pw_values),
                "breached_count": len(breached),
                "top_breached": [{"password": pw, "breach_count": n} for pw, n in top_breached],
            }
    report.breach_check = breach_check

    report.health_score = compute_health_score(
        password_unique_ratio=(unique_passwords / len(keyed)) if len(keyed) else 1.0,
        weak_count=len(weak_pw_values),
        strong_count=len(strong_pw_values),
        total_unique_pw=total_unique_pw,
        breach_check=breach_check,
    )

    return report


def compute_health_score(
    *,
    password_unique_ratio: float,
    weak_count: int,
    strong_count: int,
    total_unique_pw: int,
    breach_check: dict[str, Any],
) -> dict[str, Any]:
    """Combine reuse, strength, structure, and (if available) breach data
    into a single 0-100 score plus a letter grade and plain-language findings.
    """
    components: list[tuple[str, float, float]] = []  # (name, weight, score_0_1)

    components.append(("password_reuse", 40.0, password_unique_ratio))

    weak_ratio = (weak_count / total_unique_pw) if total_unique_pw else 0.0
    components.append(("password_strength", 20.0, 1.0 - weak_ratio))

    strong_ratio = (strong_count / total_unique_pw) if total_unique_pw else 0.0
    components.append(("structure_quality", 20.0, strong_ratio))

    if breach_check.get("performed"):
        checked = breach_check["unique_checked"]
        breached = breach_check["breached_count"]
        breach_ratio = (breached / checked) if checked else 0.0
        components.append(("breach_exposure", 20.0, 1.0 - breach_ratio))

    total_weight = sum(weight for _, weight, _ in components)
    earned = sum(weight * score for _, weight, score in components)
    score = round(earned / total_weight * 100) if total_weight else 0

    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    findings: list[str] = []
    reused_pct = round((1 - password_unique_ratio) * 100)
    if reused_pct > 0:
        findings.append(f"{reused_pct}% of passworded entries reuse a password used elsewhere in the vault.")
    if weak_count > 0:
        findings.append(f"{weak_count} unique password(s) ({round(weak_ratio * 100)}%) are weak: short, numeric-only, a plain word, or a keyboard walk.")
    if breach_check.get("performed") and breach_check.get("breached_count", 0) > 0:
        findings.append(f"{breach_check['breached_count']} unique password(s) were found in known data breaches — change these immediately.")
    elif not breach_check.get("performed"):
        findings.append("Breach exposure was not checked — re-run with --check-pwned for a complete score.")
    if not findings:
        findings.append("No major issues detected.")

    return {"score": score, "grade": grade, "key_findings": findings}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_text_report(report: VaultReport) -> str:
    """Render a VaultReport as a readable, section-by-section plain-text report."""
    d = report.as_dict()
    lines: list[str] = []

    def header(title: str) -> None:
        lines.append("")
        lines.append(f"== {title} ==")

    meta = d["meta"]
    if meta.get("analyzed_entries", 0) == 0:
        lines.append("No login entries with a usable URI were found.")
        return "\n".join(lines)

    header("Vault Overview")
    lines.append(f"Total login items:            {meta['total_login_items']}")
    lines.append(f"Excluded (no URI):             {meta['excluded_no_uri']}")
    lines.append(f"Analyzed entries:              {meta['analyzed_entries']}")
    ov = d["overview"]
    lines.append(f"Unique sites (by name):        {ov['unique_sites']}")
    lines.append(f"Unique usernames:              {ov['unique_usernames']}")
    lines.append(f"Unique passwords:              {ov['unique_passwords']}")
    lines.append(f"Avg entries per username:      {ov['avg_entries_per_username']:.2f}")
    lines.append(f"Avg entries per password:      {ov['avg_entries_per_password']:.2f}")
    lines.append(f"Unique username/password pairs:{ov['unique_pairs_count']} ({ov['unique_pairs_ratio']:.1%} of paired entries)")

    header(f"Most Reused Usernames (top {len(d['top_usernames'])})")
    for row in d["top_usernames"]:
        lines.append(f"  {row['username']!r}: {row['pct']:.2f}% ({row['count']} entries)")

    header(f"Most Reused Passwords (top {len(d['top_passwords'])})")
    for row in d["top_passwords"]:
        lines.append(f"  {row['password']!r}: {row['pct']:.2f}% ({row['count']} entries)")

    if d["top_password_entries"]:
        header(f"Vault Entries Sharing the Top {len(d['top_password_entries'])} Most Reused Passwords")
        for row in d["top_password_entries"]:
            lines.append(f"  {row['password']!r} ({row['count']} entries):")
            for entry_name in row["entries"]:
                lines.append(f"    - {entry_name}")

    header(f"Most Reused Username/Password Combos (top {len(d['top_combos'])})")
    for row in d["top_combos"]:
        lines.append(f"  user={row['username']!r} pass={row['password']!r} -> {row['pct']:.2f}% ({row['count']} entries)")

    header("Password Structure Breakdown (by unique password value)")
    for row in d["structure_breakdown"]:
        lines.append(f"  {row['label']}: {row['pct']:.1f}% ({row['count']})")

    ls = d["length_stats"]
    if ls.get("total_unique"):
        header(f"Password Length (by unique password value, {ls['total_unique']} total)")
        lines.append(f"  Min: {ls['min']}  Max: {ls['max']}  Avg: {ls['avg']}")
        lines.append(f"  Shorter than {ls['threshold']} chars: {ls['short_count']} ({ls['short_pct']:.1f}%)")

    header("Risky or Incomplete Entries")
    risky = d["risky_entries"]
    lines.append(f"  Password same as username: {risky['password_equals_username_count']}")
    if risky["password_equals_username_examples"]:
        lines.append(f"    e.g. {', '.join(risky['password_equals_username_examples'])}")
    lines.append(f"  Entries missing a password: {risky['blank_password_count']}")
    lines.append(f"  Entries missing a username: {risky['blank_username_count']}")
    lines.append(f"  Weak unique passwords:      {risky['weak_password_count']} ({risky['weak_password_pct']:.1f}%)")

    if d["duplicate_sites"]:
        header("Duplicate Site Entries (same URI, multiple items)")
        for row in d["duplicate_sites"]:
            lines.append(f"  {row['uri']}: {row['count']} entries")

    totp = d["totp_coverage"]
    if totp:
        header("Two-Factor (TOTP) Coverage")
        lines.append(f"  {totp['with_totp']} / {totp['total']} entries have TOTP configured ({totp['pct']:.1f}%)")

    bc = d["breach_check"]
    header("Breach Exposure (Have I Been Pwned)")
    if bc.get("performed"):
        lines.append(f"  Checked {bc['unique_checked']} unique password(s); {bc['breached_count']} appeared in known breaches.")
        for row in bc["top_breached"]:
            lines.append(f"    {row['password']!r}: seen in {row['breach_count']:,} breaches")
    elif bc.get("error"):
        lines.append(f"  Check failed: {bc['error']}")
    else:
        lines.append("  Not checked. Re-run with --check-pwned to include this in the score.")

    hs = d["health_score"]
    header(f"Vault Health Score: {hs['score']}/100 ({hs['grade']})")
    for finding in hs["key_findings"]:
        lines.append(f"  - {finding}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a live Bitwarden vault (via rbw) for password reuse, "
                    "weak passwords, and other vault hygiene issues.",
    )
    parser.add_argument(
        "--obfuscate", action="store_true",
        help="Mask usernames and passwords in the output.",
    )
    parser.add_argument(
        "--check-pwned", action="store_true",
        help="Check unique passwords against Have I Been Pwned's Pwned Passwords "
             "API (k-anonymity — only a hash prefix is sent, never the password).",
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_N, metavar="N",
        help=f"How many rows to show in top-N lists (default: {DEFAULT_TOP_N}).",
    )
    parser.add_argument(
        "--min-length", type=int, default=DEFAULT_MIN_LENGTH, metavar="N",
        help=f"Passwords shorter than this are flagged as short/weak (default: {DEFAULT_MIN_LENGTH}).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the report as JSON on stdout instead of a text summary.",
    )
    parser.add_argument(
        "--no-sync", action="store_true",
        help="Skip 'rbw sync' and use the local cache as-is.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        ensure_unlocked()
        entries = fetch_vault_entries(no_sync=args.no_sync)
    except RuntimeError as exc:
        log(f"Error: {exc}")
        return 1

    report = build_report(
        entries,
        top_n=args.top,
        min_length=args.min_length,
        check_pwned=args.check_pwned,
    )
    report_dict = report.as_dict()
    if args.obfuscate:
        report_dict = obfuscate_report(report_dict)

    if args.json:
        print(json.dumps(report_dict, indent=2))
    else:
        # Re-wrap the (possibly obfuscated) dict back into a VaultReport for rendering.
        print(render_text_report(VaultReport(**report_dict)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
