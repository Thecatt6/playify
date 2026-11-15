"""Playify — a self-hosted Discord music bot.

The :mod:`src.playify.app` module is imported lazily so that lightweight
sub-packages (for instance :mod:`src.playify.webui`, which is also used by the
tests and by the standalone UI demo) can be imported without pulling in
discord.py, yt-dlp and the rest of the bot runtime.
"""

from __future__ import annotations

import importlib

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        module = importlib.import_module(".app", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
