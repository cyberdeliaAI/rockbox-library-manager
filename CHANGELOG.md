# Changelog

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
