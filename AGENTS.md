# Repository instructions

## Project and change scope

Rockbox Library Manager is a Python desktop companion for PodBox/Rockbox.
It manages artist/album artwork, library health, album tags, media preparation,
and updates to an existing Rockbox database.

- Read the relevant implementation, callers, tests, and documentation before
  changing behavior.
- Do not remove existing functionality or substantially change its behavior
  or architecture without explicit user permission.
- Preserve existing English identifiers and naming conventions. Do not
  translate comments or documentation solely to change their language.
- Distinguish reproduced bugs from theoretical risks and intentional
  limitations. Check existing safeguards before proposing replacements.
- Preserve GPL-2.0-only licensing and upstream attribution in LICENSE,
  CREDITS.md, and adapted source files.

## Architecture

- `rockbox_library_manager.py`, `rockbox_manager/__main__.py`, and
  `launcher.py`: application entry points and GUI/CLI dispatch. Preserve the
  documented legacy artwork command syntax.
- `gui.py`: Tk application, gallery, settings, activity log, worker coordination,
  and thumbnail cache.
- `library.py`: SQLite physical-folder index, virtual artist identities, and
  fast directory scanner.
- `health.py`: library diagnostics; `tags.py`: metadata reading, edit planning,
  and writing; `tag_editor.py`: review and save UI.
- `artwork_engine.py`: folder/tag matching, artwork retrieval, candidate
  validation/scoring, output, and fetch caches.
- `sources.py`: online providers and shared HTTP caching/rate limiting;
  `picker.py`: interactive artwork selection.
- `folders.py`: explicit consolidation of duplicate physical artist folders.
- `media_tools.py` / `media_dialog.py`: media inspection, verified conversion
  and artwork replacement, backups, and UI.
- `rockbox_database.py` / `database_dialog.py`: binary database parsing,
  preview, backup, update, recovery, and UI.
- `common.py`, `ui_theme.py`, and `widgets.py`: shared utilities and UI components.
- `tests/`: unit and real Tk dialog tests, with a FLAC fixture.
- `README.md`, `docs/media-preparation.md`, and `docs/rockbox-database.md`:
  documented workflows, limitations, and recovery behavior.

Keep Tk updates on the UI thread. Follow the existing worker/queue callback
pattern and operation-lock coordination when modifying background operations.

## Runtime and coding conventions

- Support Python 3.10 and later on Windows, macOS, and Linux.
- Required packages: `mutagen>=1.48`, `Pillow>=12.0`, `requests>=2.33`.
  Tkinter must be available; `tkinterdnd2` is optional.
- FFmpeg is an external dependency for FLAC conversion. Artwork processing
  and scanning must remain usable without FFmpeg.
- Packaging uses Hatchling. The version comes from
  `rockbox_manager/__init__.py`.
- Standalone downloads use `rockbox-library-manager.spec`, `scripts/build_app.py`
  and `requirements-build.txt`. Keep GUI and CLI dispatch compatible with source
  runs. Native builds bundle tkinterdnd2; source installations keep it optional.
- Follow existing module boundaries and local code style. Ruff targets Python
  3.10, configures 120-column lines, and checks `E9` and `F`.
  Avoid unrelated reformatting of the adapted artwork engine.

## Existing design and compatibility

- SQLite retains physical artist/album rows. Virtual artist merging changes
  presentation; it does not itself move folders or edit music tags.
  Preserve explicit aliases and "Keep separate" decisions.
- The normal index scan avoids tag parsing. Health checks sample first,
  middle, and last tracks by default; thorough mode checks every track.
- Tag edits require a before/after review and modify selected fields only.
  Existing unchanged values should not trigger unnecessary file rewrites.
- Artwork output is square baseline JPEG, using `folder.jpg` or `cover.jpg`.
  Preserve existing artwork filenames, matching thresholds, candidate scoring,
  provider pacing, and the distinction between automatic and manual selection.
- Configuration retains the existing `RockboxArtistArt` /
  `rockbox-artist-art` locations. Fetch caches live in the music root; the
  SQLite index and thumbnails are local. Preserve existing persisted formats
  or provide a compatible migration.
- Credentials use private atomic writes, with owner-only permissions on POSIX.
- Media preparation backs up originals by default, verifies output before
  replacement, and checks for changed source files. It preserves supported
  tags and embedded pictures. Small artwork is not automatically upscaled.
  Preserve refusal of unsupported multichannel or opaque FLAC metadata.
- Direct database updates support TCH v16 in either byte order and existing
  indexed tracks. Preserve supported `/Music/...` and `/<HDD0>/Music/...` paths,
  row identities, runtime data, and unrelated fields.
- Database updates/restores require verified local backups. Preserve staging,
  stale-preview checks, dirty-master/journal recovery, and path validation.
  Do not broaden format support or guess missing track mappings implicitly.
- Database restore does not undo music tag edits or folder moves. Backups are
  retained without automatic deletion.

## Verification

Install dependencies from `requirements.txt`; development linting uses
`ruff>=0.11,<1`.

Run from the repository root:

    python -m ruff check rockbox_manager tests
    python -m unittest discover -s tests -v

- Use temporary libraries, settings, databases, and copied fixtures for tests.
  Do not test destructive operations against the user's actual player.
- Tk dialog tests require a desktop display. Linux CI uses:

      xvfb-run -a -s "-screen 0 1920x1200x24" \
        python -m unittest discover -s tests -v

- FFmpeg-dependent tests need FFmpeg. Native database interoperability tests
  additionally require `ROCKBOX_DATABASE_TOOL`.
- CI covers Windows, macOS, and Linux on Python 3.10 and 3.13.
- Native builds additionally check Windows x64, Linux x64 and both Mac
  architectures on Python 3.13. Run `python scripts/build_app.py` with the build
  dependencies installed to build and verify an extracted package. Preserve
  archive symlinks, signing integrity, license notices and version/tag checks;
  see `docs/native-builds.md` and `.github/workflows/builds.yml`.
- Report tests actually executed, skips, and missing prerequisites. Do not
  equate lint success or mocked checks with full player compatibility.
