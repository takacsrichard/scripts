#!/usr/bin/env bash
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find "$BASE_DIR" -type f \
    -not -path "*/transcoded_downloads/*" \
    -printf "%s\t%P\n" \
| awk -F'\t' '
BEGIN {
    split("mp3 flac opus wav aac ogg m4a wma ape wv alac", a, " ")
    for (i in a) audio_exts[a[i]] = 1

    split("mp4 mkv avi mov webm m4v wmv flv ts mts", v, " ")
    for (i in v) video_exts[v[i]] = 1

    total = 0
}
{
    size = $1
    path = $2
    total += size

    # extension — split basename on "." to avoid dots in dir names
    nb = split(path, bparts, "/")
    ne = split(bparts[nb], eparts, ".")
    ext = (ne > 1) ? tolower(eparts[ne]) : "(none)"
    ext_size[ext] += size

    # first-level folder
    folder = (nb > 1) ? bparts[1] : "(root)"
    folder_size[folder] += size

    # audio / video / other
    if (ext in audio_exts)      type_size["audio"] += size
    else if (ext in video_exts) type_size["video"] += size
    else                        type_size["other"] += size
}
function human(b) {
    if (b >= 1073741824) return sprintf("%7.1f GB", b / 1073741824)
    if (b >= 1048576)    return sprintf("%7.1f MB", b / 1048576)
    if (b >= 1024)       return sprintf("%7.1f KB", b / 1024)
    return sprintf("%7d  B", b)
}
function bar(pct,   n, s, i) {
    n = int(pct / 4)
    s = ""
    for (i = 0; i < n; i++) s = s "█"
    return s
}
function sort_desc(size_arr, keys,   n, i, j, tmp) {
    n = 0
    for (k in size_arr) keys[n++] = k
    for (i = 0; i < n-1; i++)
        for (j = i+1; j < n; j++)
            if (size_arr[keys[j]] > size_arr[keys[i]]) {
                tmp = keys[i]; keys[i] = keys[j]; keys[j] = tmp
            }
    return n
}
END {
    printf "\n── Total: %s ───────────────────────────────────────────────────\n", human(total)

    printf "\n── By Extension ─────────────────────────────────────────────────\n"
    n = sort_desc(ext_size, ext_keys)
    for (i = 0; i < n; i++) {
        e = ext_keys[i]
        pct = (ext_size[e] / total) * 100
        printf "  %-10s  %s  %5.1f%%  %s\n", "."e, human(ext_size[e]), pct, bar(pct)
    }

    printf "\n── By Type ──────────────────────────────────────────────────────\n"
    n = sort_desc(type_size, type_keys)
    for (i = 0; i < n; i++) {
        t = type_keys[i]
        pct = (type_size[t] / total) * 100
        printf "  %-10s  %s  %5.1f%%  %s\n", t, human(type_size[t]), pct, bar(pct)
    }

    printf "\n── By Folder ────────────────────────────────────────────────────\n"
    n = sort_desc(folder_size, folder_keys)
    for (i = 0; i < n; i++) {
        f = folder_keys[i]
        pct = (folder_size[f] / total) * 100
        printf "  %-20s  %s  %5.1f%%  %s\n", f, human(folder_size[f]), pct, bar(pct)
    }
    printf "\n"
}
'
