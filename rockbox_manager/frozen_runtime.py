# SPDX-License-Identifier: GPL-2.0-only
"""Keep system programs independent of a frozen Linux application's libraries."""
from __future__ import annotations

import os
import sys


def external_environment() -> dict[str, str] | None:
    if not getattr(sys, "frozen", False) or sys.platform != "linux":
        return None
    env = os.environ.copy()
    original = env.get("LD_LIBRARY_PATH_ORIG")
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env
