# PodBox artwork-cache feasibility

Historical research before 1.5.0-beta.1. The implementation and current
constraints are documented in [PodBox preparation tools](podbox-tools.md).

Research snapshot: 2026-11-08, PodBox commit
[`f0850c4`](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635).
This records implementation research, not an enabled cache writer or a claim
of player interoperability. No player files were modified by this research.

## What the functionality does

The desktop prepares the same album/artist thumbnails the player would normally
decode and resize itself. A complete compatible pass also records which database
state the cache covers, allowing the player to skip that background pass. This
is distinct from this application's local GUI thumbnail cache and from saving
Rockbox-compatible `folder.jpg` images.

The functionality can be implemented in the Python/Tk application without an
Electron migration or a Windows executable. That is a feasibility conclusion
from the formats and algorithms, not proof that a replacement currently produces
a complete compatible cache.

## Confirmed format and behavior

| Part | Examined upstream implementation | Consequence |
|---|---|---|
| Location | `library_files.h`: `.rockbox/library/cache/art` | Write only a positively identified PodBox cache, not an ordinary Rockbox directory. |
| Player layout | `LIBRARY_FORMAT` is 5 | Detect the layout and refuse newer/unknown formats. |
| Thumbnails | `art_cache.h`: format 8, `AT` magic, a 12-byte header, native pixels | JPEG resizing alone does not implement this cache. |
| Pixel format | Both `config/ipodvideo.h` and `config/ipod6g.h` select 16-bit RGB565 | Encode the target format independently of the desktop OS; verify byte order against reference files. |
| Sizes | `art_sizes.h`: `coverflow` 128, `list` 44, `wps` 300 pixels | Preserve the player's size names, fitting and fallback geometry. |
| Pixel order | `aa_write_aat()`: square Cover Flow thumbnails use columns; the other sizes use rows | A valid header with the wrong order can display the image incorrectly. |
| Keys | `art_name.c`, `aa_run_pass()`, scheme 2 | Artist keys use the player's name normalization; album keys normally use the database album identity. There are folder-key and spoken-content cases. Existing GUI identity hashes are not interchangeable. |
| Sources | `aa_cache_dir()` and related resolvers | Respect player settings for files/embedded art and central artwork locations. Do not silently use the desktop artwork-search rules instead. |
| Freshness | `aa_art_stamp()` | Stamps combine the exact player path, file size and player-interpreted modification time. |
| Completion | `aa_stamps_save()`, `aa_write_marks()`, `art_cache_tool_run()` | Preserve versioned libfile headers/checksums, scheme records, stamps, database marks and no-art lists. Never mark an incomplete pass as complete. |
| Safe writes | `aa_write_aat()` and libfile writer | Upstream stages `.new` files and renames on success; interrupted operations retain incomplete status. |

The C tool walks an existing player database. It does not create the database
for newly added music. The upstream tool requires that the matching firmware
has booted at least once and that a database exists.

`aa_run_pass()` also visits indexed books and the people/audiobook artwork pass
before deciding whether coverage is complete and removing orphan thumbnails.
A music-only implementation must preserve those other caches and cannot claim
global completion or perform the native full-pass orphan cleanup for them.

The current direct database editor here deliberately supports TCH v16 files
under `.rockbox`; its `_rb()`/`snapshot()` do not implement the newer PodBox
`.rockbox/library/database` location. Cache work must therefore add a separate,
explicitly validated read path rather than silently broadening database-edit or
restore support.

## Why the native executable is not the Mac implementation

The upstream Makefile explicitly reserves its native build for tests and notes
an hour's difference in summer file times. `stubs.c` supplies Windows-specific
time conversion to reproduce the player's FAT timestamp interpretation. A Mac
implementation must establish that interpretation with reference fixtures; it
cannot assume that Python `stat().st_mtime` or a timezone adjustment is equivalent.
This is a documented native-build limitation, not evidence that Python cannot
implement the functionality.

## Implementation route and validation

1. Detect the actual PodBox layout, cache format, target pixel format, database
   identities and source settings. Preview cache coverage and proposed writes
   without reading every audio tag during the normal library scan.
2. Implement the exact keys and an independently tested `.aat` reader/writer.
   Compare deterministic image fixtures with the upstream encoder/reader,
   including non-square/oversized sources, Cover Flow transposition, accented
   names, alternate artist names and different albums with the same title.
3. Establish FAT-time semantics across macOS, Windows and Linux, including
   winter/summer and timestamps from a different timezone. Validate libfile
   checksums, complete/partial marks and missing-art lists with the upstream
   player implementation. Compare against copies, never a personal player.
4. Add a separate Tk **Prepare PodBox artwork cache** operation with a before-write
   preview, verified local backup, staging, cancellation, operation locking,
   changed-source/database checks and restore. Keep music/artwork unchanged and
   never remove existing caches without an explicit reviewed rebuild.
5. Validate with the matching simulator and then the actual supported firmware.
   Prove that the player accepts the cache without rebuilding it and correctly
   detects later artwork changes. Only then enable the writer for those formats.

The `AA_STAMP_NONE` adoption path in `aa_fill()` suggests a possible smaller
prewarming mode: supply correctly keyed thumbnails and let the player establish
freshness marks. It still requires source/key/format validation and a player
walk, and is not equivalent to a fully covered cache. Existing stamps may also
invalidate replacements, so this is a candidate design, not an approved shortcut.

Open inputs for a production implementation are the firmware/layout actually
used, representative reference caches and database fixtures, and validated Mac
FAT-time behavior. Broad support for every Rockbox/PodBox release is not inferred.

Sources examined: [tool](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools/artcache_pb),
[cache implementation](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps-ipod/metadata/art_cache.c),
[format](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps-ipod/metadata/art_cache.h),
[sizes](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps-ipod/metadata/art_sizes.h),
[name keys](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps-ipod/metadata/art_name.c),
[library files](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps-ipod/system/library_files.h).
