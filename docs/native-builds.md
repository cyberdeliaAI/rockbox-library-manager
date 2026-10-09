# Standalone applications and native builds

Release downloads bundle Python, Tcl/Tk, Pillow, requests, mutagen and
tkinterdnd2 with PyInstaller. No Python installation or pip commands are needed
on the user's computer. The source installation remains available.

## Downloads and platform coverage

- `rockbox-library-manager-vVERSION-windows-x64.zip`: extract the whole folder
  and double-click `Rockbox Library Manager.exe`.
- `rockbox-library-manager-vVERSION-macos-arm64.zip`: for Apple Silicon Macs.
- `rockbox-library-manager-vVERSION-macos-x64.zip`: for Intel Macs.
  Extract the matching `.app` and move it to Applications.
- `rockbox-library-manager-vVERSION-linux-x64.tar.gz`: extract with a tool
  that preserves permissions and symbolic links. In the extracted directory,
  run `./"Rockbox Library Manager"` to open the GUI.

Windows and Linux executables need their accompanying `_internal` directory;
do not move only the executable. macOS bundles likewise need their complete
Contents directory. macOS ZIPs and Linux tarballs preserve PyInstaller's
symbolic links.

Builds are checked on Windows Server 2022, macOS 14 (Apple Silicon), macOS 15
(Intel) and Ubuntu 22.04 (x86-64). Linux needs a graphical desktop and compatible
system libraries, including glibc 2.35 or later; it is not a universal Linux
binary. Other OS versions are not part of this native build matrix.

macOS bundles have an ad-hoc integrity signature, but are not Developer ID
signed or notarized. Windows packages are not Authenticode signed. The OS may
require explicit approval before opening a downloaded application. Certificate
signing is not configured in this repository.

Every native archive has a `.sha256` file. Compare it with `Get-FileHash` on
Windows, `shasum -a 256` on macOS, or `sha256sum` on Linux.

FFmpeg is **not bundled**. Artwork and scanning work without it; audio conversion
requires a separate FFmpeg installation. Use Settings to select its full path,
especially on macOS where Finder's PATH differs from a terminal's PATH.
On frozen Linux builds, external FFmpeg and the file manager inherit the
original system library search path rather than PyInstaller's bundled paths.

Settings, credentials, SQLite data, thumbnails and backups retain their existing
locations and formats. Installing a new application package does not reset them.
Backups remain on the user's configured local storage.

## Command line

The command-line executable shares the same application and retains existing
`artwork`, `artist`, `album`, `both` and credential-management commands.

Windows, from the extracted directory:

```powershell
.\rockbox-library-manager.exe --version
.\rockbox-library-manager.exe artwork --help
```

macOS:

```sh
"/Applications/Rockbox Library Manager.app/Contents/MacOS/rockbox-library-manager" --version
"/Applications/Rockbox Library Manager.app/Contents/MacOS/rockbox-library-manager" artwork --help
```

Linux, from the extracted directory:

```sh
./rockbox-library-manager --version
./rockbox-library-manager artwork --help
```

## Build locally

Use Python 3.13 with Tcl/Tk on the target OS and architecture. PyInstaller is
not a cross-compiler. Build dependencies are separate from runtime requirements:

```sh
python -m pip install -r requirements-build.txt
python scripts/build_app.py
```

Generated files are ignored by Git: `build/` contains work files and check
reports, while `dist/packages/` contains verified archives and SHA-256 files.
The script derives the version from `rockbox_manager/__init__.py`, renders the
existing application icon, includes project/dependency license notices, and
builds GUI and CLI executables sharing one Python runtime. It tests the extracted
archive from a directory with spaces outside the checkout. macOS signatures are
checked after extraction. Build checks use only temporary settings and libraries.

The frozen checks instantiate the actual desktop and media dialog, require
native drag and drop, exercise SQLite scanning, FLAC tags, JPEG resizing and
verified backups, and check CLI help/version dispatch and HTTPS certificate data.
If FFmpeg is installed, they also perform and verify a real audio conversion.
CI requires that FFmpeg check to run; FFmpeg is still not included in downloads.
No player or live artwork provider is used by these checks.

## GitHub Actions and publishing

`Native downloads` builds all four targets on pull requests and main pushes.
Validated archives and smoke-test reports appear as workflow artifacts. This
complements the existing six-job Python/platform regression matrix.

Publishing a GitHub release triggers native builds from that release's tag.
The tag must match `v` plus the application version and contain this build recipe.
After all four builds pass, the publication job attaches the archives, SHA-256
files, and the existing source ZIP format to that release. Only the publication
job has `contents: write`; pull-request build jobs cannot publish.

To retry an existing release, run `Native downloads` manually on main and enter
its tag. This replaces generated assets with the same filenames after successful
validation. Leave the tag blank to build workflow artifacts without publishing.
