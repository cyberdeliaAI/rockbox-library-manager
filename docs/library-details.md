# Album details, health checks and updates

## Read-only album details

Select an album in Library and choose **Album details…** in its side panel.
The existing artwork, tag editor and double-click artwork search remain available.
Tracks are read only when opening this window or choosing **Reload details**;
the fast directory index does not parse tags or maintain a new track database.

The track list shows disc and track numbers, title (or filename when missing),
artist, duration, the decoder's format and available bit-depth/sample-rate/channel
information, genre and date. Selecting a row shows its relative filename,
composer and related diagnostic messages. Missing duration is shown as unknown;
the total covers only readable, known durations when the album is incomplete.

Search ignores case and accents and includes available track metadata, format
and filenames. Filters show missing titles, missing genres, possible metadata
duplicates or unreadable tracks. No tag, folder, artwork or Rockbox database is
written. Concurrent indexed operations use the existing operation lock; retry
with Reload details if another operation is busy. Closing cancels pending reads.

## Additional Library Health diagnostics

- **Possible duplicate track:** the same positive track number and normalized
  title occur on the same disc. Comparison ignores case, whitespace and straight/
  curly quotation marks; version text and other punctuation remain significant.
  Missing/ambiguous numbers and titles are not matched. Existing duplicate-number
  warnings remain separate. This does not compare audio, quality or file contents
  and never deletes files.
- **Possibly incomplete album:** numbered positions below the highest known
  track are absent. An intentional selection or partial album can produce this
  diagnostic; it is not proof that files were lost. Ambiguous or missing numbering
  prevents this inference. A complete readable listing is required: gaps are never
  inferred from a three-track sample or directory/metadata read failures.

Enable **Check every track (slower)** for gap checks across larger albums. Albums
small enough to fit entirely in the default sample can also be checked completely.
Explicit disc numbers keep discs separate; without them physical CD folders are
separate scopes. The album-details window uses the same diagnostic rules.
Existing artist identity, genre and direct-database compatibility checks remain.

## Application updates

**Settings → Application updates → Check now** checks the latest stable release
on the project's public GitHub repository. Optional automatic checks run at
startup, at most once per 24 hours; attempts including failures are cached across
restarts in `updates.json` beside the existing GUI settings. Automatic checking
is off by default and is remembered when selected.

Checks run in the background with timeouts, bounded responses and validated
version tags. No music metadata, source-provider keys, credentials, cookies or
account session is sent; only the public release request is made. Errors leave
library tools available and preserve the last successfully checked version.
The release link is constructed for this repository, not accepted from an API
response. **Download update** opens that release page; choose the package for
your system and follow the [standalone guide](native-builds.md). The application
does not replace itself or install software automatically. Existing settings
and backups keep their established locations.
