"""Runtime context shared by every Web UI request."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import WebUIConfig
from .oauth import DiscordOAuth
from .security import TokenSigner


@dataclass
class WebUIContext:
    """Everything the aiohttp application needs to talk to the bot."""

    bot: Any
    config: WebUIConfig
    signer: TokenSigner
    oauth: DiscordOAuth
    started_at: float = field(default_factory=time.time)
    runner: Any = field(default=None, repr=False)
