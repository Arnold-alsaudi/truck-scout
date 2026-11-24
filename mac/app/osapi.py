"""Picks the right OS layer: winos on Windows, macos on Mac. Both expose the same functions."""
import sys

if sys.platform == "win32":
    from winos import *  # noqa: F401,F403
    PLATFORM = "win"
else:
    from macos import *  # noqa: F401,F403
    PLATFORM = "mac"
