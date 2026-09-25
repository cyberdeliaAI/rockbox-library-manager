# Rockbox database updates and local backups

This development feature is available from **Settings → Rockbox database…**.
It updates existing indexed tracks from their music-file tags after a preview
and confirmation. A verified local backup is mandatory before any database
update, and also before restoring an earlier backup.

Direct updates are experimental until validated on the actual player. They were
developed for a player running build `260920`; the build date alone does not
establish compatibility. The tool checks the database format and structure.

## Workflow

1. Connect the iPod by USB and select its music folder in the app.
2. Edit the music tags as needed. A **Merge in library** decision only affects
   the app; use the tag editor to change the names Rockbox reads.
3. Open **Settings → Rockbox database… → Preview tag update**.
4. Review the changes, then select **Back up and apply** and confirm.
5. Keep the iPod connected until completion. Safely eject it and reboot Rockbox
   so its in-memory index is reloaded.

The update covers artist, album artist, album, genre and year. Artist changes
also update Rockbox's derived canonical-artist index. Folder moves completed by
this version's **Fix folders on disk** are recorded locally and can update the
indexed file paths without guessing which track moved.

New tracks, deleted tracks, unrecorded moves, other metadata fields and artwork
are outside this operation. Use Rockbox's **Update Now** for library additions
and removals. If an indexed file is missing or ambiguous, this tool stops before
writing. It never resolves a missing track by matching its name alone.

## Where backups are stored

Backups are stored on the computer in a `rockbox-database-backups` subfolder of
the app's existing configuration folder:

- Windows: `%APPDATA%\RockboxArtistArt\rockbox-database-backups`
- macOS/Linux: `~/.config/rockbox-artist-art/rockbox-database-backups`
  (or beneath `XDG_CONFIG_HOME` when configured).

The dialog displays the full path and has **Open local backups** and **Back up
now** buttons. Every backup uses a unique UTC timestamp and identifier. Older
backups are retained; there is no automatic rotation or deletion.

Each backup contains the database files, an existing runtime-data export or
pending-update marker, and a manifest with file sizes, SHA-256 checksums, source
device location and Rockbox build information. It contains music-library paths
and metadata, so it should be treated as personal data. Music, artwork, API
credentials and the app's own SQLite database are not included.

All copies are flushed and verified before a backup is marked complete. A
backup failure prevents the database update. Backup destinations inside the
iPod, including symlinks pointing there, are refused.

## Restore and interrupted updates

Select **Restore backup…** and choose `manifest.json` inside a completed local
backup. Review the destination and confirm. The current device database is
backed up first, even when recovering from a failed write. The selected backup
must have valid checksums, a supported complete database, and the same recorded
device location and Rockbox build. If its mount point changed, reconnect it at
the original location first.

Restoring returns ratings, play counts and resume positions to their state at
backup time. It does not undo music-file tag edits or folder moves. A serialized
RAM cache is intentionally discarded after update/restore so stale cached
values cannot replace the restored database.

All replacement files are staged before writing. A dirty master header guards
the transition, and the clean master is installed last. A detected write error
triggers automatic rollback. A power loss or disconnect can prevent rollback:
multi-file replacement is not fully atomic. A durable transaction marker then
blocks further updates and points to the local backup. Reconnect the player
and use **Restore backup…** before trying again. Do not run Rockbox or another
database editor against the mounted device during an update.

## Supported format and invariants

The implementation supports the `TCH` version 16 format (`0x54434810`) in either
byte order. Unknown versions, incomplete tables, pending commits, dirty master
headers, unexpected files, invalid offsets, symlinks and unsupported paths stop
the update. Non-default Rockbox database locations are not supported.

Since version **1.1.0-beta.2**, both `/Music/...` and PodBox's
`/<HDD0>/Music/...` paths refer to the connected iPod's music folder (for example,
`D:\Music\...` on Windows). `<HDD0>` is Rockbox's name for internal ATA volume
zero; see the [volume definitions](https://github.com/anthonyfletcher/podbox/blob/master/firmware/export/mv.h)
and [path handling](https://github.com/anthonyfletcher/podbox/blob/master/firmware/common/pathfuncs.c).
The prefix is preserved in the database, including when applying recorded folder
moves. Other volumes, nested volume labels, Windows drive/stream syntax and
multiple database records that resolve to one local file are refused.

The format contract is based on the official
[tagcache implementation](https://github.com/Rockbox/rockbox/blob/master/apps/tagcache.c)
and [field definitions](https://github.com/Rockbox/rockbox/blob/master/apps/tagcache.h):

- The master has a 24-byte header and 96-byte rows (23 fields plus flags).
- String tables use 12-byte headers and length/index/string entries. Sorted
  entries are padded to eight-byte chunks. Unique tags use Rockbox's bytewise,
  case-insensitive comparison; `<Untagged>` sorts first.
- Only changed string tables are rebuilt. Existing row IDs, deleted-row CRCs,
  runtime fields, serial, commit ID, timestamps and unrelated metadata remain.
- Year is the only numeric field edited. Music bytes are never written by this
  operation. Source file size/mtime and all original database hashes are checked
  again before applying a preview.
- The master RAM-size field is recalculated excluding the filename table, as in
  Rockbox. Non-unique title and filename reverse indices retain their row IDs.

Tests use explicit little/big-endian binary fixtures, real tagged silent FLACs,
and the actual Tk dialog. They cover round-trip recovery, runtime-data retention,
recorded folder moves, stale previews, unsupported inputs, failed backups,
write rollback, simulated disconnection and an interrupted-update recovery.
No real iPod database is used by the tests.

An additional interoperability test runs Rockbox's own host database builder:
it creates a database from real FLACs, the app updates it, then Rockbox itself
rescans a changed track and adds another track. The resulting database retains
the updated tags and seeded play counts, ratings, play time and last-played data.
This passed against upstream commit
`44e7c009ae401e7080b86a4e5e65156d6f68fb13` (2026-09-22). The tagcache and metadata
code was unmodified; the temporary macOS host build used Clang, disabled Apple's
fortify macros for compatibility with Rockbox's string declarations, and disabled
SDL threading for the single-threaded database tool. No native binary is shipped.

To run this optional test with your own built Rockbox database tool:

```sh
ROCKBOX_DATABASE_TOOL=/path/to/database.ipodvideo python -m unittest discover -s tests -p test_native_database.py -v
```
