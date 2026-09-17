# Music Library

## Structure

Music is organized primarily by **language**, then by **artist** or **genre**.

```
Music/
├── english/          # English-language music
│   ├── dave/         # Dave (UK rapper) — .webm video tracks
│   ├── lana_born_to_die/  # Lana Del Rey
│   ├── santana/      # Santana — .webm video tracks
│   ├── weeknd/       # The Weeknd — .webm video tracks
│   └── [loose files] # Mqx, SICK LEGEND, HARDEST, and other
│                     # hardstyle/sped-up tracks (.opus)
│
├── hungarian/        # Hungarian-language music
│   ├── a_jo_lacibetyar/   # A jó LaciBetyár — .mkv videos
│   │   └── A jó LaciBetyár - Azt Húzd Prímás - FLAC/  # full album
│   ├── azahriah/          # Azahriah — .mkv videos
│   ├── csoves_tom/        # Csöves Tom — .opus tracks + .webm videos
│   ├── fekete_pako/       # Fekete Pákó — 3 full albums
│   │   ├── Fekete Pákó - A csoki a szádban olvad/  (mp3 + opus versions)
│   │   ├── fekete_pako_-_kislany_vigyazz-hu-2003-adr/ (mp3 + opus)
│   │   └── Fekete Pákó - Kombiné (2005)/  (mp3 + opus versions)
│   ├── lil_gumi/          # Lil Gumi — .opus tracks
│   ├── lil_tib/           # Lil Tib — .webm videos
│   ├── magyar_hardstyle/  # Hungarian hardstyle remixes — .opus
│   ├── soundcloud_shit/   # Misc Hungarian SoundCloud tracks (.opus)
│   │                      # NOTE: files ending in _similar.opus are
│   │                      # duplicates of tracks in magyar_hardstyle/
│   ├── Betli Duo - Terített Betli (Non Stop Mulatós Nóták)/
│   ├── Dankó Pista Jubileum (Live) 1999/
│   ├── Lakatos Sándor És Zenekara - Magyar Nóták.../
│   ├── Zengö Együttes - Lakodalmas nóták No1 - FLAC/
│   ├── Zengö Együttes - Lakodalmas nóták No2 - FLAC/
│   └── [loose files]      # Hundred Sins, Huzugha, misc .opus/.mkv
│
├── japanese/         # Japanese music — DJ Okawari, Nujabes, Ryo Fukui
│
├── russian/          # Russian & Caucasian music — NEEL, Kavkaz Lezginka,
│                     # Chechen/Georgian dance music
│
├── instr/            # Ambient / background audio (brown noise etc.)
│
├── other/            # Miscellaneous — gym motivation, Spanish/Latin
│
├── music_videos/     # Full albums & compilations (audio files, despite the name)
│   └── english/      # English albums & collections
│       ├── ACDC - The Best of ACDC/           (.mp3)
│       ├── All Time Top 1000/                 (.mp3)
│       ├── Alphaville - Forever Young (Super Deluxe)/
│       ├── Jazzy Swing/                       (.mp3)
│       ├── Linkin Park - Greatest Hits/       (.mp3)
│       ├── The Blues Collection/              (.mp3)
│       ├── VA-ABC_Of_The_Blues.../            (.flac, 52 CDs)
│       └── Va-Best_Of_2015_(Top_160_Music_Hits)/  (.mp3)
│
└── temp_ytmusic/     # Unsorted downloads — needs to be sorted into
                      # the language folders above (~440 files)
```

## Known Leftovers

- `music_videos/english/` still contains empty artist subdirs (`dave/`, `weeknd/`,
  `santana/`, `lana_born_to_die/`) — their content has been moved to `english/`.
- `music_videos/english/` contains three Fekete Pákó album folders that now only
  hold cover art `.jpg` files — the audio tracks were merged into
  `hungarian/fekete_pako/`.
- `music_videos/hungarian/` contains empty subdirs (`a_jo_lacibetyar/`, `csoves_tom/`,
  `lil_tib/`) — their content has been moved to `hungarian/`.
- `music_videos/other/` is empty.
- `music_videos/temp_ytmusic/` is empty.
