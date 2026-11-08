# PodBox preparation tools

Available in **Settings → PodBox preparation tools…**. The existing **Rockbox
database…** tag updater is unchanged and remains a separate operation.

## Supported player data

- iPod Video (`ipodvideo`) and Classic (`ipod6g`), identified by `rockbox-info.txt`.
- PodBox must have booted and initialized library layout **5** in
  `.rockbox/library/format.dat`. Older/newer layouts are refused.
- Database: TCH v16, written by the pinned player's own database engine, with
  player-relative `/<HDD0>/...` paths. Primary volume only.
- Artwork: native RGB565 `.aat`, format **8**, 128px Cover Flow (column order),
  44px list and 300px WPS (row order). The engine handles exceptional elongated
  images, album/artist hashes, stamps, fallback images and completed-pass marks.
- Sound: index v3 with 64-byte records. Matching desktop codecs are bundled for
  FLAC, WAV, MP3, AAC, Vorbis, Opus, ALAC, AIFF and WavPack. Other formats can be
  reported unreadable. The ARM codecs on the player cannot be used by a desktop.

The compiled engines come from [PodBox
f0850c4647e7c1cfde11ffc89ab40b55f8aa5635](https://github.com/anthonyfletcher/podbox/tree/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/tools).
Format compatibility is checked, rather than assuming all firmware called
Rockbox/PodBox uses these layouts. Pending/dirty or unrecognized databases should
finish/recover on the player first; the new tool refuses to overwrite them.

## Workflow

1. Select the mounted player's root containing `.rockbox`.
2. Choose **Update / build database**, **Rebuild database**, **Prepare artwork
   cache** or **Soundscan**. Update/build adds new tracks, rereads changed tracks
   and handles deleted tracks using the player's engine and its scan settings.
   Rebuild can renumber rows. Runtime data is checked for retained exact paths;
   renamed/deleted tracks are not promised the same history.
3. **Prepare local preview** copies only `.rockbox/library` to local storage.
   Native engines read music in place, but their filesystem adapter denies every
   source mutation. All writes are redirected into the local library overlay.
   Music is not copied, retagged or converted. No network request is required.
4. Review the actual changed file list and native report. A cancelled or failed
   preview never installs anything. Build/update the database before artwork for
   new albums. Separate previews let you review each operation's effects.
5. **Back up and install preview…** checks unchanged music metadata, library
   hashes, configuration, timezone and staged output. It creates and verifies a
   complete local library backup, then stages a complete library on the player
   and journals its directory swap. Keep the player connected until completion.
6. Safely eject and reboot PodBox to load the changes.

There must be room locally for a preview and retained backups, and on the player
for a staged library. The library backup limit is 16 GiB / 500,000 files; input
walks are limited to 500,000 visible entries. Symlinks, unsupported targets,
incomplete reads and ambiguous case-insensitive paths are refused. Soundscan
can take a long time for large libraries. Cancellation applies to local preview,
not to installation/restoration; wait for those operations to finish.

The normal fast GUI index scan still does not read music tags or build a player
database. Library aliases and "Keep separate" choices are unchanged.

## FAT timestamps

The player's database and image stamps store FAT wall-clock time, not a normal
UTC epoch. On macOS and Windows the adapter adds the current host offset to the
host's file timestamp, frozen for each preview. Existing database times are
checked for a clear mismatch. If the timezone or mount changes, remount and
preview again. Do not use a `TZ` override that differs from the mount's timezone.
Mac verification used raw winter/summer directory entries in a temporary FAT
image, rather than just the host's reported dates.

On Linux the implicit kernel timezone is not inferred from Python's timezone.
Database/artwork preparation requires a FAT mount with an explicit `tz=UTC` or
`time_offset=<minutes>` option. The latter is the number of minutes subtracted
from FAT local time to obtain UTC; see the [kernel VFAT
documentation](https://docs.kernel.org/filesystems/vfat.html). Set this to the
actual mount/player convention; the app does not remount a drive automatically.
Non-FAT test copies can use an explicit `timestamp_offset` through the Python API.
Native tests do this for their artificial temporary fixtures.

Soundscan deliberately stores mtime **0** (unknown) and uses size for freshness,
as upstream does; it needs no FAT timezone selection. Measurements cover a
bounded portion of each sufficiently long track, not the whole song. Loudness
is **dBFS**, not LUFS or ReplayGain. No audio loudness/tag normalization happens.
The upstream algorithm retains unvisited sound records instead of inferring
deletions from an incomplete walk.

## Backups and recovery

Backups live in the application's local data folder under
`podbox-library-backups`, each with a verified `manifest.json` and complete
`library/` snapshot. They include database, user data, art cache and sound index.
They are retained until you remove them yourself. They contain library names and
personal play data; store them accordingly. **Open backups** shows their location.

If installation stops, keep the player connected and choose **Recover interrupted
installation…**. The journal is outside the swapped library. Recovery verifies the
local backup and known states; it rolls back a gap between directory renames or
finishes cleanup of an already verified installed state. Unknown/changed files
are retained for manual inspection rather than deleted. A partial copied file
is retained in a separate `.rlm-library-inspect-*` directory for manual inspection.

**Restore backup…** verifies a selected local manifest for the same player path,
backs up the current library and installs the selected snapshot. Recover a pending
transaction first. Restoration does not undo music tag edits or folder moves.
If the player was mounted under a different path, do not edit manifests to bypass
the check: preserve the backup and inspect the mapping before manual recovery.

## EQ Refit and guest credits

**Settings → EQ Refit…** uses PodBox's numerical fitter (NumPy) on a ten-slot EQ
preset. Select a maximum band count, tolerance and 44.1/48 kHz sample rate, then
review original/refitted curves, weighted/raw errors and peak-boost/precut advice.
An over-tolerance fit cannot be exported. Export uses a **new** `.cfg` filename;
existing files and the original are never overwritten. Enable/precut settings,
comments, unrelated keys, BOM and newline convention are preserved. The ten band
slots are written explicitly so unused slots cannot inherit old filter gains.
Numerical approximation does not establish listening quality or actual device
CPU savings. No automatic preset installation or in-place replacement is offered.

**Album details… → Guest credits** shows suggestions inferred from the first
`feat.`, `ft.`, `featuring`, `w/`, `f/` or `duet with` marker in title/artist text.
Known library artist names help preserve band names containing separators.
Unknown names can still split incorrectly; suggestions never rewrite tags.

## Source installations and native builds

EQ and guest suggestions work with normal Python requirements. The three native
engines are included in standalone downloads; source installations need to build
them separately. Install a C compiler, make, Perl and SDL2 development/configure
files. Windows uses the MinGW64/MSYS2 toolchain and a static CRT; macOS uses Clang.

From a **disposable** clean PodBox checkout at the pinned commit:

```sh
git clone https://github.com/anthonyfletcher/podbox /tmp/podbox-engine
git -C /tmp/podbox-engine checkout f0850c4647e7c1cfde11ffc89ab40b55f8aa5635
python scripts/build_podbox.py --source /tmp/podbox-engine
RLM_TEST_OVERLAY_PROBE=1 python -m unittest discover -s tests -p test_podbox_tools.py -v
python scripts/build_app.py
```

The build applies exact host/overlay patches, generates needed headers, and
builds engines plus nine codecs into `build/podbox`. It fails if the pinned
checkout differs. `RLM_PODBOX_TOOLS` can select an explicitly built engine folder
for source runs; it is never searched for in the music folder or on PATH.

Every native download includes the corresponding adapted source and original
license notices in `licenses/PodBox/source.tar.gz`. It contains the required
source trees, generated headers and the build/overlay recipe. To rebuild that
already adapted archive, unpack it, use its included `rlm/scripts/build_podbox.py`
with `--source <unpacked>/podbox --prepared --destination <new-output-folder>`.
Keep the output outside the unpacked source tree.

CI builds all four desktop targets and runs native fixtures before freezing.
The extracted GUI/CLI then build a temporary database, create native artwork,
decode/analyse audio through a host codec and exercise EQ export. Mac signing,
notarization, Gatekeeper checks and automatic Homebrew publication remain in
place. Tests do not establish compatibility with every physical firmware/player;
no real player was modified during development.
