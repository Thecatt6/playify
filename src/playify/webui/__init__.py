"""Optional browser control surface for Playify.

The package is self-contained: the HTTP layer only talks to the bot through the
objects it is given (``bot.voice_clients``, the guild's ``MusicPlayer`` …), and
the Playify modules are imported lazily so the web layer can be imported and
tested without discord.py, yt-dlp or a running bot.
"""

from .config import WebUIConfig, load_config
from .runtime import current_url, is_running, start_webui, stop_webui

__all__ = [
    "WebUIConfig",
    "load_config",
    "start_webui",
    "stop_webui",
    "is_running",
    "current_url",
]
