from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx

from roco_mine_mini_service.access import AccessGrant, AccessProvider
from roco_mine_mini_service.game_sessions import GameAutomationError, GameSession, SHANGHAI
from roco_mine_mini_service.qq_login import LoginCredentials


class SequenceHttpClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def get(self, _):
        self.calls += 1
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


def make_session() -> GameSession:
    now = datetime.now(SHANGHAI)
    credentials = LoginCredentials(
        uin="12345",
        angel_key="old-key",
        pskey="test-pskey",
        skey="test-skey",
        login_at=now,
    )
    return GameSession(
        AccessProvider(),
        credentials,
        AccessGrant(credentials.uin, now + timedelta(hours=1)),
    )


def response(status: int, body: bytes) -> httpx.Response:
    return httpx.Response(status, content=body)


class AngelKeyRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_retries_then_updates_key(self):
        session = make_session()
        session._http = SequenceHttpClient(
            [
                httpx.ConnectError("temporary failure"),
                response(503, b"unavailable"),
                response(200, b"<root><result>0</result><angel_key>new-key</angel_key></root>"),
            ]
        )
        session._interruptible_sleep = AsyncMock()

        await session._refresh_angel_key()

        self.assertEqual(session.credentials.angel_key, "new-key")
        self.assertEqual(session._http.calls, 3)
        self.assertEqual(session._interruptible_sleep.await_count, 2)

    async def test_refresh_disconnects_only_after_all_attempts_fail(self):
        session = make_session()
        session._http = SequenceHttpClient(
            [response(503, b"no") for _ in range(3)]
        )
        session._interruptible_sleep = AsyncMock()

        with self.assertRaisesRegex(GameAutomationError, "after retries"):
            await session._refresh_angel_key()

        self.assertEqual(session._http.calls, 3)
        self.assertEqual(session._interruptible_sleep.await_count, 2)
        self.assertEqual(session.credentials.angel_key, "old-key")


if __name__ == "__main__":
    unittest.main()
