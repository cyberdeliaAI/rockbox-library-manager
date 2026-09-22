from __future__ import annotations

import sys
from typing import Optional

from . import __version__


def _print_help() -> None:
    print(f"Rockbox Library Manager {__version__}")
    print()
    print("Usage:")
    print("  python rockbox_library_manager.py")
    print("      Open the desktop application.")
    print()
    print("  python rockbox_library_manager.py gui")
    print("      Open the desktop application explicitly.")
    print()
    print("  python rockbox_library_manager.py artwork [artist|album|both] MUSIC_ROOT [options]")
    print("      Run the integrated artwork engine from the command line.")
    print()
    print("Legacy artwork shortcuts are also accepted:")
    print("  python rockbox_library_manager.py both MUSIC_ROOT")
    print("  python rockbox_library_manager.py --show-credentials-status")
    print()
    print("For artwork-engine options:")
    print("  python rockbox_library_manager.py artwork --help")


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # No arguments = the single normal desktop application entry point.
    if not args or args[0].casefold() == "gui":
        if args and len(args) > 1:
            print("ERROR: the gui command does not take additional arguments.", file=sys.stderr)
            return 2
        from . import gui
        return gui.main()

    first = args[0].casefold()

    if first in {"-h", "--help", "help"}:
        _print_help()
        return 0

    if first in {"--version", "version"}:
        print(__version__)
        return 0

    from . import artwork_engine

    if first == "artwork":
        return artwork_engine.main(args[1:])

    # Backward-compatible path for the old art_fetch.py syntax. This makes the
    # migration to one application painless while still keeping only one public
    # entry point.
    if first in {"artist", "album", "both"} or first.startswith("-"):
        return artwork_engine.main(args)

    print(f"ERROR: unknown command: {args[0]}", file=sys.stderr)
    print("Run with --help for usage.", file=sys.stderr)
    return 2
