# SPDX-License-Identifier: GPL-2.0-only
"""Consolidating duplicate artist folders on disk, with conflict checks."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from . import artwork_engine as engine
from .library import LibraryItem


# ----------------------------------------------------------------------
# Safe artist-folder consolidation
# ----------------------------------------------------------------------
def _safe_fragment(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', "_", value or "")
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value or "alias"


def _files_identical(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with a.open("rb") as fa, b.open("rb") as fb:
            while True:
                ca = fa.read(1024 * 1024)
                cb = fb.read(1024 * 1024)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _unique_sibling(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 10000):
        candidate = path.with_name(f"{stem}.{n}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not make a unique backup name near {path}")


def consolidate_artist_folders(
    root: Path,
    physical_items: list[LibraryItem] | list[Any],
    canonical: str,
    *,
    output_name: str = "folder.jpg",
    retag_albumartist: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Safely merge physical artist folders into the selected canonical folder.

    Nothing is overwritten. A complete preflight is done before any move. Album
    directory collisions and conflicting root-level files abort the operation
    without changing the library. Different artist artwork is preserved beside
    the canonical ``folder.jpg`` under a unique alias-specific filename.
    """
    progress = progress or (lambda _text: None)
    root = Path(root)
    rows = [i for i in physical_items if getattr(i, "relative_path", "")]
    if len(rows) < 2:
        return {"ok": False, "error": "Fewer than two physical artist folders were found."}

    target_item = next((i for i in rows if str(getattr(i, "artist", "")) == canonical), None)
    if target_item is None:
        target_item = next((i for i in rows if str(getattr(i, "artist", "")).casefold() == canonical.casefold()), None)
    if target_item is None:
        return {"ok": False, "error": "The selected display name is not one of the physical artist folders."}

    target = root / Path(str(target_item.relative_path))
    if not target.is_dir():
        return {"ok": False, "error": f"Target artist folder does not exist: {target}"}

    sources: list[tuple[Any, Path]] = []
    for item in rows:
        if str(item.relative_path) == str(target_item.relative_path):
            continue
        path = root / Path(str(item.relative_path))
        if path.is_dir():
            sources.append((item, path))
    if not sources:
        return {"ok": False, "error": "No source artist folders remain to merge."}

    # Preflight first: never start a partial merge when album/file collisions exist.
    conflicts: list[str] = []
    ignorable = {".ds_store", "thumbs.db", "desktop.ini"}
    configured_art_name = output_name or "folder.jpg"
    for item, source in sources:
        source_art_name = (str(getattr(item, "artwork_name", "")) if getattr(item, "artwork_exists", False) else configured_art_name) or configured_art_name
        source_art_cf = source_art_name.casefold()
        for child in source.iterdir():
            destination = target / child.name
            if child.name.casefold() == source_art_cf and child.is_file():
                continue
            if child.name.casefold() in ignorable and child.is_file():
                continue
            if destination.exists():
                if child.is_file() and destination.is_file() and _files_identical(child, destination):
                    continue
                conflicts.append(f"{source.name} / {child.name}")
    if conflicts:
        return {
            "ok": False,
            "conflicts": conflicts,
            "error": "Nothing was changed because destination names already exist.",
        }

    moved_entries = 0
    preserved_artwork: list[str] = []
    removed_sources: list[str] = []

    for item, source in sources:
        progress(f"Merging {source.name} into {target.name}")
        source_art_name = (str(getattr(item, "artwork_name", "")) if getattr(item, "artwork_exists", False) else configured_art_name) or configured_art_name
        source_art_cf = source_art_name.casefold()
        for child in list(source.iterdir()):
            destination = target / child.name
            low = child.name.casefold()

            if child.is_file() and low == source_art_cf:
                target_art_name = (str(getattr(target_item, "artwork_name", "")) if getattr(target_item, "artwork_exists", False) else configured_art_name) or configured_art_name
                target_art = target / target_art_name
                if not target_art.exists():
                    shutil.move(str(child), str(target_art))
                    moved_entries += 1
                elif _files_identical(child, target_art):
                    child.unlink()
                else:
                    suffix = child.suffix or target_art.suffix or ".jpg"
                    backup = target / f"folder.merged-{_safe_fragment(str(getattr(item, 'artist', source.name)))}{suffix}"
                    backup = _unique_sibling(backup)
                    shutil.move(str(child), str(backup))
                    preserved_artwork.append(backup.name)
                    moved_entries += 1
                continue

            if child.is_file() and low in ignorable:
                try:
                    child.unlink()
                except OSError:
                    pass
                continue

            if destination.exists() and child.is_file() and destination.is_file() and _files_identical(child, destination):
                child.unlink()
                continue

            shutil.move(str(child), str(destination))
            moved_entries += 1

        try:
            source.rmdir()
            removed_sources.append(source.name)
        except OSError:
            # Non-destructive fallback: leave anything unexpected behind and report it.
            pass

    tag_changed = 0
    tag_failed: list[str] = []
    if retag_albumartist:
        progress(f"Updating ALBUMARTIST to {canonical}")
        for audio_path in engine.iter_audio_files(target):
            try:
                audio = engine.MutagenFile(str(audio_path), easy=True)
                if audio is None:
                    raise RuntimeError("unsupported metadata")
                audio["albumartist"] = [canonical]
                audio.save()
                tag_changed += 1
            except Exception as exc:
                tag_failed.append(f"{audio_path.name}: {exc}")

    return {
        "ok": True,
        "target": str(target),
        "moved_entries": moved_entries,
        "folder_moves": [[str(source.resolve()), str(target.resolve())] for _item, source in sources],
        "removed_sources": removed_sources,
        "preserved_artwork": preserved_artwork,
        "tag_changed": tag_changed,
        "tag_failed": tag_failed,
    }
