"""Single-user QR login and game-session orchestration."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .access import AccessProvider
from .game_sessions import (
    FarmOperationError,
    FarmSnapshot,
    GameSessionExpired,
    GameSessionNotFound,
    GameSessionRegistry,
    MinigameOperationError,
    ParadiseOperationError,
    ParadiseSnapshot,
    SessionSnapshot,
    SessionState,
    service_window_open,
    session_valid_until,
)
from .logging_setup import SHANGHAI as LOG_SHANGHAI
from .qq_login import QQLoginFlow, QQPasswordLoginFlow, QqLoginError

logger = logging.getLogger(__name__)
SCAN_TIMEOUT_SECONDS = 120.0

# Auto-sow seed priority: 乖乖蘑菇 → 小Q牛轧糖 → most abundant inventory seed.
DEFAULT_PREFERRED_SEED_ID: int | None = 100728957
DEFAULT_FALLBACK_SEED_IDS: tuple[int, ...] = (100728955,)


class ScanInProgress(Exception):
    pass


class AlreadyConnected(Exception):
    pass


class ServiceClosed(Exception):
    pass


class QrNotFound(Exception):
    pass


class AutomationOperationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ScanTicket:
    token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class BotStatus:
    status: str
    message: str
    credits: int = 0
    rock_coins: int = 0
    state: str | None = None


@dataclass(frozen=True, slots=True)
class OnlineTimeStatus:
    status: str
    message: str
    online_time_seconds: int = 0
    state: str | None = None


@dataclass(frozen=True, slots=True)
class FarmStatus:
    status: str
    message: str
    manor_level: int | None = None
    land_count: int = 0
    planted_count: int = 0
    harvestable_count: int = 0
    empty_count: int = 0
    seeds: tuple[dict[str, int], ...] = ()
    updated_at: str | None = None
    last_action: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ParadiseStatus:
    status: str
    message: str
    level: int | None = None
    experience: int | None = None
    spirit_count: int = 0
    countdown: int = -1
    participants: int = -1
    times: int = -1
    limit: int = -1
    remaining: int = -1
    updated_at: str | None = None
    last_action: str | None = None
    last_error: str | None = None
    last_reward: dict | None = None


@dataclass(frozen=True, slots=True)
class AutomationStatus:
    status: str
    message: str
    active: bool = False
    started_at: str | None = None
    last_log: str | None = None
    log_interval_seconds: int = 0
    farm_enabled: bool = False
    paradise_enabled: bool = False
    hang_enabled: bool = False
    hang_state: str = "idle"
    farm_interval_seconds: int = 0
    paradise_interval_seconds: int = 0
    hang_minutes: int = 0
    hang_cooldown_minutes: int = 0
    harvested: int = 0
    planted: int = 0
    adventures: int = 0
    claims: int = 0
    failures: int = 0


BOT_COMMANDS = (
    ("说明", "查看机器人支持的指令及效果"),
    ("扫码", "获取两分钟有效的 QQ 登录二维码"),
    ("密码登录", "使用 config.yaml 中的账号密码登录"),
    ("挂机", "进入房间后开始小游戏挂机"),
    ("停止挂机", "停止小游戏挂机，保持游戏连接"),
    ("查询", "查询本次挂机获得的学分和洛克贝"),
    ("在线时间", "查询当前账号今日在线时长"),
    ("农场", "查看庄园土地和种子背包状态"),
    ("收菜", "收获庄园中所有可收获的作物"),
    ("播种", "在空闲土地上自动播种（默认选背包最多的种子）"),
    ("乐园", "查看乐园精灵和探险状态"),
    ("探险", "让乐园精灵开始新一轮探险"),
    ("领奖", "领取已完成的乐园探险奖励"),
    ("全自动", "密码登录模式下自动挂机、收菜、播种、探险"),
    ("断开", "停止挂机并断开当前游戏连接"),
)


def help_message() -> str:
    lines = ["支持的指令："]
    lines.extend(f"{command}：{effect}" for command, effect in BOT_COMMANDS)
    return "\n".join(lines)


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分钟{seconds}秒"
    if minutes:
        return f"{minutes}分钟{seconds}秒"
    return f"{seconds}秒"


def format_duration_compact(seconds: int) -> str:
    """Compact duration: 4740 -> '1h19min', 3600 -> '1h', 120 -> '2min'."""

    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}min" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}min" if seconds == 0 else f"{minutes}min{seconds}sec"
    return f"{seconds}sec"


class SingleUserGameService:
    def __init__(
        self,
        *,
        login_flow_factory: Callable[[], QQLoginFlow] = QQLoginFlow,
        password_flow_factory: Callable[[str, str], QQPasswordLoginFlow] = (
            lambda account, password: QQPasswordLoginFlow(account, password)
        ),
        window_open: Callable[[], bool] = service_window_open,
        scan_timeout_seconds: float = SCAN_TIMEOUT_SECONDS,
        preferred_seed_id: int | None = DEFAULT_PREFERRED_SEED_ID,
        fallback_seed_ids: tuple[int, ...] = DEFAULT_FALLBACK_SEED_IDS,
    ) -> None:
        if scan_timeout_seconds <= 0:
            raise ValueError("scan_timeout_seconds must be positive")
        self._access = AccessProvider()
        self._registry = GameSessionRegistry(self._access, max_sessions=1)
        self._login_flow_factory = login_flow_factory
        self._password_flow_factory = password_flow_factory
        self._window_open = window_open
        self._scan_timeout_seconds = scan_timeout_seconds
        self._preferred_seed_id = preferred_seed_id
        self._fallback_seed_ids = fallback_seed_ids
        self._session_id: str | None = None
        self._scan_task: asyncio.Task[None] | None = None
        self._scan_flow: QQLoginFlow | None = None
        self._qr_token: str | None = None
        self._qr_image: bytes | None = None
        self._qr_expires_at = 0.0
        self._scan_lock = asyncio.Lock()
        self._automation_task: asyncio.Task[None] | None = None
        self._automation_stop = asyncio.Event()
        self._automation_lock = asyncio.Lock()
        self._automation_cfg: dict[str, object] = {}
        self._automation_stats: dict[str, int] = {
            "harvested": 0,
            "planted": 0,
            "adventures": 0,
            "claims": 0,
            "failures": 0,
        }
        self._automation_started_at: datetime | None = None
        self._automation_last_log: str | None = None
        self._auto_hang_state = "idle"
        self._auto_hang_until = 0.0
        self._auto_cooldown_until = 0.0

    async def create_scan(self) -> ScanTicket:
        """Create a temporary QR URL token and continue login in the background."""

        if not self._window_open():
            raise ServiceClosed()
        async with self._scan_lock:
            await self._discard_terminal_session()
            if await self._usable_snapshot() is not None:
                raise AlreadyConnected()
            if self._scan_task is not None and not self._scan_task.done():
                raise ScanInProgress()

            flow = self._login_flow_factory()
            try:
                image = await flow.create_qr_code()
            except BaseException:
                await flow.close()
                raise
            token = secrets.token_urlsafe(32)
            self._scan_flow = flow
            self._qr_token = token
            self._qr_image = image
            self._qr_expires_at = time.monotonic() + self._scan_timeout_seconds
            self._scan_task = asyncio.create_task(self._finish_scan(flow, token))
            return ScanTicket(token, int(self._scan_timeout_seconds))

    def qr_image(self, token: str) -> bytes:
        if (
            not token
            or token != self._qr_token
            or self._qr_image is None
            or time.monotonic() >= self._qr_expires_at
        ):
            if token == self._qr_token:
                self._clear_qr(token)
            raise QrNotFound()
        return self._qr_image

    async def _finish_scan(self, flow: QQLoginFlow, token: str) -> None:
        try:
            async def status_changed(_: str) -> None:
                return None

            async with asyncio.timeout(self._scan_timeout_seconds):
                credentials = await flow.wait_for_login(status_changed)
            if not self._window_open():
                return
            grant = self._access.issue(credentials.uin, session_valid_until())
            result = await self._registry.start(credentials, grant)
            self._session_id = result.session.session_id
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.info("QR login expired after %.0f seconds", self._scan_timeout_seconds)
        except Exception as exc:
            logger.warning("background QR login failed: %s", type(exc).__name__)
        finally:
            await flow.close()
            if self._scan_flow is flow:
                self._scan_flow = None
            self._clear_qr(token)

    async def status_for_bot(self) -> BotStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return BotStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return BotStatus(
                "connecting",
                "正在连接游戏服务器",
                state=snapshot.state.value,
            )
        return BotStatus(
            "online",
            f"学分：{snapshot.total_exp}，洛克贝：{snapshot.total_money}",
            credits=snapshot.total_exp,
            rock_coins=snapshot.total_money,
            state=snapshot.state.value,
        )

    async def start_hang_for_bot(self) -> BotStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return BotStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return BotStatus("connecting", "正在连接游戏服务器", state=snapshot.state.value)
        result = await self._registry.start_minigame(snapshot.session_id)
        return BotStatus(
            "running",
            "已开始挂机",
            credits=result.total_exp,
            rock_coins=result.total_money,
            state=result.state.value,
        )

    async def online_time_for_bot(self) -> OnlineTimeStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return OnlineTimeStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return OnlineTimeStatus(
                "connecting",
                "正在连接游戏服务器",
                state=snapshot.state.value,
            )
        refreshed = await self._registry.refresh_online_time(snapshot.session_id)
        seconds = refreshed.online_time_seconds
        if seconds is None or seconds < 0:
            return OnlineTimeStatus(
                "unavailable",
                "暂时无法获取在线时间",
                state=refreshed.state.value,
            )
        return OnlineTimeStatus(
            "online",
            f"当前在线时间：{format_duration_compact(seconds)}",
            online_time_seconds=seconds,
            state=refreshed.state.value,
        )

    async def stop_minigame_for_bot(self) -> BotStatus:
        """Stop the minigame while keeping the game connection open."""

        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return BotStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return BotStatus(
                "connecting",
                "正在连接游戏服务器",
                state=snapshot.state.value,
            )
        try:
            result = await self._registry.stop_minigame(snapshot.session_id)
        except MinigameOperationError as exc:
            if exc.error_code == "MINIGAME_NOT_RUNNING":
                return BotStatus(
                    "stopped",
                    "挂机未在运行",
                    credits=snapshot.total_exp,
                    rock_coins=snapshot.total_money,
                    state=snapshot.state.value,
                )
            raise
        return BotStatus(
            "stopped",
            "挂机已停止",
            credits=result.total_exp,
            rock_coins=result.total_money,
            state=result.state.value,
        )

    async def login_with_password(self, account: str, password: str) -> BotStatus:
        """Log in with QQ account/password and start a game session."""

        if not self._window_open():
            raise ServiceClosed()
        async with self._scan_lock:
            await self._discard_terminal_session()
            if await self._usable_snapshot() is not None:
                raise AlreadyConnected()
            if self._scan_task is not None and not self._scan_task.done():
                raise ScanInProgress()

            flow = self._password_flow_factory(account, password)
            try:
                credentials = await flow.login()
            except BaseException as exc:
                await flow.close()
                if isinstance(exc, QqLoginError):
                    logger.warning(
                        "password_login_failed uin=%s error_code=%s error=%s",
                        account[-4:],
                        exc.error_code,
                        _safe_error_text(exc),
                    )
                else:
                    logger.warning(
                        "password_login_failed uin=%s error_type=%s error=%s",
                        account[-4:],
                        type(exc).__name__,
                        _safe_error_text(exc),
                    )
                raise
            await flow.close()

            if not self._window_open():
                return BotStatus("closed", "服务窗口已关闭，无法登录")
            grant = self._access.issue(credentials.uin, session_valid_until())
            result = await self._registry.start(credentials, grant)
            self._session_id = result.session.session_id
            logger.info(
                "password_login_succeeded uin=%s resumed=%s",
                result.session.credentials.uin[-4:],
                result.resumed,
            )
            return await self.status_for_bot()

    async def farm_status_for_bot(self) -> FarmStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return FarmStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return FarmStatus("connecting", "正在连接游戏服务器")
        try:
            farm = await self._registry.refresh_farm(snapshot.session_id)
        except FarmOperationError as exc:
            return FarmStatus("error", str(exc), last_error=str(exc))
        _log_farm_seed_inventory(farm, snapshot.uin)
        return _farm_status_from_snapshot(farm)

    async def harvest_farm_for_bot(self) -> FarmStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return FarmStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return FarmStatus("connecting", "正在连接游戏服务器")
        try:
            farm = await self._registry.harvest_farm(snapshot.session_id)
        except FarmOperationError as exc:
            return FarmStatus(
                "error",
                str(exc),
                last_action=snapshot.farm.last_action if snapshot.farm else None,
                last_error=str(exc),
            )
        return _farm_status_from_snapshot(farm, farm.last_action or "已收获")

    async def plant_auto_for_bot(
        self,
        seed_id: int | None = None,
        fallback_seed_ids: tuple[int, ...] | None = None,
    ) -> FarmStatus:
        """Sow empty unlocked lands; seed picked by priority.

        When seed_id is given only that seed is used. Otherwise the default
        priority applies: preferred seed (乖乖蘑菇) -> fallback seeds
        (小Q牛轧糖) -> the most abundant seed in the inventory.
        """

        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return FarmStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return FarmStatus("connecting", "正在连接游戏服务器")
        session_id = snapshot.session_id
        try:
            farm = await self._registry.refresh_farm(session_id)
            empty = [
                land for land in farm.lands if land.unlocked and land.empty
            ]
            if not empty:
                return _farm_status_from_snapshot(farm, "没有空闲土地可播种")
            available = {
                seed.seed_id: seed.count for seed in farm.seeds if seed.count > 0
            }
            if not available:
                return _farm_status_from_snapshot(farm, "种子背包为空，无法播种")
            if seed_id is not None:
                if seed_id not in available:
                    return _farm_status_from_snapshot(
                        farm,
                        f"种子 {seed_id} 不在背包中，本次未播种",
                    )
                chosen = seed_id
            else:
                chosen = pick_seed_id(
                    available,
                    self._preferred_seed_id,
                    (
                        fallback_seed_ids
                        if fallback_seed_ids is not None
                        else self._fallback_seed_ids
                    ),
                )
            planted = 0
            for land in empty:
                try:
                    await self._registry.plant_farm(session_id, land.ground_id, chosen)
                except FarmOperationError:
                    break
                planted += 1
            farm = await self._registry.refresh_farm(session_id)
            message = (
                f"已播种 {planted} 块土地（种子 {chosen}）"
                if planted
                else "没有土地播种成功"
            )
            return _farm_status_from_snapshot(farm, message)
        except FarmOperationError as exc:
            return FarmStatus("error", str(exc), last_error=str(exc))

    async def paradise_status_for_bot(self) -> ParadiseStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return ParadiseStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return ParadiseStatus("connecting", "正在连接游戏服务器")
        try:
            paradise = await self._registry.refresh_paradise(snapshot.session_id)
        except ParadiseOperationError as exc:
            return ParadiseStatus("error", str(exc), last_error=str(exc))
        return _paradise_status_from_snapshot(paradise)

    async def start_paradise_for_bot(self) -> ParadiseStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return ParadiseStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return ParadiseStatus("connecting", "正在连接游戏服务器")
        try:
            paradise = await self._registry.start_paradise_adventure(
                snapshot.session_id
            )
        except ParadiseOperationError as exc:
            return ParadiseStatus("error", str(exc), last_error=str(exc))
        return _paradise_status_from_snapshot(
            paradise,
            paradise.last_action or "已开始探险",
        )

    async def claim_paradise_for_bot(self) -> ParadiseStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            return ParadiseStatus("not_logged_in", "暂未登录")
        if not snapshot.connected:
            return ParadiseStatus("connecting", "正在连接游戏服务器")
        try:
            paradise = await self._registry.claim_paradise_rewards(
                snapshot.session_id
            )
        except ParadiseOperationError as exc:
            return ParadiseStatus("error", str(exc), last_error=str(exc))
        return _paradise_status_from_snapshot(
            paradise,
            paradise.last_action or "奖励已领取",
        )

    async def start_automation(
        self,
        *,
        farm: bool = True,
        paradise: bool = True,
        hang: bool = True,
        log_interval: int = 5,
        farm_interval: int = 60,
        paradise_interval: int = 15,
        hang_minutes: int = 30,
        hang_cooldown_minutes: int = 5,
        preferred_seed_id: int | None = None,
        fallback_seed_ids: tuple[int, ...] | None = None,
    ) -> AutomationStatus:
        """Start the fully automatic loop (password login mode)."""

        async with self._automation_lock:
            task = self._automation_task
            if task is not None and not task.done():
                raise AutomationOperationError(
                    "AUTOMATION_ALREADY_RUNNING",
                    "自动任务已经启动",
                )
            snapshot = await self._usable_snapshot()
            if snapshot is None:
                raise AutomationOperationError(
                    "AUTOMATION_NOT_LOGGED_IN",
                    "暂未登录，无法启动自动任务",
                )
            if not snapshot.connected:
                raise AutomationOperationError(
                    "AUTOMATION_SESSION_NOT_READY",
                    "游戏尚未连接完成，无法启动自动任务",
                )
            self._automation_cfg = {
                "farm": bool(farm),
                "paradise": bool(paradise),
                "hang": bool(hang),
                "log_interval": max(1, int(log_interval)),
                "farm_interval": max(5, int(farm_interval)),
                "paradise_interval": max(5, int(paradise_interval)),
                "hang_minutes": max(1, int(hang_minutes)),
                "hang_cooldown_minutes": max(1, int(hang_cooldown_minutes)),
                "preferred_seed_id": (
                    preferred_seed_id
                    if preferred_seed_id is not None
                    else self._preferred_seed_id
                ),
                "fallback_seed_ids": tuple(
                    fallback_seed_ids
                    if fallback_seed_ids is not None
                    else self._fallback_seed_ids
                ),
            }
            self._automation_stats = {
                "harvested": 0,
                "planted": 0,
                "adventures": 0,
                "claims": 0,
                "failures": 0,
            }
            self._automation_started_at = datetime.now(LOG_SHANGHAI)
            self._automation_last_log = None
            self._auto_hang_state = "idle"
            self._auto_hang_until = 0.0
            self._auto_cooldown_until = 0.0
            self._automation_stop.clear()
            # Initial pass: status/paradise first, then farm, then hang
            # (the hang loop takes over and starts the minigame on its first tick).
            if self._automation_cfg.get("paradise"):
                await self._automation_paradise_once()
            if self._automation_cfg.get("farm"):
                await self._automation_farm_while_idle()
            await self._automation_log_once()
            self._automation_task = asyncio.create_task(self._automation_loop())
            logger.info(
                "automation_started farm=%s paradise=%s hang=%s log_interval=%d",
                farm,
                paradise,
                hang,
                self._automation_cfg["log_interval"],
            )
            return self.automation_status()

    async def stop_automation(self) -> AutomationStatus:
        async with self._automation_lock:
            task = self._automation_task
            if task is None or task.done():
                self._automation_task = None
                return self.automation_status()
            self._automation_stop.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._automation_task = None
            logger.info("automation_stopped")
            return self.automation_status()

    def automation_status(self) -> AutomationStatus:
        task = self._automation_task
        active = task is not None and not task.done()
        cfg = self._automation_cfg
        stats = self._automation_stats
        started_at = self._automation_started_at
        return AutomationStatus(
            status="running" if active else "stopped",
            message="自动任务运行中" if active else "自动任务未运行",
            active=active,
            started_at=(
                None if started_at is None else started_at.isoformat(timespec="seconds")
            ),
            last_log=self._automation_last_log,
            log_interval_seconds=int(cfg.get("log_interval", 0)),
            farm_enabled=bool(cfg.get("farm", False)),
            paradise_enabled=bool(cfg.get("paradise", False)),
            hang_enabled=bool(cfg.get("hang", False)),
            hang_state=self._auto_hang_state,
            farm_interval_seconds=int(cfg.get("farm_interval", 0)),
            paradise_interval_seconds=int(cfg.get("paradise_interval", 0)),
            hang_minutes=int(cfg.get("hang_minutes", 0)),
            hang_cooldown_minutes=int(cfg.get("hang_cooldown_minutes", 0)),
            harvested=stats.get("harvested", 0),
            planted=stats.get("planted", 0),
            adventures=stats.get("adventures", 0),
            claims=stats.get("claims", 0),
            failures=stats.get("failures", 0),
        )

    async def _automation_loop(self) -> None:
        cfg = self._automation_cfg
        farm_enabled = bool(cfg.get("farm"))
        paradise_enabled = bool(cfg.get("paradise"))
        hang_enabled = bool(cfg.get("hang"))
        log_interval = max(1.0, float(cfg.get("log_interval", 5)))
        farm_interval = max(5.0, float(cfg.get("farm_interval", 60)))
        paradise_interval = max(5.0, float(cfg.get("paradise_interval", 15)))
        now = time.monotonic()
        next_log = now + log_interval
        next_farm = now + 5.0
        next_paradise = now + 5.0
        next_hang = now + 5.0
        try:
            while not self._automation_stop.is_set():
                now = time.monotonic()
                if now >= next_log:
                    try:
                        await self._automation_log_once()
                    except Exception as exc:
                        logger.warning(
                            "automation_log_failed error_type=%s",
                            type(exc).__name__,
                        )
                    next_log = now + log_interval
                if farm_enabled and now >= next_farm:
                    await self._automation_farm_while_idle()
                    next_farm = now + farm_interval
                if paradise_enabled and now >= next_paradise:
                    await self._automation_paradise_once()
                    next_paradise = now + paradise_interval
                if hang_enabled and now >= next_hang:
                    await self._automation_hang_tick()
                    next_hang = now + 5.0
                try:
                    await asyncio.wait_for(
                        self._automation_stop.wait(), timeout=1.0
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._automation_task = None

    async def _automation_hang_tick(self) -> None:
        """Manage the minigame lifecycle: idle -> hanging -> cooldown -> hang.

        The minigame keeps running for `hang_minutes`; when the timer expires
        the automation pauses it, farms while the connection is idle, waits out
        the cooldown (洛克王国免时挂机 5 分钟间隔) and restarts the minigame.
        """

        now = time.monotonic()
        cfg = self._automation_cfg
        hang_seconds = max(1.0, float(cfg.get("hang_minutes", 30))) * 60.0
        cooldown = max(1.0, float(cfg.get("hang_cooldown_minutes", 5))) * 60.0
        snapshot = await self._usable_snapshot()
        if snapshot is None or not snapshot.connected:
            self._auto_hang_state = "idle"
            return
        session_id = snapshot.session_id
        running = bool(snapshot.minigame_active)
        state = self._auto_hang_state

        if state == "hanging":
            if not running:
                # The minigame stopped by itself; wait out the cooldown.
                self._auto_hang_state = "cooldown"
                self._auto_cooldown_until = now + cooldown
                logger.info("automation_hang_stopped_unexpectedly")
            elif now >= self._auto_hang_until:
                # Planned pause: exit the minigame to farm, then cooldown.
                try:
                    await self._registry.stop_minigame(session_id)
                except MinigameOperationError as exc:
                    logger.warning(
                        "automation_hang_pause_failed error_code=%s",
                        exc.error_code,
                    )
                    self._auto_hang_until = now + 60.0
                    return
                self._auto_hang_state = "cooldown"
                self._auto_cooldown_until = now + cooldown
                logger.info("automation_hang_paused_for_farm")
        elif state == "cooldown":
            if now >= self._auto_cooldown_until:
                await self._automation_farm_while_idle()
                try:
                    await self._registry.start_minigame(session_id)
                except MinigameOperationError as exc:
                    self._auto_hang_state = "cooldown"
                    self._auto_cooldown_until = now + cooldown
                    logger.warning(
                        "automation_hang_restart_failed error_code=%s",
                        exc.error_code,
                    )
                    return
                self._auto_hang_state = "hanging"
                self._auto_hang_until = now + hang_seconds
                logger.info("automation_hang_restarted")
        else:  # idle
            if running:
                self._auto_hang_state = "hanging"
                self._auto_hang_until = now + hang_seconds
                return
            try:
                await self._registry.start_minigame(session_id)
            except MinigameOperationError as exc:
                logger.warning(
                    "automation_hang_start_failed error_code=%s",
                    exc.error_code,
                )
                return
            self._auto_hang_state = "hanging"
            self._auto_hang_until = now + hang_seconds
            logger.info("automation_hang_started")

    async def _automation_log_once(self) -> None:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            self._automation_last_log = "暂未登录"
            return
        paradise = snapshot.paradise
        countdown = paradise.countdown if paradise is not None else -1
        times = paradise.times if paradise is not None else -1
        limit = paradise.limit if paradise is not None else -1
        remaining = (
            max(0, limit - times)
            if paradise is not None and limit > 0 and times >= 0
            else -1
        )
        countdown_text = _paradise_countdown_text(countdown)
        online = snapshot.online_time_seconds or 0
        line = (
            f"洛克贝={snapshot.total_money} 学分={snapshot.total_exp} "
            f"在线时间={format_duration(online)} "
            f"乐园剩余次数={remaining if remaining >= 0 else '未知'} "
            f"本次探险={countdown_text} 状态={snapshot.state.value}"
        )
        self._automation_last_log = line
        logger.info("automation_status uin=%s %s", snapshot.uin[-4:], line)

    async def _automation_farm_while_idle(self) -> None:
        """Harvest and sow while the minigame is NOT running.

        The game protocol rejects farm operations during the minigame, so this
        only acts when the session is idle (ready / cooldown window).
        """

        snapshot = await self._usable_snapshot()
        if snapshot is None or not snapshot.connected:
            return
        if snapshot.minigame_active:
            return
        session_id = snapshot.session_id
        try:
            farm = await self._registry.refresh_farm(session_id)
            harvestable = [
                land for land in farm.lands if land.unlocked and land.has_fruit
            ]
            if harvestable:
                farm = await self._registry.harvest_farm(session_id)
                self._automation_stats["harvested"] += len(harvestable)
                logger.info(
                    "automation_farm_harvested uin=%s lands=%d",
                    snapshot.uin[-4:],
                    len(harvestable),
                )
            empty = [land for land in farm.lands if land.unlocked and land.empty]
            available = {
                seed.seed_id: seed.count
                for seed in farm.seeds
                if seed.count > 0
            }
            if empty and available:
                chosen = pick_seed_id(
                    available,
                    self._automation_cfg.get("preferred_seed_id"),
                    tuple(
                        self._automation_cfg.get("fallback_seed_ids") or ()
                    ),
                )
                if chosen is None:
                    chosen = max(available, key=lambda sid: available[sid])
                planted = 0
                for land in empty:
                    try:
                        await self._registry.plant_farm(
                            session_id, land.ground_id, chosen
                        )
                    except FarmOperationError:
                        break
                    planted += 1
                if planted:
                    self._automation_stats["planted"] += planted
                    logger.info(
                        "automation_farm_planted uin=%s seed_id=%d lands=%d",
                        snapshot.uin[-4:],
                        chosen,
                        planted,
                    )
        except (FarmOperationError, GameSessionNotFound, GameSessionExpired) as exc:
            self._automation_stats["failures"] += 1
            logger.warning(
                "automation_farm_failed uin=%s error_type=%s",
                snapshot.uin[-4:],
                type(exc).__name__,
            )

    async def _automation_paradise_once(self) -> None:
        snapshot = await self._usable_snapshot()
        if snapshot is None or not snapshot.connected:
            return
        session_id = snapshot.session_id
        try:
            paradise = await self._registry.refresh_paradise(session_id)
            countdown = paradise.countdown
            times = paradise.times
            limit = paradise.limit
            if countdown == 0:
                await self._registry.claim_paradise_rewards(session_id)
                self._automation_stats["claims"] += 1
                logger.info(
                    "automation_paradise_claimed uin=%s",
                    snapshot.uin[-4:],
                )
            elif (
                countdown == -1
                and paradise.spirit_ids
                and (limit <= 0 or times < limit)
            ):
                await self._registry.start_paradise_adventure(session_id)
                self._automation_stats["adventures"] += 1
                logger.info(
                    "automation_paradise_started uin=%s spirits=%d",
                    snapshot.uin[-4:],
                    len(paradise.spirit_ids),
                )
        except (ParadiseOperationError, GameSessionNotFound, GameSessionExpired) as exc:
            self._automation_stats["failures"] += 1
            logger.warning(
                "automation_paradise_failed uin=%s error_type=%s",
                snapshot.uin[-4:],
                type(exc).__name__,
            )

    async def disconnect_for_bot(self) -> BotStatus:
        snapshot = await self._usable_snapshot()
        if snapshot is None:
            await self._cancel_pending_scan()
            return BotStatus("not_logged_in", "暂未登录")
        await self._registry.disconnect(snapshot.session_id)
        self._clear_session()
        return BotStatus("disconnected", "已断开连接")

    async def close(self) -> None:
        self._automation_stop.set()
        automation = self._automation_task
        if automation is not None and not automation.done():
            automation.cancel()
            await asyncio.gather(automation, return_exceptions=True)
        self._automation_task = None
        await self._cancel_pending_scan()
        await self._registry.close_all()
        self._clear_session()

    async def _usable_snapshot(self) -> SessionSnapshot | None:
        await self._discard_terminal_session()
        snapshot = await self._active_snapshot()
        if (
            snapshot is not None
            and not snapshot.connected
            and snapshot.state
            in {
                SessionState.READY,
                SessionState.WAITING_BEFORE_JUMP,
                SessionState.JUMPING_TO_BANK,
                SessionState.ENABLING_TIME_STOP,
                SessionState.RUNNING,
                SessionState.PICKUP_RUNNING,
            }
        ):
            try:
                await self._registry.disconnect(snapshot.session_id)
            except Exception:
                pass
            self._clear_session()
            return None
        return snapshot

    async def _discard_terminal_session(self) -> None:
        snapshot = await self._active_snapshot()
        if snapshot is not None and snapshot.state in {SessionState.CLOSED, SessionState.ERROR}:
            self._clear_session()

    async def _active_snapshot(self) -> SessionSnapshot | None:
        if self._session_id is None:
            return None
        try:
            return await self._registry.snapshot(self._session_id)
        except (GameSessionNotFound, GameSessionExpired):
            self._clear_session()
            return None

    def _clear_session(self) -> None:
        self._session_id = None
        self._access.clear()

    def _clear_qr(self, token: str | None = None) -> None:
        if token is not None and token != self._qr_token:
            return
        self._qr_token = None
        self._qr_image = None
        self._qr_expires_at = 0.0

    async def _cancel_pending_scan(self) -> None:
        task = self._scan_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        flow = self._scan_flow
        if flow is not None:
            await flow.close()
            self._scan_flow = None
        self._scan_task = None
        self._clear_qr()


def _farm_status_from_snapshot(
    farm: FarmSnapshot,
    message: str | None = None,
) -> FarmStatus:
    lands = farm.lands
    planted = sum(1 for land in lands if land.unlocked and not land.empty)
    harvestable = sum(1 for land in lands if land.unlocked and land.has_fruit)
    empty = sum(1 for land in lands if land.unlocked and land.empty)
    seeds = tuple(
        {"seed_id": seed.seed_id, "count": seed.count}
        for seed in farm.seeds
    )
    default_message = (
        f"庄园等级 {farm.manor_level or '未知'}，"
        f"土地 {len(lands)} 块（已种 {planted}，可收获 {harvestable}，"
        f"空闲 {empty}），种子 {len(seeds)} 种"
    )
    return FarmStatus(
        status="ok",
        message=message or default_message,
        manor_level=farm.manor_level,
        land_count=len(lands),
        planted_count=planted,
        harvestable_count=harvestable,
        empty_count=empty,
        seeds=seeds,
        updated_at=farm.updated_at,
        last_action=farm.last_action,
        last_error=farm.last_error,
    )


def _paradise_status_from_snapshot(
    paradise: ParadiseSnapshot,
    message: str | None = None,
) -> ParadiseStatus:
    times = paradise.times
    limit = paradise.limit
    remaining = max(0, limit - times) if limit > 0 and times >= 0 else -1
    default_message = (
        f"乐园等级 {paradise.level or '未知'}，精灵 {len(paradise.spirit_ids)} 只，"
        f"探险次数 {times if times >= 0 else '未知'}"
        f"{f'/{limit}' if limit > 0 else ''}"
        f"{f'（剩余 {remaining} 次）' if remaining >= 0 else ''}，"
        f"本次探险{_paradise_countdown_text(paradise.countdown)}"
    )
    reward = paradise.last_reward
    return ParadiseStatus(
        status="ok",
        message=message or default_message,
        level=paradise.level,
        experience=paradise.experience,
        spirit_count=len(paradise.spirit_ids),
        countdown=paradise.countdown,
        participants=paradise.participants,
        times=times,
        limit=limit,
        remaining=remaining,
        updated_at=paradise.updated_at,
        last_action=paradise.last_action,
        last_error=paradise.last_error,
        last_reward=(
            None
            if reward is None
            else {
                "experience": reward.experience,
                "reward_type": reward.reward_type,
                "items": [
                    {"item_id": item.item_id, "count": item.count, "item_type": item.item_type}
                    for item in reward.items
                ],
            }
        ),
    )


def _paradise_countdown_text(countdown: int) -> str:
    if countdown == 0:
        return "可领奖"
    if countdown < 0:
        return "空闲"
    return format_duration(countdown)


def _safe_error_text(exc: BaseException) -> str:
    """Bound and sanitize an exception message for logs."""

    message = str(exc) or type(exc).__name__
    return " ".join(message.split())[:200]


def pick_seed_id(
    available: dict[int, int],
    preferred: int | None = DEFAULT_PREFERRED_SEED_ID,
    fallbacks: tuple[int, ...] = DEFAULT_FALLBACK_SEED_IDS,
) -> int | None:
    """Pick the seed to sow: preferred -> fallbacks -> most abundant."""

    if not available:
        return None
    candidates = []
    if preferred is not None:
        candidates.append(preferred)
    candidates.extend(fallbacks)
    for seed_id in candidates:
        if seed_id in available:
            return seed_id
    return max(available, key=lambda seed_id: available[seed_id])


def _log_farm_seed_inventory(farm: FarmSnapshot, uin: str) -> None:
    """Log the raw seed inventory so the preferred seed id can be matched."""

    if not farm.seeds:
        logger.info("farm_seed_inventory uin=%s seeds=empty", uin[-4:])
        return
    listing = ", ".join(
        f"0x{seed.seed_id:08X}×{seed.count}" for seed in farm.seeds
    )
    logger.info("farm_seed_inventory uin=%s seeds=%s", uin[-4:], listing)
