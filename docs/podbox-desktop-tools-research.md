# PodBox desktop tools: implementation research

Historical feasibility review before 1.5.0-beta.1. For the implemented workflows,
verification and limitations, see [PodBox preparation tools](podbox-tools.md).

Reviewed 2026-11-08 using the actual tool sources, the relevant player routines
and documentation in [PodBox](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools).
This is a feasibility/priority review, not an enabled database, artwork-cache or
sound-index writer. No player was attached or modified during this research.

## Database update and rebuild

`tools/database_pb/database_pb.c` compiles the player's `apps-ipod/database/tagcache.c`
and invokes `tagcache_tool_run()`. It updates an existing database or rebuilds
it, reads the player's scan settings and stores player-relative `/<HDD0>/...`
paths. This is substantially broader than our existing database tag updater.

| Behavior | Existing Rockbox Library Manager | `database_pb` functionality |
|---|---|---|
| Existing records | Reviewed tag/path updates, retaining row IDs and runtime fields | Incremental scan/merge of the player database |
| New tracks | Left to the player | Scanned, read and added |
| Changed tracks | Only reviewed supported fields | Detected and reread through the player's metadata readers |
| Missing tracks | Refuse unrecorded missing/moved tracks | Checks removals, with special handling for incomplete walks |
| Fresh/rebuilt database | Not implemented | Supported after the player has initialized the matching library layout |
| Runtime data | Preserve existing numeric data in retained records | Save/import play count, rating, play time, last played, arrival commit and resume position/offset |
| Storage | `.rockbox` TCH files | `.rockbox/library/database`, with versioned library/user/cache files |
| Recovery | Verified local backups, staged installation, stale checks and an application journal | Native temp/`.new`/swap-marker recovery and runtime export/import |

Both currently use TCH magic `0x54434810` (v16) and the examined real-tag enum
has 23 fields. The common binary family is useful, but is not evidence that
changing a directory path makes all scan, runtime and recovery behavior
compatible. Our current `_rb()`/`snapshot()` intentionally do not support that
new storage location, new/deleted records or a full rebuild.

The examined entry points provide concrete safeguards: the CLI checks the
library layout against `LIBRARY_FORMAT` (5), the engine refuses an unsupported
master magic, pending swaps/temp commits are handled first, and a failed runtime
export write stops the rebuild before deletion. Runtime reimport is intended to
preserve user statistics; that intention is not a guarantee for corrupted files,
ambiguous identity mapping or failed writes without fixture tests.

The native Makefile explicitly describes its build as test-only because summer
timestamps differ by an hour. Windows stubs reproduce the player's FAT-time
interpretation. A Mac feature must solve and validate that behavior rather than
run this native build unchanged. The tool also respects `database scan paths`,
`year from folder` and `skip ipod games`; the ordinary GUI index scan must remain
the separate, fast directory-only operation it is today.

Two implementation routes merit evaluation: port/build the shared player engine
with validated host/filesystem adapters, or implement a dedicated Python writer
with reference-format interoperability tests. Neither means launching a Windows
EXE as the Mac feature. The new operation should remain separate from the
existing conservative editor and preserve its supported workflows.

Before enabling writes, test temporary libraries for add/change/delete/rebuild,
duplicate and renamed tracks, multi-value metadata, byte order, known seeded
runtime data, interrupted swaps, failed writes, unplugged/unreadable folders,
unknown layouts and FAT timestamps across seasons/timezones. Back up and verify
the actual library database plus affected user/cache/recovery files, including
runtime exports; the existing old-layout backup manifest is not automatically
sufficient. Require reviewed plans and recovery, not an implicit rebuild.

## Further useful tools

| Tool | Useful application functionality | Constraints observed in code |
|---|---|---|
| `soundscan` | Prepare PodBox's audio-analysis index on the desktop: tempo, loudness in dBFS, pitch/chroma and other analysis fields | Writes `library/sound.dat`; uses the matching host codecs and player's analysis algorithm/window. Standard LUFS or ReplayGain rewriting is not what this code implements. |
| `eq_refit` | Review/refit an existing Rockbox EQ preset onto fewer active bands, showing response error and exporting a new preset | Requires NumPy. Results depend on sample rate and configured error tolerance; supports separate output, dry-run and explicit in-place modes. Preserve originals and preview the curve/precut. |
| `dbfeat` / `db_featured_parse.c` | Guest-credit suggestions such as `(feat. X)` in titles/artists for browsing or reviewed metadata assistance | A heuristic parser with a known-artists callback; unknown band names can be split incorrectly. Suitable for suggestions, not automatic tag rewriting. |
| `art_fetch` | Existing artist/album artwork lookup and output | Already the basis of this application's artwork engine. All 67 upstream top-level function/class names are represented here; 46 AST implementations match exactly. This is not proof that every later behavior change is already adopted. Preserve our added safeguards/providers when comparing changes. |
| `pfgeom` | Developer verification of the player's Cover Flow projection/culling | Geometry test harness, not an artwork-cache builder or a ready-made Tk carousel feature. |

The analysis code reads a bounded part of each track (the examined soundscan
window is 40 seconds, with minimum-length rules) and writes separate analysis
records. It uses a size-based fallback when it cannot reproduce FAT time.
Its finalization deliberately does not prune records merely because its folder
walk failed to visit them all. A future integration should preserve those
distinctions and avoid mixing sample-based metrics with whole-track guarantees.
Its advertised dry mode still prepares a working index/temporary state in the
implementation, so it must not be assumed byte-for-byte read-only on a player.

The standalone `dbfeat` harness was compiled and run on this Mac against the
actual parser: **56 cases, 0 failures**, including its two documented debatable
choices. This validates those fixed parser cases, not the account/library
knowledge or the accuracy of every possible credit. The other native writers
were source-reviewed, not run against a player. EQ fitting was reviewed but
not benchmarked or listening-tested here.

Most remaining tools are for firmware building, graphics/fonts, diagnostics or
bootloaders. In particular `codecscan.pl` reads compiled codec memory maps,
`gen_about_art.py` generates the firmware About-page artwork, and `songdb.pl`
declares the old TCH v6 format. `tools/database` is the upstream database tool;
the fork's own Makefile explicitly distinguishes `database_pb` from it. Those
are not interchangeable database builders for current PodBox.

## Suggested order

1. A validated player-database update/rebuild feature, preserving the current
   tag updater as a separate operation.
2. The [PodBox artwork-cache generator](podbox-art-cache-research.md), using the
   actual current player database and format. Newly added music needs its
   database update first.
3. Optional sound-index preparation. It can measure without an existing tag
   database; its place here is a product priority, not a hard database dependency.
4. Optional EQ preset refitting and guest-credit suggestions.

Further work needs the actual target firmware/model/layout, representative copied
fixtures, validated cross-platform timestamp behavior and player/simulator
interoperability checks. This document does not broaden supported formats or
authorize automatic data repair.

Sources: [database tool](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools/database_pb),
[tagcache engine](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps-ipod/database/tagcache.c),
[soundscan](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools/soundscan),
[EQ refit](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools/eq_refit),
[guest-credit tests](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools/dbfeat).
