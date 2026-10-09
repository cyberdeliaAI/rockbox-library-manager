# SPDX-License-Identifier: GPL-2.0-only
"""Build-only entry point; normal application dispatch remains in launcher."""
import sys

from rockbox_manager.launcher import main

if __name__ == "__main__":
    if sys.argv[1:2] == ["--bundle-smoke-test"]:
        from bundle_check import run
        raise SystemExit(run(sys.argv[2:]))
    raise SystemExit(main())
