"""A tiny Discord OAuth2 client used to identify Web UI visitors.

Only the ``identify`` scope is requested: the avatar and username are used for
display, and the Discord user id is what the bot uses to check that the visitor
is really sitting in the same voice channel as Playify.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger("playify.webui")

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
USER_URL = "https://discord.com/api/users/@me"
SCOPES = "identify"


class OAuthError(Exception):
    """Raised when the Discord OAuth2 dance fails."""


class DiscordOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session

    # ---------------------------------------------------------------- internals
    def _client_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # -------------------------------------------------------------------- flow
    def authorize_url(self, redirect_uri: str, state: str) -> str:
        query = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "prompt": "none",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        try:
            async with self._client_session().post(
                TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
            ) as response:
                payload = await _read_json(response)
                if response.status != 200:
                    raise OAuthError(
                        f"Discord refused the token exchange ({response.status}): "
                        f"{payload.get('error_description') or payload.get('error') or 'unknown error'}"
                    )
        except aiohttp.ClientError as exc:
            raise OAuthError(f"Could not reach Discord: {exc}") from exc

        token = payload.get("access_token")
        if not token:
            raise OAuthError("Discord did not return an access token")
        return str(token)

    async def fetch_user(self, access_token: str) -> dict:
        try:
            async with self._client_session().get(
                USER_URL, headers={"Authorization": f"Bearer {access_token}"}
            ) as response:
                payload = await _read_json(response)
                if response.status != 200:
                    raise OAuthError(
                        f"Discord refused the profile request ({response.status})"
                    )
        except aiohttp.ClientError as exc:
            raise OAuthError(f"Could not reach Discord: {exc}") from exc

        if not payload.get("id"):
            raise OAuthError("Discord returned an unexpected profile payload")
        return payload


async def _read_json(response: aiohttp.ClientResponse) -> dict:
    text = await response.text()
    try:
        payload = json.loads(text)
    except ValueError:
        return {"raw": text[:200]}
    return payload if isinstance(payload, dict) else {"raw": text[:200]}
