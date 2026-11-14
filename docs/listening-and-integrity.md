# Listening history, optional scrobbling and audio integrity

Available in the 1.6.0 beta. The stable release and Homebrew remain on 1.5.0.
The gallery, metadata health checks, tags, artwork/media preparation and both
database workflows retain their existing behavior.

## Local history

Open **Settings → Listening history and statistics…**. Opening loads local data
only. Choose a `.scrobbler.log` exported by your player's scrobbler plugin. The
app does not derive events from aggregate play counts or modify playback data.

1. **Preview log** shows UTC start times, artist, album, title, L (listened) / S
   (skipped), full duration, and invalid/repeated line counts. The first 200
   entries are displayed; import includes all valid reviewed entries.
2. `#TZ/UTC` and explicit `±HHMM`/`±HH:MM` headers are respected. Unknown/absent
   timezones require an explicit historical UTC offset in minutes, e.g. `+60`
   or `-300`. The current desktop offset is never guessed. A fixed offset applies
   to the entire unknown-timezone log; logs spanning daylight-saving changes
   need separate correctly offset input files. Review dates before importing.
   Unknown log versions and timeless logs without real dates are refused.
3. **Import reviewed log** asks for confirmation. A changed source invalidates
   the preview; cancellation rolls back that import. The source log is never
   renamed, archived, truncated or removed.
4. Exact artist/album/title text, UTC start and rating identify an event. Reimport
   or import of an identical copied log does not duplicate those events. Changed
   metadata/timestamps may create different identities; this is not audio
   fingerprinting. Logged identities do not modify library artist aliases.
5. **Local history**, literal search and Previous/Next show pages of 200 events.
   Statistics count imported events, independently of online submissions. Top
   artists/albums/tracks use L events. Listening time **estimates** the sum of full
   track durations; actual elapsed playback is not in the log. Sessions use a
   20-minute gap after estimated track end.
6. **Export…** writes all local history to a new JSON/CSV file. Existing files
   are refused. JSON preserves exact text; formula-like CSV fields receive an
   apostrophe for spreadsheet safety. Times are UTC epochs.

Data is stored separately from the folder index in local `listening.sqlite3`,
with owner-only POSIX permissions. Limits: 64 MiB per log, 16 KiB per line and
250,000 unique local events. Invalid UTF-8/data lines are counted rather than
silently repaired. Back up/export this private data as needed. This is a local
ledger, not an automatic two-way account-history synchronizer.

## Optional manual scrobbling

The **Scrobbling · optional** tab supports Last.fm and ListenBrainz. Sending is
off each time the window opens. No listening data is sent on startup, scan or
import. Libre.fm is not included in this beta.

- **Last.fm:** supply your application's API key and matching shared secret,
  choose **Authorize Last.fm…**, grant access in the browser, then select
  **Finish Last.fm connection**. The existing artwork-only API-key setting is
  unchanged and is insufficient by itself to write scrobbles.
- **ListenBrainz:** enter your user token and choose **Validate and save
  ListenBrainz token**. This explicit validation request sends no listens.
- Credentials are separate in private `scrobbling.json`, using atomic owner-only
  POSIX writes. Secrets are not included in activity logs. **Disconnect selected
  service** removes local credentials; history/receipts remain. Revoke remote
  authorization on the service itself. This beta uses private files, not an OS
  keychain. Corrupt credentials do not prevent local history use.

Load local history, select rows, choose a connected service and enable manual
scrobbling. **Review selected history rows** lists eligible/excluded events.
**Send reviewed selection…** asks for explicit confirmation of count/account and
the data disclosed: artist, album, title, duration and UTC start. At most 200
events per review are supported. Only L events with artist/title and a duration
of at least 30 seconds qualify. The L flag comes from the player; actual elapsed
playback cannot be independently established from this log. Future events are
excluded. Last.fm also excludes events older than its 14-day window. ListenBrainz
imports retain original times. Optional log MBIDs remain local: the app does not
assume they identify recordings correctly for submission.

Receipts are scoped by service and validated username, independently of token
rotation. Last.fm replies are checked per track, including ignored codes,
timestamps and totals. HTTP/API success alone is insufficient. Accepted events
cannot be resent through the normal workflow. Ignored receipts remain visible;
select a history row for its detail. Service/account changes do not affect stats.

A durable **unknown** receipt reserves events before each request. A timeout,
malformed response, process interruption or ambiguous outcome is never retried
automatically: the service may have accepted it. Check the online profile and
wait for any request to finish before choosing **Allow retry of selected
unaccepted rows…**, which warns about duplicates. Accepted receipts cannot be
cleared by this action. Cancellation stops between batches; an in-flight request
first records its outcome. Exactly-once remote delivery cannot be guaranteed
across network/process failures.

Fixed HTTPS endpoints, timeouts, bounded replies and disabled redirects/proxies
are used. Tests simulate service replies; they do not submit to a real account.
Live account interoperability needs beta feedback.

References: [portable-player log](https://github.com/anthonyfletcher/podbox/blob/f0850c4647e7c1cfde11ffc89ab40b55f8aa5635/apps/plugins/lastfm_scrobbler.c),
[Last.fm authentication](https://www.last.fm/api/authspec),
[Last.fm scrobbling](https://www.last.fm/api/scrobbling),
[ListenBrainz API](https://listenbrainz.readthedocs.io/en/latest/users/api/core.html).

## Audio integrity: separate full decoding

Open **Settings → Check audio integrity…**, choose a music folder and select
**Start full decode**. External FFmpeg is required; the executable selected in
Settings is used, falling back to PATH. Normal scans and artwork still work
without FFmpeg. This operation never starts as part of a normal scan or Health.

Every audio stream in supported FLAC, MP3, M4A/MP4, AAC, Ogg, Opus, WAV, AIFF,
WavPack and WMA files is decoded with strict errors and null output. There is no
repair, normalization or music-file output. Input network protocols are disabled.
Links are skipped, hidden directories and OS metadata folders excluded, and
unreadable entries reported. Per-file timeouts are five minutes; diagnostics are
bounded. Cancellation kills the active decoder and marks its result incomplete.
A source changed during decoding cannot receive a successful result.

Results distinguish passed, error, warning, changed, skipped, unreadable and
incomplete. An error is a decoding failure, not a proven diagnosis: unsupported
codecs can fail too. Passing does not establish lossless provenance, bit-perfect
identity, clipping/mastering quality or absence of audible defects. Reference
FLAC MD5 and custom MP3 frame-count tests are not included in this beta.

A report covers up to 100,000 entries; the window displays at most 1,000.
**Export report…** writes all results and cancellation status to a new JSON file.
Large libraries/slow players can take considerable time. Existing conversion
verification and metadata health checks remain unchanged.
