# Prepare media for PodBox

Version 1.2.0-beta.1 adds **Settings → Prepare media for PodBox → Scan and
prepare…**. This is a separate maintenance scan; the normal library Rescan
stays focused on the artist/album index.

## Scan first

Choose the music folder and artwork size in Settings, then open **Scan and
prepare…**. You can scan FLAC, artwork, or both. Scanning does not modify media.

Already know which folder needs attention? Click **Choose folder…** to inspect
just that folder and its subfolders immediately, without scanning the whole
library first. It can also be a folder elsewhere on your computer. Leave only
**FLAC** checked if you only want to inspect music. The short header check still
identifies which files need conversion; standard-resolution FLAC is skipped.
**Whole library** resets the scope. Choosing a maintenance folder does not
change the application's main music-folder setting. If the configured library
is disconnected, opening this tool lets you choose another folder directly.

The scan visits the selected folder recursively. For standard FLAC it reads
only the first 42 bytes (the STREAMINFO header), and for artwork it reads image
dimensions. It does not decode entire tracks or images. A first pass still has
to open each relevant file, so a large library on a slow USB connection can
take time. Later scans reuse a local cache when file identity, size and
timestamps match. **Ignore cache** forces fresh header reads. This is an
inspection of properties, not a complete audio-integrity scan.

FLAC above 16 bits or 44.1 kHz appears as a conversion candidate. This does not
mean every hi-res file is unplayable: support and performance depend on the
player and firmware. The scan uses file properties, not the download service
or filename, so a standard 16-bit/44.1 kHz FLAC from Tidal is left alone.

Artwork inspection covers existing `folder.jpg` and `cover.jpg` files, including
capitalized filenames. Embedded covers and unrelated images are not resized.

## Select and apply

Use Ctrl/Shift to select rows, or **Select FLAC**, **Select large artwork** or
**Select both**. No files are selected automatically after a scan. The final
confirmation lists the operation and whether original backups are enabled.

- **FLAC:** convert to 16-bit at 44.1 kHz, preserving lower sample rates and
  mono/stereo channels. Resampling and triangular dithering reduce the audio
  resolution; the result is still FLAC, but it is not a bit-for-bit copy of
  the high-resolution original. Tags (including custom/multi-value tags) and
  embedded pictures are preserved and checked. Output format, sample count
  and a full decode are verified before replacement.
- **Large artwork:** resize to the square size in Settings, such as 300×300
  or 500×500. Non-square images are centre cropped. The result is a baseline
  JPEG. Use the regular artwork editor instead if you want to choose the crop.
- **Small artwork:** either dimension below the chosen size is too small.
  For example, 200×200 needs replacement when the setting is 300×300. It is
  reported separately and never automatically enlarged. Select one row and
  choose **Find better artwork…** to open the existing artwork search, choose
  a sufficiently large source, then save it from the artwork panel. The
  artist/album must be in the library index; run the normal Rescan if needed.

Multichannel FLAC is reported for manual handling; there is no automatic
downmix. FLAC with an embedded cuesheet or application/unknown metadata blocks
is refused during conversion because resampling can invalidate that data.
Unsupported or damaged headers are reported in the scan. These files stay
unchanged. The tool does not remove DRM or perform MQA unfolding.

## FFmpeg

Only FLAC conversion requires FFmpeg. Scanning and artwork resizing work with
the application's existing Python dependencies.

Install an FFmpeg executable using the links on the
[official download page](https://ffmpeg.org/download.html). On Windows, extract
the downloaded build and choose `bin/ffmpeg.exe` using **Settings → FFmpeg
executable → Browse…**. On macOS/Linux, choose the installed `ffmpeg` executable
or leave the field empty if it is on PATH. A full path is useful when launching
by double-click because desktop apps may have a different PATH than terminals.
FFmpeg is not bundled in this project.

The converter uses FFmpeg's
[resampler and dithering options](https://ffmpeg.org/ffmpeg-resampler.html).

## Originals and recovery

**Back up originals before converting or resizing** is enabled by default in
Settings. The default destination is `media-backups` in this computer's
application-data directory. You can choose another folder on the computer.
Backups inside the music folder or a detected Rockbox player are refused.
Make sure the destination has enough space for the selected originals.

Each run creates a dated folder containing `manifest.json` and
`originals/<relative music path>`. Backups are copied and their SHA-256 checksums
verified before originals are replaced. **Open backups** in Settings opens the
chosen folder. To recover, close the app and copy the desired file from
`originals` back to the matching relative path in your music folder. The
manifest records the original library location, paths and checksums. Backups
are retained until you remove them yourself.

If you already maintain your own backups, you can disable this option. The
dialog and apply confirmation clearly show **Local backups OFF**. There is no
automatic undo in that mode. This choice only affects the new media preparation
actions; it does not change the separate Rockbox database backup requirement
or the existing artwork editor's save behavior.

Conversion uses a temporary file beside the original. It requires additional
free space on the player even with local backups disabled. The original is
replaced only after verification and a check that it has not changed since
the scan. Individual failures are listed, and other selected files can finish.
**Cancel operation** stops the current conversion and leaves its original in
place; files already completed stay converted. Wait for the operation to stop
before disconnecting the player. Conversion and verification read full tracks
and take substantially longer than scanning.

After finishing, safely eject and restart the player to reload artwork. For
converted audio, use Rockbox's **Database → Update Now** to refresh file
properties. This beta still needs validation on a physical iPod.
