# Changelog

## 1.5.0

Stable release of the features evaluated in 1.5.0-beta.1 and 1.5.0-beta.2.
Application behavior is unchanged from beta 2; this release updates the version
and release documentation.

- Include Rockbox preparation tools for database update/build/rebuild, native
  artwork-cache preparation and soundscan, with reviewed local output, verified
  complete-library backups and journaled installation/recovery.
- Include EQ Refit with new-file preset export and read-only guest-credit
  suggestions in Album details. Preserve the existing Tk interface and workflows.
- Keep the explicit PodBox layout-5 and iPod Video/Classic requirements for player
  preparation. Keep the conservative Rockbox database tag updater separate.
- Publish Windows x64, Linux x64 and Apple-signed/notarized macOS Apple Silicon
  and Intel downloads, with checksums and the automatic stable Homebrew update.

## 1.5.0-beta.2

Second test release. The stable release and Homebrew channel remain on 1.4.0.

- Rename the preparation tools to Rockbox in Settings and the dialog, retaining
  the explicit PodBox layout-5 compatibility requirements.
- Clarify that installation reuses the preview's prepared files without rerunning
  the native engine; input checks, verified backups and installation checks remain.
- Verify reuse of prepared database, artwork and soundscan output in the native
  tests, including rebuilds, without rerunning the engines during installation.

## 1.5.0-beta.1

First test release of the five PodBox extensions. The stable Homebrew channel
remains on 1.4.0 while this prerelease is evaluated.

- Add separate PodBox preparation tools using pinned player engines: build/update
  or rebuild the layout-5 database, prepare format-8 artwork caches and run
  soundscan with matching host codecs. Keep the existing Rockbox tag updater.
- Run native previews in a local library overlay. The C filesystem adapter denies
  all source/player mutations. Review the resulting files before installation;
  verify local backups, unchanged inputs, output formats and retained runtime data.
- Stage complete library installations, journal the directory swap, recover
  interrupted swaps and restore verified backups. Music files are never changed.
- Freeze the FAT mount offset for previews. Verify the Mac adapter against raw
  winter/summer FAT directory timestamps; Linux database/artwork writes require
  an explicit FAT mount timezone. Soundscan records unknown mtime as zero.
- Add EQ Refit with curve/error/precut review and exclusive export to a new preset,
  preserving originals, comments, unrelated settings and line endings. Add NumPy.
- Add read-only guest-credit suggestions and an Album details filter, porting
  PodBox's known-artist parser and its 56 fixed reference cases.
- Bundle native engines/codecs and corresponding adapted source; exercise them
  after extraction alongside existing GUI/CLI/signing/Homebrew verification.

## 1.4.0

- Keep the existing Tk interface and add explicit, read-only artist biographies and
  album background from TheAudioDB. Opening the view makes no request; exact
  identities, bounded local text caching and a separate source link are used.
- Sign official macOS Apple Silicon and Intel builds with Developer ID and the
  hardened runtime, notarize with Apple and staple the accepted ticket. Verify
  the extracted GUI, CLI, signatures and Gatekeeper before writing checksums.
- Require archive-bound signing/runtime evidence for both Mac downloads before
  release attachment. Preserve ordinary ad-hoc PR builds and older tag recipes.
- Preserve existing artwork rules, tag review, database recovery, CLI commands,
  optional update checks, external FFmpeg and automatic Homebrew publishing.

## 1.3.0

- Add Settings > Application updates: manual stable-release checks and optional
  automatic checks at startup, limited to once per day including failed attempts.
  Use GitHub's public API without source keys or credentials; open release pages
  in the browser instead of installing updates automatically. Existing users
  keep automatic checking off until they enable it.
- Add a read-only Album details window with on-demand track metadata, disc/track
  order, duration, format information, genres, year, composer and file details.
  Search across tracks and filter missing titles/genres, possible duplicates and
  unreadable tracks. Preserve the fast folder index and existing tag/artwork tools.
- Add Library Health diagnostics for same-disc numbered title matches and
  possible numbering gaps. Preserve existing duplicate-number warnings. Never
  infer gaps from sampled or incomplete reads, or compare separate untagged disc
  folders as one disc. Matching metadata does not establish identical audio.
- Exercise the new dialogs, failure/cancellation paths, update caching and
  temporary-library read-only behavior in regression and frozen package checks.

## 1.2.2

- Add native PyInstaller GUI/CLI downloads for Windows x64, Linux x64, and
  macOS Apple Silicon and Intel. Bundle Python, Tk, runtime libraries and
  drag-and-drop support; keep source installations and external FFmpeg support.
- Build and test extracted archives on GitHub Actions before attaching them,
  checksums and the existing source ZIP to a release. Include project and
  dependency license notices, and reuse the existing application icon.
- Verify the frozen GUI, native drag and drop, SQLite, metadata, JPEG processing,
  backups, CLI dispatch and real FFmpeg conversion against temporary data.
- Restore the system library search path for external FFmpeg and the file manager
  in frozen Linux builds. Source runs keep their existing environment behavior.

## 1.2.1

- Check duplicate-folder destinations across all source folders before moving
  anything, and refuse destinations that appear during the merge. Preserve
  identical-file deduplication and alternate artist artwork.
- Report interrupted folder merges accurately, retain completed entry moves for
  Rockbox database updates, and rescan partially changed libraries.
- Keep the previous library index and indexed root after directory read errors
  or cancellation. Publish successful scans in one SQLite transaction; report
  incomplete scans instead of showing them as complete.
- Keep scan markers unique when the system clock does not advance between
  scans, so deleted folders are still removed from the index on Windows.
- Tie indexed operations and pending artwork to the scanned music root. Refuse
  stale-root saves, serialize manual saves with other operations, and avoid
  applying late save results to a different library index.
- Handle separator-only genre tags without aborting Library Health.
- Cache the artwork candidate actually selected, including interactive retries
  and multiple images from the same provider.
- Fall back to online album artwork when embedded artwork is rejected, while
  retaining embedded-first selection and existing cache controls.
- Redact API keys and tokens from artwork-provider error messages.
- Restore four media regression tests that were outside their test class, and
  add regression coverage for the corrected folder, scan, artwork and UI paths.
- Reserve space for media results at high display scaling, including 200% on
  Linux, while keeping the dialog within the screen size.
- Add the approved AGENTS.md repository instructions for future development.

## 1.2.0

Releases the media preparation from 1.2.0-beta.1 together with:

- Find artwork in more places without a key: Cover Art Archive release groups
  (1200 px), Deezer covers and artist pictures, and Apple Music covers. Discogs
  covers and artist pictures with a personal token. The sources are adapted from
  Tagcast and share one paced client, so MusicBrainz still gets one request a
  second.
- Search without edition text such as "(2018 Remaster)" and match names without
  regard to accents. Automatic fetches only accept close artist/album matches.
- Add online year and genre suggestions to the tag editor, and a review of all
  changes (grouped before → after) before tags are saved. Unchanged tracks are
  no longer rewritten.
- Show every value of a tag in the editor and always save one value per field;
  refuse years the Rockbox database update can't read.
- Add Library Health checks for several values in one tag, unreadable years,
  combined genres, genre spellings Rockbox lists separately, and missing years,
  genres and track numbers, with an option to check every track.
- Point database-update errors caused by tags to the Health check that lists them.
- Media preparation: rewrite progressive JPEG artwork as baseline, replace small
  artwork with a larger online source (with backups), keep the scan cache valid
  after reconnecting the player, and report leftover temporary files. The
  results list uses the dark list style (its rows were white with light text),
  and one **Select rows** menu replaces the selection buttons so the dialog fits
  at 200 % scaling on Linux.
- Store a Discogs token; drop the unused Last.fm shared secret. Credentials are
  written atomically and readable by the owner only. Downloaded images are
  limited to 25 MB.
- Split the desktop code into modules (library index, Health, tag editor, picker,
  folders, theme and widgets), add `pyproject.toml` and ruff, and run the tests
  on Windows, macOS and Linux.

## 1.2.0-beta.1

- Add Settings → Prepare media for PodBox with a separate, cancellable scan
  of FLAC headers and existing `folder.jpg` / `cover.jpg` dimensions.
- Choose a specific folder (including subfolders), even outside the library,
  to prepare just those files without scanning the full music collection.
- Cache unchanged file information locally; changing the artwork size
  reevaluates cached dimensions without decoding images again.
- Convert selected mono/stereo hi-res FLAC to 16-bit / up to 44.1 kHz using
  an optional FFmpeg installation. Preserve tags and embedded covers, verify
  output format, duration and decoding, then replace each original atomically.
- Resize large artwork to the configured square JPEG size, with centre cropping
  for non-square images. Flag small artwork for replacement using online search;
  never automatically upscale it.
- Add configurable local original-media backups, enabled by default, with
  checksum verification and a recovery manifest. Users with their own backups
  can disable them in Settings; the apply confirmation shows that choice.
- Refuse stale previews, damaged conversions, linked paths, multichannel audio
  and FLAC with cuesheet/application metadata that cannot safely be preserved.
- Keep the current file unchanged after cancellation or verification failure;
  report completed files and individual errors. Add Windows FFmpeg integration tests.
- Recheck actual tags before database writes so edits with unchanged file size
  and timestamps cannot silently reuse an outdated database preview.

This beta has not yet been validated on a physical iPod. High-resolution FLAC
is a conversion candidate, not a definitive playback-failure diagnosis.

## 1.1.0

- Release the Rockbox database dialog with tag-change previews, mandatory
  verified local backups, manual backups, and recovery controls.

- Fix database previews for PodBox paths such as `/<HDD0>/Music/...`, mapping
  the internal volume to the connected iPod on Windows, macOS and Linux.
- Preserve the original database path and its volume prefix, including after
  recorded folder moves. Refuse other volumes, ambiguous aliases and paths
  that could escape the device or access a Windows alternate data stream.
- Include prominent credits to Anthony Fletcher and PodBox, the original
  artwork source references, and the full GNU GPL v2 license.

Direct database updates remain experimental until validated on the actual player.

## 1.1.0-beta.1

- Preview updates to existing Rockbox database records after editing music tags.
- Save mandatory, verified, timestamped database backups on the computer before
  updates and restores, with manual backup and recovery controls.
- Retain runtime data and track identities; update recorded folder-move paths.
- Block unsupported or inconsistent databases and stale previews; roll back
  write errors and retain recovery information after interrupted updates.
- Serialize tag saves with database operations to avoid concurrent edits.

Direct database updates are experimental pending validation on the actual iPod.

## 1.0.0

First public release of Rockbox Library Manager, including the desktop library
browser, artwork engine, Library Health, duplicate-artist resolution and album
tag editor.

The application and command line share one version number. Platform launchers
support a local `.venv` installation.
