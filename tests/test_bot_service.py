from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from roco_mine_mini_service.game_sessions import SessionSnapshot, SessionState
from roco_mine_mini_service.qq_login import (
    LoginCredentials,
    parse_ptui_callback,
    parse_ptui_raw,
)
from roco_mine_mini_service.server import create_app
from roco_mine_mini_service.service import (
    AlreadyConnected,
    AutomationOperationError,
    QrNotFound,
    SingleUserGameService,
    pick_seed_id,
)


class SeedPickerTests(unittest.TestCase):
    def test_prefers_guai_guai_mushroom(self):
        available = {100728955: 47, 100728957: 640, 100728958: 37}

        self.assertEqual(pick_seed_id(available), 100728957)

    def test_falls_back_to_niu_zhai_tang(self):
        available = {100728955: 47, 100728958: 37}

        self.assertEqual(pick_seed_id(available), 100728955)

    def test_falls_back_to_most_abundant(self):
        available = {100728958: 37, 100728959: 11, 100728875: 1}

        self.assertEqual(pick_seed_id(available), 100728958)

    def test_explicit_preferred_overrides_default(self):
        available = {100728957: 640, 100728840: 50}

        self.assertEqual(
            pick_seed_id(available, preferred=100728840),
            100728840,
        )

    def test_empty_inventory_returns_none(self):
        self.assertIsNone(pick_seed_id({}))


class PtuiParserTests(unittest.TestCase):
    def test_parse_ptui_check_vc_raw_arguments(self):
        values = parse_ptui_raw("ptui_checkVC('0','!NQW','session','0','0')")

        self.assertEqual(values, ("0", "!NQW", "session", "0", "0"))

    def test_parse_ptui_check_vc_without_captcha(self):
        values = parse_ptui_raw("ptui_checkVC('0','','','0','0')")

        self.assertEqual(values[0], "0")
        self.assertEqual(values[1], "")

    def test_parse_ptui_cb_login_success(self):
        values = parse_ptui_raw(
            "ptuiCB('0','0','https://graph.qq.com/oauth2.0/login_jump?x=1',"
            "'0','登录成功！','昵称')"
        )

        self.assertEqual(values[0], "0")
        self.assertEqual(values[2], "https://graph.qq.com/oauth2.0/login_jump?x=1")
        self.assertEqual(values[4], "登录成功！")

    def test_parse_ptui_callback_summary_keeps_qr_flow_semantics(self):
        self.assertEqual(
            parse_ptui_callback("ptuiCB('0','0','https://x','0','ok','n')"),
            ("0", "https://x", "ok"),
        )
        self.assertEqual(
            parse_ptui_callback("ptuiCB('4','1','','0','密码错误','')")[0],
            "4",
        )


SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_service(**kwargs):
    # Tests must not depend on the real 23:00 service window.
    kwargs.setdefault("window_open", lambda: True)
    return SingleUserGameService(**kwargs)


def snapshot(
    *,
    state: SessionState,
    connected: bool,
    exp: int = 0,
    money: int = 0,
    online_time: int = 0,
    minigame_active: bool = False,
):
    now = datetime.now(SHANGHAI).isoformat()
    return SessionSnapshot(
        session_id="a" * 43,
        uin="12345",
        state=state,
        connected=connected,
        room_id=1,
        scene_id=1,
        online_time_seconds=online_time,
        rounds=0,
        total_money=money,
        total_exp=exp,
        access_expires_at=now,
        valid_until=now,
        login_at=now,
        started_at=now,
        time_stop_active=False,
        last_error_code=None,
        last_error=None,
        minigame_active=minigame_active,
    )


class FakeRegistry:
    def __init__(self, current: SessionSnapshot):
        self.current = current
        self.disconnected = False
        self.started = []

    async def snapshot(self, _: str) -> SessionSnapshot:
        return self.current

    async def disconnect(self, _: str) -> SessionSnapshot:
        self.disconnected = True
        return self.current

    async def refresh_online_time(self, _: str) -> SessionSnapshot:
        return self.current

    async def stop_minigame(self, _: str) -> SessionSnapshot:
        return self.current

    async def start_minigame(self, _: str) -> SessionSnapshot:
        return self.current

    async def start(self, credentials, grant):
        session = SimpleNamespace(session_id="a" * 43, credentials=credentials)
        self.started.append((credentials, grant))
        return SimpleNamespace(session=session, resumed=False)

    async def close_all(self) -> None:
        return None


class FakePasswordFlow:
    def __init__(self, credentials: LoginCredentials):
        self.credentials = credentials
        self.closed = False

    async def login(self) -> LoginCredentials:
        return self.credentials

    async def close(self) -> None:
        self.closed = True


class WaitingLoginFlow:
    def __init__(self):
        self.waiting = asyncio.Event()
        self.closed = False

    async def create_qr_code(self) -> bytes:
        return b"png-data"

    async def wait_for_login(self, _):
        await self.waiting.wait()

    async def close(self):
        self.closed = True


class BotServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_api_returns_public_temporary_qr_url(self):
        flow = WaitingLoginFlow()
        service = make_service(login_flow_factory=lambda: flow)
        app = create_app(service)
        old_public_url = os.environ.get("ROCO_PUBLIC_BASE_URL")
        os.environ["ROCO_PUBLIC_BASE_URL"] = "https://roco.example.com"
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post("/api/v1/scan")
                payload = response.json()
                token = payload["qr_url"].rsplit("/", 1)[-1]
                image = await client.get(f"/api/v1/qr/{token}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["expires_in"], 120)
            self.assertEqual(
                payload["qr_url"],
                f"https://roco.example.com/api/v1/qr/{token}",
            )
            self.assertEqual(image.headers["content-type"], "image/png")
            self.assertEqual(image.content, b"png-data")
        finally:
            await service.close()
            if old_public_url is None:
                os.environ.pop("ROCO_PUBLIC_BASE_URL", None)
            else:
                os.environ["ROCO_PUBLIC_BASE_URL"] = old_public_url

    async def test_scan_returns_png_while_login_continues_in_background(self):
        flow = WaitingLoginFlow()
        service = make_service(login_flow_factory=lambda: flow)

        ticket = await service.create_scan()

        self.assertEqual(service.qr_image(ticket.token), b"png-data")
        self.assertEqual(ticket.expires_in, 120)
        self.assertIsNotNone(service._scan_task)
        self.assertFalse(service._scan_task.done())
        await service.disconnect_for_bot()
        self.assertTrue(flow.closed)

    async def test_scan_expires_and_releases_flow(self):
        flow = WaitingLoginFlow()
        service = make_service(
            login_flow_factory=lambda: flow,
            scan_timeout_seconds=0.001,
        )

        ticket = await service.create_scan()
        await asyncio.wait_for(service._scan_task, timeout=1)

        self.assertTrue(flow.closed)
        self.assertIsNone(service._scan_flow)
        with self.assertRaises(QrNotFound):
            service.qr_image(ticket.token)

    async def test_unknown_qr_token_is_rejected(self):
        flow = WaitingLoginFlow()
        service = make_service(login_flow_factory=lambda: flow)
        await service.create_scan()

        with self.assertRaises(QrNotFound):
            service.qr_image("unknown")

        await service.close()

    async def test_status_returns_totals_for_connected_session(self):
        service = make_service()
        service._session_id = "a" * 43
        service._registry = FakeRegistry(
            snapshot(state=SessionState.RUNNING, connected=True, exp=12, money=34)
        )

        result = await service.status_for_bot()

        self.assertEqual(result.status, "online")
        self.assertEqual(result.credits, 12)
        self.assertEqual(result.rock_coins, 34)
        self.assertEqual(result.message, "学分：12，洛克贝：34")

    async def test_online_time_is_refreshed_and_formatted(self):
        service = make_service()
        service._session_id = "a" * 43
        service._registry = FakeRegistry(
            snapshot(
                state=SessionState.READY,
                connected=True,
                online_time=7384,
            )
        )

        result = await service.online_time_for_bot()

        self.assertEqual(result.status, "online")
        self.assertEqual(result.online_time_seconds, 7384)
        self.assertEqual(result.message, "当前在线时间：2h3min")

    async def test_online_time_returns_not_logged_in_without_session(self):
        service = make_service()

        result = await service.online_time_for_bot()

        self.assertEqual(result.status, "not_logged_in")
        self.assertEqual(result.message, "暂未登录")

    async def test_help_api_lists_all_bot_commands(self):
        service = make_service()
        app = create_app(service)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/help")

        payload = response.json()
        commands = {item["command"] for item in payload["commands"]}
        self.assertEqual(
            commands,
            {
                "说明",
                "扫码",
                "密码登录",
                "挂机",
                "停止挂机",
                "查询",
                "在线时间",
                "农场",
                "收菜",
                "播种",
                "乐园",
                "探险",
                "领奖",
                "全自动",
                "断开",
            },
        )
        self.assertIn("在线时间：查询当前账号今日在线时长", payload["message"])

    async def test_dropped_socket_is_reported_as_not_logged_in(self):
        service = make_service()
        service._session_id = "a" * 43
        service._registry = FakeRegistry(
            snapshot(state=SessionState.READY, connected=False)
        )

        result = await service.status_for_bot()

        self.assertEqual(result.status, "not_logged_in")
        self.assertEqual(result.message, "暂未登录")
        self.assertIsNone(service._session_id)

    async def test_disconnect_is_idempotent_when_not_logged_in(self):
        service = make_service()

        result = await service.disconnect_for_bot()

        self.assertEqual(result.status, "not_logged_in")
        self.assertEqual(result.message, "暂未登录")

    async def test_password_login_starts_session_and_reports_status(self):
        credentials = LoginCredentials(
            uin="12345",
            angel_key="key",
            pskey="pskey",
            skey="skey",
        )
        flow = FakePasswordFlow(credentials)
        service = make_service(password_flow_factory=lambda a, p: flow)
        service._registry = FakeRegistry(
            snapshot(state=SessionState.READY, connected=True, exp=5, money=7)
        )

        result = await service.login_with_password("12345", "secret")

        self.assertEqual(result.status, "online")
        self.assertEqual(result.credits, 5)
        self.assertEqual(result.rock_coins, 7)
        self.assertTrue(flow.closed)
        self.assertEqual(service._registry.started[0][0].uin, "12345")
        await service.close()

    async def test_password_login_rejects_already_connected(self):
        flow = FakePasswordFlow(
            LoginCredentials(uin="12345", angel_key="k", pskey="p", skey="s")
        )
        service = make_service(password_flow_factory=lambda a, p: flow)
        service._session_id = "a" * 43
        service._registry = FakeRegistry(
            snapshot(state=SessionState.READY, connected=True)
        )

        with self.assertRaises(AlreadyConnected):
            await service.login_with_password("12345", "secret")

    async def test_farm_and_paradise_apis_return_not_logged_in_without_session(self):
        service = make_service()
        app = create_app(service)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            farm = await client.get("/api/v1/farm")
            paradise = await client.get("/api/v1/paradise")
            plant = await client.post("/api/v1/farm/plant", json={})
            automation = await client.get("/api/v1/automation/status")

        self.assertEqual(farm.json()["status"], "not_logged_in")
        self.assertEqual(paradise.json()["status"], "not_logged_in")
        self.assertEqual(plant.json()["status"], "not_logged_in")
        self.assertFalse(automation.json()["active"])
        await service.close()

    async def test_login_api_requires_credentials(self):
        service = make_service()
        app = create_app(service)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            empty = await client.post("/api/v1/login", json={})

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["code"], "CREDENTIALS_REQUIRED")
        await service.close()

    async def test_automation_start_requires_login(self):
        service = make_service()

        with self.assertRaises(AutomationOperationError):
            await service.start_automation()

        self.assertFalse(service.automation_status().active)

    async def test_automation_hang_tick_starts_minigame_from_idle(self):
        service = make_service()
        service._session_id = "a" * 43
        registry = FakeRegistry(
            snapshot(state=SessionState.READY, connected=True)
        )
        started = []
        original = registry.start_minigame

        async def tracked_start(_):
            started.append(1)
            return await original(_)

        registry.start_minigame = tracked_start
        service._registry = registry
        service._automation_cfg = {
            "hang": True,
            "hang_minutes": 30,
            "hang_cooldown_minutes": 5,
        }

        await service._automation_hang_tick()

        self.assertEqual(service._auto_hang_state, "hanging")
        self.assertEqual(len(started), 1)

    async def test_automation_hang_tick_pauses_for_farm_and_restarts(self):
        service = make_service()
        service._session_id = "a" * 43
        registry = FakeRegistry(
            snapshot(
                state=SessionState.RUNNING,
                connected=True,
                minigame_active=True,
            )
        )
        # A RUNNING snapshot reports minigame_active for the pause branch.
        paused = []
        original_stop = registry.stop_minigame
        original_start = registry.start_minigame

        async def tracked_stop(_):
            paused.append(1)
            return await original_stop(_)

        registry.stop_minigame = tracked_stop
        service._registry = registry
        service._automation_cfg = {
            "hang": True,
            "hang_minutes": 30,
            "hang_cooldown_minutes": 5,
        }
        service._auto_hang_state = "hanging"
        service._auto_hang_until = 0.0  # expire immediately

        await service._automation_hang_tick()

        self.assertEqual(service._auto_hang_state, "cooldown")
        self.assertEqual(len(paused), 1)

    async def test_stop_minigame_reports_stopped_with_totals(self):
        service = make_service()
        service._session_id = "a" * 43
        service._registry = FakeRegistry(
            snapshot(state=SessionState.RUNNING, connected=True, exp=12, money=34)
        )

        result = await service.stop_minigame_for_bot()

        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.message, "挂机已停止")
        self.assertEqual(result.credits, 12)
        self.assertEqual(result.rock_coins, 34)


if __name__ == "__main__":
    unittest.main()
