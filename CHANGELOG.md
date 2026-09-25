# Changelog

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
