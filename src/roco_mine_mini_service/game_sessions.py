"""Per-UIN game connections and fully automatic minigame execution."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

import httpx

from .access import AccessGrant, AccessProvider
from .game_protocol import (
    DIR_INIT,
    ENTER_ROOM,
    FARM_SEED_INVENTORY,
    MANOR_GROUND_INFO,
    MANOR_LAND_COUNT,
    MANOR_PLANT_REAP,
    MANOR_PLANT_SOW,
    ONLINE_TIME,
    PARADISE_SPIRIT_LIST,
    RECOMMEND_ROOM_REPLY,
    ROOM_INIT_COMPLETE,
    SCENE_JUMP,
    TIME_PAUSE,
    FarmLand,
    FarmSeed,
    GameProtocolError,
    Packet,
    PacketAssembler,
    Room,
    build_farm_seed_inventory_request,
    build_enter_room_request,
    build_heartbeat,
    build_manor_query_request,
    build_manor_reap_request,
    build_manor_sow_request,
    build_minigame_start_request,
    build_online_time_request,
    build_paradise_spirit_list_request,
    build_recommend_room_request,
    build_scene_jump_request,
    build_time_pause_request,
    choose_least_populated_room,
    parse_enter_room_response,
    parse_farm_seed_inventory_response,
    parse_manor_query_response,
    parse_manor_reap_response,
    parse_manor_sow_response,
    parse_online_time_response,
    parse_paradise_spirit_list_response,
    parse_packet,
    parse_recommended_rooms,
    parse_scene_jump_response,
    parse_time_pause_response,
    require_success,
)
from .qq_login import LoginCredentials
from .logging_setup import masked_uin
from .pickup import PICKUP_GROUPS_BY_KEY, PickupGroup, PickupItem

SHANGHAI = ZoneInfo("Asia/Shanghai")
GAME_ID = 2121
BANK_SCENE_ID = 25
GAME_SCORE = 5000
GAME_TYPE = 2
GAME_INTERVAL_SECONDS = 5.0
FCGI_BASE_URL = "https://web2.17roco.qq.com/fcgi-bin/"
PARADISE_URL = "https://17roco.qq.com/cgi-bin/paradise_experience"
GAME_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.6261.95 Safari/537.36"
)
AUTHORIZATION_CHECK_INTERVAL_SECONDS = 5 * 60.0
ONLINE_TIME_REFRESH_INTERVAL_SECONDS = 60.0
ANGEL_KEY_REFRESH_ATTEMPTS = 3
ANGEL_KEY_REFRESH_RETRY_SECONDS = 5.0
AUTHORIZATION_EXPIRED_CODE = "AUTHORIZATION_EXPIRED"
AUTHORIZATION_EXPIRED_MESSAGE = "当日连接已过期，挂机已停止，请重新扫码"
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_SENSITIVE_RESPONSE_FIELD_PATTERN = re.compile(
    r"(?i)\b(angel_key|angel_uin|p_skey|pskey|skey|cookie|access_grant)\b"
    r"\s*(?:=|:)\s*[^&,;\s]+"
)
_LONG_SECRET_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_UIN_IN_MESSAGE_PATTERN = re.compile(r"(?<!\d)([1-9][0-9]{4,19})(?!\d)")
_MAX_SAFE_RESPONSE_MESSAGE_CHARS = 200
logger = logging.getLogger(__name__)


def service_window_open(now: datetime | None = None) -> bool:
    current = now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)
    return current.hour != 23


def session_valid_until(now: datetime | None = None) -> datetime:
    """Return the 23:00 Asia/Shanghai cutoff for the current local day."""

    current = now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)
    return current.replace(hour=23, minute=0, second=0, microsecond=0)


class GameAutomationError(Exception):
    pass


class GameSessionNotFound(Exception):
    pass


class GameSessionExpired(Exception):
    pass


class GameCapacityReached(Exception):
    pass


class FarmOperationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ParadiseOperationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class MinigameOperationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class PickupOperationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SessionState(StrEnum):
    CREATED = "created"
    CONNECTING_DIRECTORY = "connecting_directory"
    SELECTING_ROOM = "selecting_room"
    CONNECTING_GAME = "connecting_game"
    ENTERING_ROOM = "entering_room"
    READY = "ready"
    WAITING_BEFORE_JUMP = "waiting_before_jump"
    JUMPING_TO_BANK = "jumping_to_bank"
    ENABLING_TIME_STOP = "enabling_time_stop"
    RUNNING = "running"
    PICKUP_RUNNING = "pickup_running"
    CLOSED = "closed"
    ERROR = "error"


class AsyncPacketConnection:
    """One TCP connection with its own assembler, waiters, and write lock."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._assembler = PacketAssembler()
        self._waiters: dict[int, deque[asyncio.Future[Packet]]] = defaultdict(deque)
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._close_started = False
        self.last_receive_at = time.monotonic()
        self._receiver_task = asyncio.create_task(self._receive_loop())

    @property
    def is_open(self) -> bool:
        return not self._closed

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        fallback_host: str | None = None,
        timeout: float = 10.0,
    ) -> AsyncPacketConnection:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except (OSError, TimeoutError):
            if not fallback_host or fallback_host == host:
                raise
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(fallback_host, port), timeout=timeout
            )
        return cls(reader, writer)

    def expect(self, command: int) -> asyncio.Future[Packet]:
        if self._closed:
            raise ConnectionError("connection is closed")
        future: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()
        self._waiters[command].append(future)
        return future

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("connection is closed")
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def send_and_wait(
        self,
        data: bytes,
        response_command: int,
        *,
        timeout: float = 5.0,
    ) -> Packet:
        future = self.expect(response_command)
        try:
            await self.send(data)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            if not future.done():
                future.cancel()

    async def close(self) -> None:
        if self._close_started:
            return
        self._close_started = True
        self._closed = True
        self._receiver_task.cancel()
        if self._receiver_task is not asyncio.current_task():
            await asyncio.gather(self._receiver_task, return_exceptions=True)
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self._fail_waiters(ConnectionError("connection closed"))

    async def _receive_loop(self) -> None:
        failure: BaseException | None = None
        try:
            while True:
                chunk = await self._reader.read(8192)
                if not chunk:
                    raise ConnectionError("remote server closed the connection")
                self.last_receive_at = time.monotonic()
                for raw_packet in self._assembler.feed(chunk):
                    packet = parse_packet(raw_packet)
                    waiters = self._waiters.get(packet.command)
                    while waiters:
                        waiter = waiters.popleft()
                        if not waiter.done():
                            waiter.set_result(packet)
                            break
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
        finally:
            if failure is not None:
                self._closed = True
                self._fail_waiters(failure)

    def _fail_waiters(self, failure: BaseException) -> None:
        for waiters in self._waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(failure)
        self._waiters.clear()


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    uin: str
    state: SessionState
    connected: bool
    room_id: int | None
    scene_id: int | None
    online_time_seconds: int | None
    rounds: int
    total_money: int
    total_exp: int
    access_expires_at: str
    valid_until: str
    login_at: str
    started_at: str
    time_stop_active: bool
    last_error_code: str | None
    last_error: str | None
    farm: FarmSnapshot | None = None
    paradise: ParadiseSnapshot | None = None
    minigame_active: bool = False
    minigame_started_at: str | None = None
    pickup: PickupSnapshot | None = None


@dataclass(frozen=True, slots=True)
class FarmSnapshot:
    manor_level: int | None
    lands: tuple[FarmLand, ...]
    seeds: tuple[FarmSeed, ...]
    updated_at: str | None
    last_action: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ParadiseRewardItem:
    item_id: int
    count: int
    item_type: int


@dataclass(frozen=True, slots=True)
class ParadiseReward:
    experience: int
    reward_type: int
    items: tuple[ParadiseRewardItem, ...]


@dataclass(frozen=True, slots=True)
class ParadiseSnapshot:
    level: int | None
    experience: int | None
    spirit_ids: tuple[int, ...]
    countdown: int
    participants: int
    times: int
    limit: int
    updated_at: str | None
    last_action: str | None
    last_error: str | None
    last_reward: ParadiseReward | None


@dataclass(frozen=True, slots=True)
class PickupResponse:
    group: str
    item_id: int
    result: int
    message: str


@dataclass(frozen=True, slots=True)
class PickupSnapshot:
    active: bool
    selected_groups: tuple[str, ...]
    completed: int
    total: int
    failed: int
    current_group: str | None
    current_item: int | None
    started_at: str | None
    last_action: str | None
    last_error: str | None
    responses: tuple[PickupResponse, ...]


@dataclass(frozen=True, slots=True)
class GameCapacity:
    current_connections: int
    max_connections: int

    @property
    def full(self) -> bool:
        return self.current_connections >= self.max_connections


FinishedCallback = Callable[[str, "GameSession"], Awaitable[None]]
AccessGrantProvider = Callable[[str], AccessGrant | None]


class GameSession:
    """An isolated directory/game client and minigame loop for one UIN."""

    def __init__(
        self,
        access_provider: AccessProvider,
        credentials: LoginCredentials,
        access_grant: AccessGrant,
        *,
        directory_host: str = "dir.17roco.qq.com",
        directory_port: int = 443,
        session_id: str | None = None,
        valid_until: datetime | None = None,
        started_at: datetime | None = None,
        on_finished: FinishedCallback | None = None,
        window_open: Callable[[], bool] = service_window_open,
        current_grant: AccessGrantProvider | None = None,
    ) -> None:
        self.access_provider = access_provider
        self._current_grant = (
            current_grant or access_provider.current_grant
        )
        self.credentials = credentials
        self.access_grant = access_grant
        self.directory_host = directory_host
        self.directory_port = directory_port
        self.session_id = session_id or secrets.token_urlsafe(32)
        self.valid_until = valid_until or session_valid_until()
        self.login_at = credentials.login_at.astimezone(SHANGHAI)
        self.started_at = (
            started_at.astimezone(SHANGHAI)
            if started_at is not None
            else datetime.now(SHANGHAI)
        )
        self.on_finished = on_finished
        self._window_open = window_open
        self.state = SessionState.CREATED
        self.room: Room | None = None
        self.scene_id: int | None = None
        self.online_time_seconds: int | None = None
        self.rounds = 0
        self.total_money = 0
        self.total_exp = 0
        self.time_stop_active = False
        self.last_error_code: str | None = None
        self.last_error: str | None = None
        self.minigame_started_at: datetime | None = None
        self._farm_manor_level: int | None = None
        self._farm_lands: dict[int, FarmLand] = {}
        self._farm_seeds: dict[int, FarmSeed] = {}
        self._farm_updated_at: str | None = None
        self._farm_last_action: str | None = None
        self._farm_last_error: str | None = None
        self._farm_lock = asyncio.Lock()
        self._paradise_level: int | None = None
        self._paradise_experience: int | None = None
        self._paradise_spirit_ids: tuple[int, ...] = ()
        self._paradise_countdown = -2
        self._paradise_participants = -1
        self._paradise_times = -1
        self._paradise_limit = -1
        self._paradise_updated_at: str | None = None
        self._paradise_last_action: str | None = None
        self._paradise_last_error: str | None = None
        self._paradise_last_reward: ParadiseReward | None = None
        self._paradise_lock = asyncio.Lock()
        self._pickup_active = False
        self._pickup_selected_groups: tuple[str, ...] = ()
        self._pickup_completed = 0
        self._pickup_total = 0
        self._pickup_failed = 0
        self._pickup_current_group: str | None = None
        self._pickup_current_item: int | None = None
        self._pickup_started_at: datetime | None = None
        self._pickup_last_action: str | None = None
        self._pickup_last_error: str | None = None
        self._pickup_responses: list[PickupResponse] = []
        self._pickup_task: asyncio.Task[None] | None = None
        self._pickup_stop_event = asyncio.Event()
        self._pickup_control_lock = asyncio.Lock()
        self._activity_lock = asyncio.Lock()
        self._directory: AsyncPacketConnection | None = None
        self._game: AsyncPacketConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._minigame_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._minigame_control_lock = asyncio.Lock()
        self._minigame_round_lock = asyncio.Lock()
        self._online_time_lock = asyncio.Lock()
        self._minigame_stop_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._finished_event = asyncio.Event()
        self._http: httpx.AsyncClient | None = None
        self._cleanup_lock = asyncio.Lock()
        self._cleaned_up = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._runner())

    @property
    def is_active(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self.state not in {SessionState.CLOSED, SessionState.ERROR}
        )

    def snapshot(self) -> SessionSnapshot:
        game = self._game
        return SessionSnapshot(
            session_id=self.session_id,
            uin=self.credentials.uin,
            state=self.state,
            connected=game is not None and game.is_open,
            room_id=None if self.room is None else self.room.room_index,
            scene_id=self.scene_id,
            online_time_seconds=self.online_time_seconds,
            rounds=self.rounds,
            total_money=self.total_money,
            total_exp=self.total_exp,
            access_expires_at=self.access_grant.expires_at.isoformat(),
            valid_until=self.valid_until.isoformat(),
            login_at=self.login_at.isoformat(timespec="seconds"),
            started_at=self.started_at.isoformat(timespec="seconds"),
            time_stop_active=self.time_stop_active,
            last_error_code=self.last_error_code,
            last_error=self.last_error,
            farm=self.farm_snapshot(),
            paradise=self.paradise_snapshot(),
            pickup=self.pickup_snapshot(),
            minigame_active=(
                self.state
                in {
                    SessionState.WAITING_BEFORE_JUMP,
                    SessionState.JUMPING_TO_BANK,
                    SessionState.ENABLING_TIME_STOP,
                    SessionState.RUNNING,
                }
                or (
                    self._minigame_task is not None
                    and not self._minigame_task.done()
                )
            ),
            minigame_started_at=(
                None
                if self.minigame_started_at is None
                else self.minigame_started_at.isoformat(timespec="seconds")
            ),
        )

    def farm_snapshot(self) -> FarmSnapshot:
        return FarmSnapshot(
            manor_level=self._farm_manor_level,
            lands=tuple(
                self._farm_lands[ground_id]
                for ground_id in sorted(self._farm_lands)
            ),
            seeds=tuple(
                self._farm_seeds[seed_id]
                for seed_id in sorted(self._farm_seeds)
                if self._farm_seeds[seed_id].count > 0
            ),
            updated_at=self._farm_updated_at,
            last_action=self._farm_last_action,
            last_error=self._farm_last_error,
        )

    def paradise_snapshot(self) -> ParadiseSnapshot:
        return ParadiseSnapshot(
            level=self._paradise_level,
            experience=self._paradise_experience,
            spirit_ids=self._paradise_spirit_ids,
            countdown=self._paradise_countdown,
            participants=self._paradise_participants,
            times=self._paradise_times,
            limit=self._paradise_limit,
            updated_at=self._paradise_updated_at,
            last_action=self._paradise_last_action,
            last_error=self._paradise_last_error,
            last_reward=self._paradise_last_reward,
        )

    def pickup_snapshot(self) -> PickupSnapshot:
        return PickupSnapshot(
            active=self._pickup_active,
            selected_groups=self._pickup_selected_groups,
            completed=self._pickup_completed,
            total=self._pickup_total,
            failed=self._pickup_failed,
            current_group=self._pickup_current_group,
            current_item=self._pickup_current_item,
            started_at=(
                None
                if self._pickup_started_at is None
                else self._pickup_started_at.isoformat(timespec="seconds")
            ),
            last_action=self._pickup_last_action,
            last_error=self._pickup_last_error,
            responses=tuple(self._pickup_responses),
        )

    async def refresh_farm(self) -> FarmSnapshot:
        async with self._farm_lock:
            try:
                return await self._refresh_farm_locked()
            except FarmOperationError:
                raise
            except Exception as exc:
                self._raise_farm_failure(
                    "refresh",
                    "FARM_REFRESH_FAILED",
                    "农场数据读取失败，请稍后重试",
                    exc,
                )

    async def harvest_farm(self, ground_id: int | None = None) -> FarmSnapshot:
        async with self._farm_lock:
            try:
                game = self._require_farm_connection()
                if len(self._farm_lands) != MANOR_LAND_COUNT:
                    await self._refresh_farm_locked()
                if ground_id is None:
                    targets = [
                        land
                        for land in self._farm_lands.values()
                        if land.unlocked and land.has_fruit
                    ]
                else:
                    land = self._farm_lands.get(ground_id)
                    if land is None:
                        raise FarmOperationError(
                            "FARM_LAND_NOT_FOUND",
                            "指定土地不存在，请刷新后重试",
                        )
                    if not land.unlocked:
                        raise FarmOperationError(
                            "FARM_LAND_LOCKED",
                            f"第 {ground_id + 1} 块土地尚未解锁",
                        )
                    if not land.has_fruit:
                        raise FarmOperationError(
                            "FARM_NOT_HARVESTABLE",
                            f"第 {ground_id + 1} 块土地当前没有可收获作物",
                        )
                    targets = [land]
                if not targets:
                    self._farm_last_action = "当前没有可收获作物"
                    self._farm_last_error = None
                    return self.farm_snapshot()

                total_fruit = 0
                total_experience = 0
                for index, land in enumerate(sorted(targets, key=lambda item: item.ground_id)):
                    response = await game.send_and_wait(
                        build_manor_reap_request(
                            int(self.credentials.uin),
                            land.ground_id,
                            pskey=self.credentials.pskey,
                            skey=self.credentials.skey,
                        ),
                        MANOR_PLANT_REAP,
                        timeout=5.0,
                    )
                    result = parse_manor_reap_response(response)
                    if result.result != 0:
                        raise GameProtocolError(
                            f"manor reap was rejected ({result.result})"
                        )
                    if result.land.ground_id != land.ground_id:
                        raise GameProtocolError(
                            "manor-reap response returned another land"
                        )
                    self._farm_lands[result.land.ground_id] = result.land
                    total_fruit += result.fruit_count
                    total_experience += result.experience
                    if index + 1 < len(targets):
                        await self._interruptible_sleep(0.8)

                self._touch_farm()
                self._farm_last_action = (
                    f"已收获 {len(targets)} 块土地，获得 {total_fruit} 个果实"
                )
                self._farm_last_error = None
                logger.info(
                    "farm_harvest_completed uin=%s lands=%d fruits=%d exp=%d",
                    masked_uin(self.credentials.uin),
                    len(targets),
                    total_fruit,
                    total_experience,
                )
                return self.farm_snapshot()
            except FarmOperationError:
                raise
            except Exception as exc:
                self._raise_farm_failure(
                    "harvest",
                    "FARM_HARVEST_FAILED",
                    "收获失败，请刷新土地状态后重试",
                    exc,
                )

    async def plant_farm(self, ground_id: int, seed_id: int) -> FarmSnapshot:
        async with self._farm_lock:
            try:
                game = self._require_farm_connection()
                if (
                    len(self._farm_lands) != MANOR_LAND_COUNT
                    or not self._farm_seeds
                ):
                    await self._refresh_farm_locked()
                land = self._farm_lands.get(ground_id)
                if land is None:
                    raise FarmOperationError(
                        "FARM_LAND_NOT_FOUND",
                        "指定土地不存在，请刷新后重试",
                    )
                if not land.unlocked:
                    raise FarmOperationError(
                        "FARM_LAND_LOCKED",
                        f"第 {ground_id + 1} 块土地尚未解锁",
                    )
                if not land.empty:
                    raise FarmOperationError(
                        "FARM_LAND_OCCUPIED",
                        f"第 {ground_id + 1} 块土地已有作物",
                    )
                seed = self._farm_seeds.get(seed_id)
                if seed is None or seed.count < 1:
                    raise FarmOperationError(
                        "FARM_SEED_UNAVAILABLE",
                        "所选种子数量不足，请刷新种子背包",
                    )

                response = await game.send_and_wait(
                    build_manor_sow_request(
                        int(self.credentials.uin),
                        seed_id,
                        ground_id,
                    ),
                    MANOR_PLANT_SOW,
                    timeout=5.0,
                )
                result = parse_manor_sow_response(response)
                if result.land.ground_id != ground_id:
                    raise GameProtocolError("manor-sow response returned another land")
                if result.land.seed_id != seed_id:
                    raise GameProtocolError("manor-sow response returned another seed")
                self._farm_lands[ground_id] = result.land
                remaining = seed.count - 1
                if remaining > 0:
                    self._farm_seeds[seed_id] = FarmSeed(seed_id, remaining)
                else:
                    self._farm_seeds.pop(seed_id, None)
                self._touch_farm()
                self._farm_last_action = (
                    f"已在第 {ground_id + 1} 块土地播种，剩余种子 {remaining} 个"
                )
                self._farm_last_error = None
                logger.info(
                    "farm_plant_completed uin=%s ground=%d seed_id=%d remaining=%d",
                    masked_uin(self.credentials.uin),
                    ground_id,
                    seed_id,
                    remaining,
                )
                return self.farm_snapshot()
            except FarmOperationError:
                raise
            except Exception as exc:
                self._raise_farm_failure(
                    "plant",
                    "FARM_PLANT_FAILED",
                    "播种失败，请刷新土地和种子背包后重试",
                    exc,
                )

    async def _refresh_farm_locked(self) -> FarmSnapshot:
        game = self._require_farm_connection()
        uin = int(self.credentials.uin)
        manor_response = await game.send_and_wait(
            build_manor_query_request(uin),
            MANOR_GROUND_INFO,
            timeout=5.0,
        )
        manor = parse_manor_query_response(manor_response)
        seed_response = await game.send_and_wait(
            build_farm_seed_inventory_request(uin),
            FARM_SEED_INVENTORY,
            timeout=5.0,
        )
        seeds = parse_farm_seed_inventory_response(seed_response)
        self._farm_manor_level = manor.manor_level
        self._farm_lands = {land.ground_id: land for land in manor.lands}
        self._farm_seeds = {seed.seed_id: seed for seed in seeds}
        self._touch_farm()
        self._farm_last_action = "土地和种子背包已刷新"
        self._farm_last_error = None
        logger.info(
            "farm_refreshed uin=%s lands=%d seeds=%d manor_level=%d",
            masked_uin(self.credentials.uin),
            len(self._farm_lands),
            len(self._farm_seeds),
            manor.manor_level,
        )
        return self.farm_snapshot()

    def _require_farm_connection(self) -> AsyncPacketConnection:
        game = self._game
        if self.state in {
            SessionState.WAITING_BEFORE_JUMP,
            SessionState.JUMPING_TO_BANK,
            SessionState.ENABLING_TIME_STOP,
            SessionState.RUNNING,
        }:
            raise FarmOperationError(
                "FARM_UNAVAILABLE_DURING_MINIGAME",
                "小游戏挂机进行中，停止挂机后才能操作农场",
            )
        if self._pickup_active or self.state is SessionState.PICKUP_RUNNING:
            raise FarmOperationError(
                "FARM_UNAVAILABLE_DURING_PICKUP",
                "取物进行中，停止取物后才能操作农场",
            )
        if (
            self.state is not SessionState.READY
            or game is None
            or not game.is_open
        ):
            raise FarmOperationError(
                "FARM_SESSION_NOT_READY",
                "游戏尚未连接完成，暂时不能操作农场",
            )
        return game

    def _touch_farm(self) -> None:
        self._farm_updated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")

    def _raise_farm_failure(
        self,
        action: str,
        error_code: str,
        message: str,
        exc: BaseException,
    ) -> None:
        self._farm_last_error = message
        logger.warning(
            "farm_operation_failed uin=%s action=%s error_type=%s",
            masked_uin(self.credentials.uin),
            action,
            type(exc).__name__,
        )
        raise FarmOperationError(error_code, message) from None

    async def refresh_paradise(self) -> ParadiseSnapshot:
        async with self._paradise_lock:
            try:
                return await self._refresh_paradise_locked()
            except ParadiseOperationError:
                raise
            except Exception as exc:
                self._raise_paradise_failure(
                    "refresh",
                    "PARADISE_REFRESH_FAILED",
                    "乐园状态读取失败，请稍后重试",
                    exc,
                )

    async def start_paradise_adventure(self) -> ParadiseSnapshot:
        async with self._paradise_lock:
            try:
                if self._pickup_active:
                    raise ParadiseOperationError(
                        "PARADISE_UNAVAILABLE_DURING_PICKUP",
                        "取物进行中，停止取物后才能操作乐园",
                    )
                self._require_paradise_connection()
                if self._paradise_level is None:
                    await self._refresh_paradise_scene()
                await self._query_paradise_status()
                if not self._paradise_spirit_ids:
                    raise ParadiseOperationError(
                        "PARADISE_NO_SPIRITS",
                        "乐园里还没有精灵，暂时不能开始探险",
                    )
                if self._paradise_countdown != -1:
                    raise ParadiseOperationError(
                        "PARADISE_NOT_IDLE",
                        "乐园当前不是空闲状态，请刷新后重试",
                    )
                if (
                    self._paradise_limit > 0
                    and self._paradise_times >= self._paradise_limit
                ):
                    raise ParadiseOperationError(
                        "PARADISE_LIMIT_REACHED",
                        "今天的乐园探险次数已经用完",
                    )

                root = await self._request_paradise(
                    1,
                    participants=len(self._paradise_spirit_ids),
                )
                countdown = _xml_int(root, "countdown", -1)
                if countdown < 0:
                    raise GameProtocolError(
                        "paradise start response has no countdown"
                    )
                self._paradise_countdown = countdown
                self._paradise_participants = len(self._paradise_spirit_ids)
                self._touch_paradise()
                self._paradise_last_action = (
                    f"已开始探险，{self._paradise_participants} 只精灵参与"
                )
                self._paradise_last_error = None
                logger.info(
                    "paradise_adventure_started uin=%s spirits=%d countdown=%d",
                    masked_uin(self.credentials.uin),
                    self._paradise_participants,
                    countdown,
                )
                return self.paradise_snapshot()
            except ParadiseOperationError:
                raise
            except Exception as exc:
                self._raise_paradise_failure(
                    "start",
                    "PARADISE_START_FAILED",
                    "开始乐园探险失败，请刷新状态后重试",
                    exc,
                )

    async def claim_paradise_rewards(self) -> ParadiseSnapshot:
        async with self._paradise_lock:
            try:
                if self._pickup_active:
                    raise ParadiseOperationError(
                        "PARADISE_UNAVAILABLE_DURING_PICKUP",
                        "取物进行中，停止取物后才能操作乐园",
                    )
                self._require_paradise_connection()
                await self._query_paradise_status()
                if self._paradise_countdown != 0:
                    raise ParadiseOperationError(
                        "PARADISE_REWARD_NOT_READY",
                        "乐园探险尚未完成，当前不能领取奖励",
                    )

                root = await self._request_paradise(2)
                reward = ParadiseReward(
                    experience=_xml_int(root, "exp", 0),
                    reward_type=_xml_int(root, "type", -1),
                    items=tuple(
                        ParadiseRewardItem(
                            item_id=_xml_attribute_int(item, "id", -1),
                            count=_xml_attribute_int(item, "count", 0),
                            item_type=_xml_attribute_int(item, "type", -1),
                        )
                        for item in root.findall("Item")
                        if _xml_attribute_int(item, "id", -1) > 0
                    ),
                )
                self._paradise_last_reward = reward
                try:
                    await self._query_paradise_status()
                except Exception:
                    self._paradise_countdown = -2
                    self._touch_paradise()
                self._paradise_last_action = (
                    f"奖励已领取，经验 +{reward.experience}，"
                    f"道具 {len(reward.items)} 种"
                )
                self._paradise_last_error = None
                logger.info(
                    "paradise_rewards_claimed uin=%s exp=%d items=%d",
                    masked_uin(self.credentials.uin),
                    reward.experience,
                    len(reward.items),
                )
                return self.paradise_snapshot()
            except ParadiseOperationError:
                raise
            except Exception as exc:
                self._raise_paradise_failure(
                    "claim",
                    "PARADISE_CLAIM_FAILED",
                    "领取乐园奖励失败，请刷新状态后重试",
                    exc,
                )

    async def _refresh_paradise_locked(self) -> ParadiseSnapshot:
        self._require_paradise_connection()
        await self._refresh_paradise_scene()
        await self._query_paradise_status()
        self._paradise_last_action = "乐园精灵和探险状态已刷新"
        self._paradise_last_error = None
        logger.info(
            "paradise_refreshed uin=%s level=%d spirits=%d countdown=%d "
            "times=%d limit=%d",
            masked_uin(self.credentials.uin),
            self._paradise_level or 0,
            len(self._paradise_spirit_ids),
            self._paradise_countdown,
            self._paradise_times,
            self._paradise_limit,
        )
        return self.paradise_snapshot()

    async def start_pickup(self, selected_groups: tuple[str, ...]) -> SessionSnapshot:
        async with self._pickup_control_lock:
            if self._minigame_active():
                raise PickupOperationError(
                    "PICKUP_UNAVAILABLE_DURING_MINIGAME",
                    "挂机进行中，停止挂机后才能进行取物",
                )
            game = self._require_pickup_connection()
            if self._pickup_task is not None and not self._pickup_task.done():
                raise PickupOperationError(
                    "PICKUP_ALREADY_RUNNING",
                    "取物已经启动",
                )
            if any(key not in PICKUP_GROUPS_BY_KEY for key in selected_groups):
                raise PickupOperationError(
                    "PICKUP_GROUP_INVALID",
                    "取物分类无效，请刷新页面后重试",
                )
            groups = tuple(
                PICKUP_GROUPS_BY_KEY[key]
                for key in selected_groups
            )
            if not groups:
                raise PickupOperationError(
                    "PICKUP_GROUP_REQUIRED",
                    "请至少选择一个取物分类",
                )
            async with self._farm_lock, self._paradise_lock, self._activity_lock:
                if self._minigame_active():
                    raise PickupOperationError(
                        "PICKUP_UNAVAILABLE_DURING_MINIGAME",
                        "挂机进行中，停止挂机后才能进行取物",
                    )
                self._pickup_active = True
                self.state = SessionState.PICKUP_RUNNING
                self._pickup_selected_groups = tuple(group.key for group in groups)
                self._pickup_completed = 0
                self._pickup_total = sum(len(group.items) for group in groups)
                self._pickup_failed = 0
                self._pickup_current_group = None
                self._pickup_current_item = None
                self._pickup_started_at = datetime.now(SHANGHAI)
                self._pickup_last_action = "正在取物"
                self._pickup_last_error = None
                self._pickup_responses = []
                self._pickup_stop_event.clear()
                self._pickup_task = asyncio.create_task(self._pickup_loop(groups))
            return self.snapshot()

    async def stop_pickup(self) -> SessionSnapshot:
        async with self._pickup_control_lock:
            task = self._pickup_task
            if not self._pickup_active or task is None or task.done():
                raise PickupOperationError("PICKUP_NOT_RUNNING", "取物尚未启动")
            self._pickup_stop_event.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._pickup_task = None
            self._pickup_active = False
            if self.state is SessionState.PICKUP_RUNNING and self._game_is_open():
                self.state = SessionState.READY
            self._pickup_current_group = None
            self._pickup_current_item = None
            self._pickup_last_action = "取物已停止"
            return self.snapshot()

    async def _pickup_loop(self, groups: tuple[PickupGroup, ...]) -> None:
        try:
            for group in groups:
                self._pickup_current_group = group.name
                for item in group.items:
                    if self._pickup_stop_event.is_set():
                        return
                    self._pickup_current_item = item.item_id
                    if self.scene_id != item.scene_id:
                        await self._pickup_jump_to(item.scene_id)
                    await self._pickup_sleep(1.0)
                    result, message = await self._submit_pickup_item(item)
                    received = result == 0
                    self._pickup_responses.append(
                        PickupResponse(
                            group=group.name,
                            item_id=item.item_id,
                            result=result,
                            message=message,
                        )
                    )
                    self._pickup_completed += 1
                    if received:
                        self._pickup_last_action = (
                            f"{group.name} · 道具 {item.item_id}领取成功"
                        )
                    else:
                        self._pickup_failed += 1
                        self._pickup_last_error = (
                            f"{group.name} · 道具 {item.item_id}："
                            f"{message or 'msg 为空'}"
                        )
                        logger.info(
                            "pickup_item_unavailable uin=%s item_id=%d "
                            "completed=%d total=%d",
                            masked_uin(self.credentials.uin),
                            item.item_id,
                            self._pickup_completed,
                            self._pickup_total,
                        )
                    await self._pickup_sleep(0.8)
            self._pickup_last_action = (
                "取物完成"
                if self._pickup_failed == 0
                else f"取物完成，{self._pickup_failed} 个道具未领取"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = _safe_response_message(str(exc) or "未知错误")
            self._pickup_last_error = (
                f"{self._pickup_current_group or '取物'}"
                f" · 道具 {self._pickup_current_item or '—'}：{detail}"
            )
            self._pickup_last_action = (
                f"取物中断，已处理 {self._pickup_completed}/{self._pickup_total}"
            )
            logger.warning(
                "pickup_failed uin=%s error_type=%s",
                masked_uin(self.credentials.uin),
                type(exc).__name__,
            )
        finally:
            self._pickup_active = False
            self._pickup_task = None
            self._pickup_current_group = None
            self._pickup_current_item = None
            if self.state is SessionState.PICKUP_RUNNING and self._game_is_open():
                self.state = SessionState.READY

    async def _pickup_jump_to(self, target_scene: int) -> None:
        game = self._require_pickup_connection()
        response = await game.send_and_wait(
            build_scene_jump_request(
                int(self.credentials.uin),
                self.scene_id or 0,
                target_scene,
                version=0,
            ),
            SCENE_JUMP,
            timeout=5.0,
        )
        scene, _version = parse_scene_jump_response(response)
        if scene != target_scene:
            raise GameProtocolError("server did not enter pickup scene")
        self.scene_id = scene

    async def _submit_pickup_item(self, item: PickupItem) -> tuple[int, str]:
        if self._http is None:
            raise ConnectionError("pickup HTTP client is unavailable")
        endpoint = "scene_game_award" if item.condition != -1 else "hurdle_game_award"
        params: dict[str, str | int] = {
            "id": item.item_id,
            "type": item.item_type,
            "angel_uin": self.credentials.uin,
            "angel_key": self.credentials.angel_key,
        }
        if item.condition != -1:
            params["condition"] = item.condition
        else:
            params["score"] = item.score
        response = await self._http.get(
            f"https://17roco.qq.com/cgi-bin/{endpoint}",
            params=params,
        )
        if response.status_code != 200:
            raise GameAutomationError("pickup request failed")
        root = _parse_xml(response.content)
        payload = root.find("xml")
        if payload is None:
            payload = root
        result = _xml_int(payload, "result", -1)
        return result, _safe_response_message(_xml_text(payload, "msg"))

    async def _pickup_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._pickup_stop_event.wait(), timeout=seconds)
            raise asyncio.CancelledError
        except TimeoutError:
            return

    def _require_pickup_connection(self) -> AsyncPacketConnection:
        game = self._game
        if game is None or not game.is_open or self.state not in {
            SessionState.READY,
            SessionState.PICKUP_RUNNING,
        }:
            raise PickupOperationError(
                "PICKUP_SESSION_NOT_READY",
                "游戏尚未连接完成，暂时不能进行取物",
            )
        return game

    def _game_is_open(self) -> bool:
        return self._game is not None and self._game.is_open

    def _minigame_active(self) -> bool:
        task = self._minigame_task
        return self.state in {
            SessionState.WAITING_BEFORE_JUMP,
            SessionState.JUMPING_TO_BANK,
            SessionState.ENABLING_TIME_STOP,
            SessionState.RUNNING,
        } or (task is not None and not task.done())

    async def _refresh_paradise_scene(self) -> None:
        game, _http = self._require_paradise_connection()
        uin = int(self.credentials.uin)
        response = await game.send_and_wait(
            build_paradise_spirit_list_request(uin),
            PARADISE_SPIRIT_LIST,
            timeout=5.0,
        )
        scene = parse_paradise_spirit_list_response(response)
        self._paradise_level = scene.level
        self._paradise_experience = scene.experience
        self._paradise_spirit_ids = scene.spirit_ids
        self._touch_paradise()

    async def _query_paradise_status(self) -> None:
        root = await self._request_paradise(0)
        self._paradise_countdown = _xml_int(root, "countdown", -1)
        self._paradise_times = _xml_int(root, "times", -1)
        self._paradise_limit = _xml_int(root, "limit", -1)
        self._paradise_participants = _xml_int(root, "num", -1)
        self._touch_paradise()

    async def _request_paradise(
        self,
        command: int,
        *,
        participants: int | None = None,
    ) -> ET.Element:
        _game, client = self._require_paradise_connection()
        params: dict[str, str | int] = {
            "cmd": command,
            "unkown": self.credentials.pskey,
            "skey": self.credentials.skey,
            "platfrom": 2,
            "angel_uin": self.credentials.uin,
            "angel_key": self.credentials.angel_key,
            "time": int(time.time() * 1000),
        }
        if participants is not None:
            params["num"] = participants
        try:
            response = await client.get(PARADISE_URL, params=params)
        except httpx.HTTPError as exc:
            raise GameAutomationError("paradise HTTP request failed") from exc
        if response.status_code != 200:
            raise GameAutomationError(
                f"paradise HTTP request failed ({response.status_code})"
            )
        try:
            root = _parse_xml(response.content)
        except ET.ParseError as exc:
            raise GameAutomationError(
                "paradise HTTP response was not valid XML"
            ) from exc
        result = root.find("result")
        if result is None:
            raise GameProtocolError("paradise response has no result")
        try:
            result_code = int(result.attrib.get("value", "-1"))
        except ValueError as exc:
            raise GameProtocolError("paradise result is invalid") from exc
        if result_code != 0:
            message = _safe_response_message(
                result.findtext("msg") or "乐园请求被游戏服务器拒绝"
            )
            raise GameProtocolError(message)
        return root

    def _require_paradise_connection(
        self,
    ) -> tuple[AsyncPacketConnection, httpx.AsyncClient]:
        game = self._game
        client = self._http
        if (
            self.state not in {
                SessionState.READY,
                SessionState.RUNNING,
                SessionState.PICKUP_RUNNING,
            }
            or game is None
            or not game.is_open
            or client is None
        ):
            raise ParadiseOperationError(
                "PARADISE_SESSION_NOT_READY",
                "游戏尚未连接完成，暂时不能操作乐园",
            )
        if self._pickup_active:
            raise ParadiseOperationError(
                "PARADISE_UNAVAILABLE_DURING_PICKUP",
                "取物进行中，停止取物后才能操作乐园",
            )
        return game, client

    def _touch_paradise(self) -> None:
        self._paradise_updated_at = datetime.now(SHANGHAI).isoformat(
            timespec="seconds"
        )

    def _raise_paradise_failure(
        self,
        action: str,
        error_code: str,
        message: str,
        exc: BaseException,
    ) -> None:
        self._paradise_last_error = message
        logger.warning(
            "paradise_operation_failed uin=%s action=%s error_type=%s",
            masked_uin(self.credentials.uin),
            action,
            type(exc).__name__,
        )
        raise ParadiseOperationError(error_code, message) from None

    async def start_minigame(self) -> SessionSnapshot:
        async with self._minigame_control_lock:
            game = self._require_minigame_connection()
            task = self._minigame_task
            if task is not None and not task.done():
                raise MinigameOperationError(
                    "MINIGAME_ALREADY_RUNNING",
                    "小游戏挂机已经启动",
                )
            if self._pickup_active or self.state is SessionState.PICKUP_RUNNING:
                raise MinigameOperationError(
                    "MINIGAME_UNAVAILABLE_DURING_PICKUP",
                    "取物进行中，停止取物后才能开始挂机",
                )
            if self.state is not SessionState.READY:
                raise MinigameOperationError(
                    "MINIGAME_SESSION_NOT_READY",
                    "游戏尚未完成进房，暂时不能开始挂机",
                )

            self._minigame_task = None
            self._minigame_stop_event.clear()
            self.last_error_code = None
            self.last_error = None
            try:
                # Serialize the transition out of READY with farm operations.
                # An operation already in progress finishes first; all later
                # farm requests observe the minigame state and are rejected.
                async with self._farm_lock, self._paradise_lock, self._activity_lock:
                    self.state = SessionState.WAITING_BEFORE_JUMP
                await self._interruptible_sleep(5.0)
                uin = int(self.credentials.uin)
                if self.scene_id != BANK_SCENE_ID:
                    self.state = SessionState.JUMPING_TO_BANK
                    jump_response = await game.send_and_wait(
                        build_scene_jump_request(
                            uin,
                            self.scene_id or 0,
                            BANK_SCENE_ID,
                            version=0,
                        ),
                        SCENE_JUMP,
                        timeout=5.0,
                    )
                    new_scene, _version = parse_scene_jump_response(jump_response)
                    if new_scene != BANK_SCENE_ID:
                        raise GameAutomationError(
                            "server did not enter Roco Bank"
                        )
                    self.scene_id = new_scene

                self.state = SessionState.ENABLING_TIME_STOP
                baseline = await self._enable_time_stop()
            except asyncio.CancelledError:
                if game.is_open:
                    self.state = SessionState.READY
                raise
            except MinigameOperationError:
                raise
            except Exception as exc:
                if self.time_stop_active:
                    try:
                        await self._disable_time_stop()
                    except Exception:
                        pass
                if game.is_open:
                    self.state = SessionState.READY
                logger.warning(
                    "minigame_start_failed uin=%s error_type=%s",
                    masked_uin(self.credentials.uin),
                    type(exc).__name__,
                )
                raise MinigameOperationError(
                    "MINIGAME_START_FAILED",
                    "小游戏挂机启动失败，请稍后重试",
                ) from None

            self.minigame_started_at = datetime.now(SHANGHAI)
            self.state = SessionState.RUNNING
            self._minigame_task = asyncio.create_task(
                self._minigame_loop(baseline)
            )
            logger.info(
                "minigame_started uin=%s room_id=%d scene_id=%d",
                masked_uin(self.credentials.uin),
                self.room.room_index if self.room is not None else -1,
                self.scene_id if self.scene_id is not None else -1,
            )
            return self.snapshot()

    async def stop_minigame(self) -> SessionSnapshot:
        async with self._minigame_control_lock:
            game = self._require_minigame_connection()
            task = self._minigame_task
            if task is None or task.done():
                self._minigame_task = None
                if self.state is not SessionState.READY:
                    self.state = SessionState.READY
                raise MinigameOperationError(
                    "MINIGAME_NOT_RUNNING",
                    "小游戏挂机尚未启动",
                )

            # Match the reference client: mark processing as stopped, wait for
            # the current round to leave its critical section, exit time-stop,
            # then cancel the loop so no later minigame request can be sent.
            self._minigame_stop_event.set()
            disable_error: Exception | None = None
            async with self._minigame_round_lock:
                if self.time_stop_active:
                    try:
                        await self._disable_time_stop()
                    except Exception as exc:
                        disable_error = exc

            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._minigame_task = None
            if game.is_open:
                self.state = SessionState.READY
            logger.info(
                "minigame_stopped uin=%s rounds=%d money=%d exp=%d",
                masked_uin(self.credentials.uin),
                self.rounds,
                self.total_money,
                self.total_exp,
            )
            if disable_error is not None:
                self.last_error = "退出时间停止失败，小游戏请求已经停止"
                raise MinigameOperationError(
                    "MINIGAME_TIME_STOP_EXIT_FAILED",
                    self.last_error,
                ) from None
            self.last_error_code = None
            self.last_error = None
            return self.snapshot()

    async def stop_minigame_and_disconnect(self) -> SessionSnapshot:
        """Stop the minigame first, then close sockets and background tasks."""

        if self._minigame_active():
            try:
                await self.stop_minigame()
            except MinigameOperationError as exc:
                # The disconnect must still release the socket and session task
                # if the minigame stopped concurrently or time-stop exit failed.
                logger.warning(
                    "minigame_stop_before_disconnect_failed uin=%s error_code=%s",
                    masked_uin(self.credentials.uin),
                    exc.error_code,
                )
        await self.close()
        logger.info(
            "game_session_disconnected_by_user uin=%s session_id=%s",
            masked_uin(self.credentials.uin),
            self.session_id,
        )
        return self.snapshot()

    async def refresh_online_time(self) -> SessionSnapshot:
        await self._request_online_time()
        return self.snapshot()

    def _require_minigame_connection(self) -> AsyncPacketConnection:
        game = self._game
        if game is None or not game.is_open:
            raise MinigameOperationError(
                "MINIGAME_SESSION_NOT_READY",
                "游戏尚未连接完成，暂时不能操作挂机",
            )
        return game

    async def close(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._cleanup()

    async def wait_finished(self) -> None:
        await self._finished_event.wait()

    async def _runner(self) -> None:
        masked = masked_uin(self.credentials.uin)
        logger.info(
            "game_session_started uin=%s login_at=%s started_at=%s",
            masked,
            self.login_at.isoformat(timespec="seconds"),
            self.started_at.isoformat(timespec="seconds"),
        )
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed_state = self.state.value
            self.state = SessionState.ERROR
            error = str(exc) or type(exc).__name__
            if error == AUTHORIZATION_EXPIRED_CODE:
                self.last_error_code = AUTHORIZATION_EXPIRED_CODE
                self.last_error = AUTHORIZATION_EXPIRED_MESSAGE
            else:
                self.last_error_code = None
                self.last_error = error
            logger.error(
                "game_session_failed uin=%s stage=%s error_type=%s "
                "error_code=%s rounds=%d",
                masked,
                failed_state,
                type(exc).__name__,
                self.last_error_code or "GAME_SESSION_ERROR",
                self.rounds,
            )
        finally:
            await self._cleanup()
            elapsed_seconds = max(
                0,
                int((datetime.now(SHANGHAI) - self.started_at).total_seconds()),
            )
            logger.info(
                "game_session_finished uin=%s state=%s elapsed_seconds=%d "
                "rounds=%d money=%d exp=%d",
                masked,
                self.state.value,
                elapsed_seconds,
                self.rounds,
                self.total_money,
                self.total_exp,
            )
            self._finished_event.set()
            if self.on_finished is not None:
                await self.on_finished(self.credentials.uin, self)

    async def _run(self) -> None:
        if not self._window_open():
            raise GameAutomationError("SERVICE_CLOSED")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=10.0),
            headers={
                "User-Agent": GAME_HTTP_USER_AGENT,
                "Sec-Ch-Ua-Platform": "Windows",
                "Cookie": self.credentials.cookie_header,
            },
        )
        self.state = SessionState.CONNECTING_DIRECTORY
        self._directory = await AsyncPacketConnection.connect(
            self.directory_host, self.directory_port
        )
        init_future = self._directory.expect(DIR_INIT)
        await self._directory.send(
            b"tgw_l7_forward\r\nHost: dir.17roco.qq.com:443\r\n\r\n"
        )
        directory_init = await asyncio.wait_for(init_future, timeout=8.0)
        init_result = require_success(directory_init)
        if init_result.message.lower() != "ok":
            raise GameAutomationError("directory server initialization failed")

        self.state = SessionState.SELECTING_ROOM
        uin = int(self.credentials.uin)
        rooms_packet = await self._directory.send_and_wait(
            build_recommend_room_request(uin, self.credentials.angel_key),
            RECOMMEND_ROOM_REPLY,
            timeout=8.0,
        )
        self.room = choose_least_populated_room(
            parse_recommended_rooms(rooms_packet)
        )
        await self._directory.close()
        self._directory = None

        self.state = SessionState.CONNECTING_GAME
        zone_host = f"zone{self.room.zone_id}.17roco.qq.com"
        self._game = await AsyncPacketConnection.connect(
            zone_host,
            self.room.port,
            fallback_host=self.room.room_ip,
        )
        room_init_future = self._game.expect(ROOM_INIT_COMPLETE)
        await self._game.send(
            (
                "tgw_l7_forward\r\n"
                f"Host: zone{self.room.zone_id}.17roco.qq.com:443\r\n\r\n"
            ).encode("ascii")
        )
        room_init = await asyncio.wait_for(room_init_future, timeout=8.0)
        room_init_result = require_success(room_init)
        if room_init_result.message.lower() != "ok":
            raise GameAutomationError("game server initialization failed")

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.state = SessionState.ENTERING_ROOM
        enter_response = await self._game.send_and_wait(
            build_enter_room_request(
                uin, self.room.room_index, self.credentials.angel_key
            ),
            ENTER_ROOM,
            timeout=10.0,
        )
        _room_id, self.scene_id, _scene_version = parse_enter_room_response(
            enter_response
        )

        self.state = SessionState.READY
        logger.info(
            "game_session_ready uin=%s room_id=%d scene_id=%d",
            masked_uin(self.credentials.uin),
            self.room.room_index if self.room is not None else -1,
            self.scene_id if self.scene_id is not None else -1,
        )
        try:
            await self.refresh_farm()
        except FarmOperationError:
            # Farm availability must not prevent the connected game session.
            pass
        try:
            await self.refresh_paradise()
        except ParadiseOperationError:
            # Paradise availability must not prevent the connected game session.
            pass
        await self._request_online_time()
        await self._session_loop()

    async def _session_loop(self) -> None:
        next_authorization_check = time.monotonic()
        next_key_refresh = time.monotonic() + 20 * 60.0
        next_online_time_refresh = (
            time.monotonic() + ONLINE_TIME_REFRESH_INTERVAL_SECONDS
        )

        while not self._stop_event.is_set():
            now = time.monotonic()
            if not self._window_open():
                raise GameAutomationError("SERVICE_CLOSED")

            minigame = self._minigame_task
            if minigame is not None and minigame.done():
                if minigame.cancelled():
                    pass
                else:
                    failure = minigame.exception()
                    if failure is not None:
                        raise failure

            if now >= next_authorization_check:
                current = await asyncio.to_thread(
                    self._current_grant,
                    self.credentials.uin,
                )
                if current is None:
                    logger.warning(
                        "authorization_expired uin=%s",
                        masked_uin(self.credentials.uin),
                    )
                    raise GameAutomationError(AUTHORIZATION_EXPIRED_CODE)
                self.access_grant = current
                next_authorization_check = (
                    now + AUTHORIZATION_CHECK_INTERVAL_SECONDS
                )
            if now >= next_key_refresh:
                await self._refresh_angel_key()
                next_key_refresh = now + 20 * 60.0
            if (
                now >= next_online_time_refresh
                and self.state is SessionState.READY
            ):
                await self._request_online_time()
                next_online_time_refresh = (
                    now + ONLINE_TIME_REFRESH_INTERVAL_SECONDS
                )

            await self._interruptible_sleep(1.0)

    async def _minigame_loop(self, online_time_baseline: int) -> None:
        if self._game is None:
            raise ConnectionError("game connection is unavailable")
        uin = int(self.credentials.uin)
        next_time_stop_check = time.monotonic() + 60.0
        consecutive_reward_errors = 0

        while (
            not self._stop_event.is_set()
            and not self._minigame_stop_event.is_set()
        ):
            async with self._minigame_round_lock:
                if (
                    self._stop_event.is_set()
                    or self._minigame_stop_event.is_set()
                ):
                    break
                await self._game.send(
                    build_minigame_start_request(uin, GAME_ID)
                )
                if not await self._wait_minigame_interval(
                    GAME_INTERVAL_SECONDS
                ):
                    break

                if time.monotonic() >= next_time_stop_check:
                    current_online_time = await self._request_online_time()
                    if (
                        online_time_baseline >= 0
                        and current_online_time >= 0
                        and current_online_time != online_time_baseline
                    ):
                        online_time_baseline = await self._enable_time_stop()
                    elif current_online_time >= 0:
                        online_time_baseline = current_online_time
                        self.time_stop_active = True
                    else:
                        online_time_baseline = await self._enable_time_stop()
                    next_time_stop_check = time.monotonic() + 60.0

                try:
                    money, experience = await self._claim_reward()
                except Exception as exc:
                    consecutive_reward_errors += 1
                    self.last_error_code = None
                    self.last_error = str(exc) or type(exc).__name__
                    logger.warning(
                        "reward_request_failed uin=%s consecutive_errors=%d "
                        "error_type=%s",
                        masked_uin(self.credentials.uin),
                        consecutive_reward_errors,
                        type(exc).__name__,
                    )
                    if consecutive_reward_errors >= 3:
                        raise GameAutomationError(
                            "minigame reward failed three times"
                        ) from exc
                else:
                    consecutive_reward_errors = 0
                    self.last_error_code = None
                    self.last_error = None
                    self.rounds += 1
                    self.total_money += money
                    self.total_exp += experience

    async def _wait_minigame_interval(self, seconds: float) -> bool:
        session_stop = asyncio.create_task(self._stop_event.wait())
        minigame_stop = asyncio.create_task(self._minigame_stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {session_stop, minigame_stop},
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return not done
        finally:
            for task in (session_stop, minigame_stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                session_stop,
                minigame_stop,
                return_exceptions=True,
            )

    async def _enable_time_stop(self) -> int:
        if self._game is None:
            raise ConnectionError("game connection is unavailable")
        uin = int(self.credentials.uin)
        last_failure: Exception | None = None
        for _ in range(3):
            try:
                response = await self._game.send_and_wait(
                    build_time_pause_request(uin, True),
                    TIME_PAUSE,
                    timeout=3.0,
                )
                parse_time_pause_response(response)
                self.time_stop_active = True
                return await self._request_online_time()
            except Exception as exc:
                last_failure = exc
                self.time_stop_active = False
        raise GameAutomationError("could not enable time-stop mode") from last_failure

    async def _disable_time_stop(self) -> None:
        if self._game is None:
            raise ConnectionError("game connection is unavailable")
        try:
            response = await self._game.send_and_wait(
                build_time_pause_request(
                    int(self.credentials.uin),
                    False,
                ),
                TIME_PAUSE,
                timeout=3.0,
            )
            parse_time_pause_response(response)
        finally:
            self.time_stop_active = False

    async def _request_online_time(self) -> int:
        async with self._online_time_lock:
            if self._game is None:
                return -1
            try:
                response = await self._game.send_and_wait(
                    build_online_time_request(int(self.credentials.uin)),
                    ONLINE_TIME,
                    timeout=2.0,
                )
                online_time = parse_online_time_response(response)
                self.online_time_seconds = online_time
                return online_time
            except Exception:
                return -1

    async def _claim_reward(self) -> tuple[int, int]:
        if self._http is None:
            raise ConnectionError("reward HTTP client is unavailable")
        masked = masked_uin(self.credentials.uin)
        started = time.monotonic()
        try:
            response = await self._http.get(
                "https://17roco.qq.com/cgi-bin/hurdle_game_award",
                params={
                    "id": str(GAME_ID),
                    "score": str(GAME_SCORE),
                    "type": str(GAME_TYPE),
                    "angel_uin": self.credentials.uin,
                    "angel_key": self.credentials.angel_key,
                },
            )
        except httpx.HTTPError as exc:
            # Do not retain an exception containing the credential-bearing URL.
            logger.warning(
                "reward_claim_failed uin=%s failure_type=request_error "
                "error_type=%s elapsed_ms=%d",
                masked,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("reward request failed") from None
        if response.status_code != 200:
            content_type, response_bytes, body_sha256 = _response_metadata(response)
            logger.warning(
                "reward_claim_failed uin=%s failure_type=http_status "
                "http_status=%d content_type=%r response_bytes=%d "
                "body_sha256=%s elapsed_ms=%d",
                masked,
                response.status_code,
                content_type,
                response_bytes,
                body_sha256,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError(
                f"reward server returned HTTP {response.status_code}"
            )
        try:
            root = _parse_xml(response.content)
        except GameAutomationError:
            content_type, response_bytes, body_sha256 = _response_metadata(response)
            logger.warning(
                "reward_claim_failed uin=%s failure_type=invalid_xml "
                "http_status=%d content_type=%r response_bytes=%d "
                "body_sha256=%s elapsed_ms=%d",
                masked,
                response.status_code,
                content_type,
                response_bytes,
                body_sha256,
                int((time.monotonic() - started) * 1000),
            )
            raise
        raw_result = _xml_text(root, "result")
        try:
            result = int(raw_result)
        except ValueError:
            content_type, response_bytes, body_sha256 = _response_metadata(response)
            logger.warning(
                "reward_claim_failed uin=%s failure_type=invalid_payload "
                "field=result http_status=%d content_type=%r response_bytes=%d "
                "body_sha256=%s elapsed_ms=%d",
                masked,
                response.status_code,
                content_type,
                response_bytes,
                body_sha256,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("reward server returned an invalid result") from None
        if result != 0:
            message = _safe_response_message(
                _xml_text(root, "msg") or "reward server rejected the round"
            )
            logger.warning(
                "reward_claim_failed uin=%s failure_type=business_rejection "
                "result=%d message=%r elapsed_ms=%d",
                masked,
                result,
                message,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError(message)
        return _xml_int(root, "money", 0), _xml_int(root, "exp", 0)

    async def _refresh_angel_key(self) -> None:
        last_error: GameAutomationError | None = None
        for attempt in range(1, ANGEL_KEY_REFRESH_ATTEMPTS + 1):
            try:
                await self._refresh_angel_key_once(attempt)
                return
            except GameAutomationError as exc:
                last_error = exc
                if attempt >= ANGEL_KEY_REFRESH_ATTEMPTS:
                    break
                logger.warning(
                    "angel_key_refresh_retry_scheduled uin=%s attempt=%d "
                    "max_attempts=%d retry_in_seconds=%.1f",
                    masked_uin(self.credentials.uin),
                    attempt,
                    ANGEL_KEY_REFRESH_ATTEMPTS,
                    ANGEL_KEY_REFRESH_RETRY_SECONDS,
                )
                await self._interruptible_sleep(ANGEL_KEY_REFRESH_RETRY_SECONDS)
        logger.error(
            "angel_key_refresh_exhausted uin=%s attempts=%d",
            masked_uin(self.credentials.uin),
            ANGEL_KEY_REFRESH_ATTEMPTS,
        )
        raise GameAutomationError(
            "angel_key refresh failed after retries"
        ) from last_error

    async def _refresh_angel_key_once(self, attempt: int) -> None:
        if self._http is None:
            raise ConnectionError("key refresh HTTP client is unavailable")
        encoded_pskey = "".join(
            f"%{byte:02X}" for byte in self.credentials.pskey.encode("utf-8")
        )
        url = (
            f"{FCGI_BASE_URL}sign2?"
            f"angel_uin={self.credentials.uin}&"
            f"angel_key={self.credentials.angel_key}&"
            f"unkown={encoded_pskey}"
        )
        masked = masked_uin(self.credentials.uin)
        started = time.monotonic()
        logger.info(
            "angel_key_refresh_started uin=%s attempt=%d max_attempts=%d",
            masked,
            attempt,
            ANGEL_KEY_REFRESH_ATTEMPTS,
        )
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            # The request URL includes angel_key and pskey; keep it out of state/logs.
            logger.warning(
                "angel_key_refresh_failed uin=%s failure_type=request_error "
                "error_type=%s attempt=%d elapsed_ms=%d",
                masked,
                type(exc).__name__,
                attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("angel_key refresh failed") from None
        if response.status_code != 200:
            logger.warning(
                "angel_key_refresh_failed uin=%s failure_type=http_status "
                "http_status=%d attempt=%d elapsed_ms=%d",
                masked,
                response.status_code,
                attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("angel_key refresh failed")
        try:
            root = _parse_xml(response.content)
        except ET.ParseError:
            logger.warning(
                "angel_key_refresh_failed uin=%s failure_type=invalid_xml "
                "attempt=%d elapsed_ms=%d",
                masked,
                attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("angel_key refresh returned invalid XML") from None
        result = _xml_int(root, "result", -1)
        if result != 0:
            logger.warning(
                "angel_key_refresh_failed uin=%s failure_type=rejected "
                "result=%d attempt=%d elapsed_ms=%d",
                masked,
                result,
                attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("angel_key refresh was rejected")
        new_key = _xml_text(root, "angel_key")
        if not new_key:
            logger.warning(
                "angel_key_refresh_failed uin=%s failure_type=empty_key "
                "attempt=%d elapsed_ms=%d",
                masked,
                attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise GameAutomationError("angel_key refresh returned an empty key")
        self.credentials.angel_key = new_key
        logger.info(
            "angel_key_refresh_succeeded uin=%s attempt=%d elapsed_ms=%d",
            masked,
            attempt,
            int((time.monotonic() - started) * 1000),
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._interruptible_sleep(180.0)
                game = self._game
                if game is not None and time.monotonic() - game.last_receive_at > 120.0:
                    await game.send(build_heartbeat(int(self.credentials.uin)))
        except asyncio.CancelledError:
            raise

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            raise asyncio.CancelledError
        except TimeoutError:
            return

    async def _cleanup(self) -> None:
        async with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
            self._minigame_stop_event.set()
            self._pickup_stop_event.set()
            pickup = self._pickup_task
            if (
                pickup is not None
                and pickup is not asyncio.current_task()
                and not pickup.done()
            ):
                pickup.cancel()
                await asyncio.gather(pickup, return_exceptions=True)
            self._pickup_task = None
            self._pickup_active = False
            minigame = self._minigame_task
            if (
                minigame is not None
                and minigame is not asyncio.current_task()
                and not minigame.done()
            ):
                minigame.cancel()
                await asyncio.gather(minigame, return_exceptions=True)
            self._minigame_task = None
            heartbeat = self._heartbeat_task
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            game = self._game
            if game is not None:
                if self.time_stop_active:
                    try:
                        await game.send(
                            build_time_pause_request(
                                int(self.credentials.uin), False
                            )
                        )
                    except Exception:
                        pass
                await game.close()
                self._game = None
            if self._directory is not None:
                await self._directory.close()
                self._directory = None
            if self._http is not None:
                await self._http.aclose()
                self._http = None
            self.time_stop_active = False
            if self.state is not SessionState.ERROR:
                self.state = SessionState.CLOSED


@dataclass(frozen=True, slots=True)
class SessionStartResult:
    session: GameSession
    resumed: bool


class GameSessionRegistry:
    def __init__(
        self,
        access_provider: AccessProvider,
        *,
        now: Callable[[], datetime] | None = None,
        max_sessions: int = 50,
        current_grant: AccessGrantProvider | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.access_provider = access_provider
        self._now = now or (lambda: datetime.now(SHANGHAI))
        self._current_grant = (
            current_grant or access_provider.current_grant
        )
        self._max_sessions = max_sessions
        self._sessions: dict[str, GameSession] = {}
        self._sessions_by_id: dict[str, GameSession] = {}
        self._finished: dict[str, SessionSnapshot] = {}
        self._valid_until: dict[str, datetime] = {}
        self._latest_by_uin: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        credentials: LoginCredentials,
        access_grant: AccessGrant,
    ) -> SessionStartResult:
        now = self._now().astimezone(SHANGHAI)
        valid_until = session_valid_until(now)
        if now >= valid_until:
            raise GameAutomationError("SERVICE_CLOSED")
        session = GameSession(
            self.access_provider,
            credentials,
            access_grant,
            session_id=secrets.token_urlsafe(32),
            valid_until=valid_until,
            started_at=now,
            on_finished=self._remove_finished,
            current_grant=self._current_grant,
        )
        async with self._lock:
            old_session = self._sessions.get(credentials.uin)
            if old_session is not None and old_session.is_active:
                old_session.access_grant = access_grant
                old_session.login_at = credentials.login_at.astimezone(SHANGHAI)
                return SessionStartResult(session=old_session, resumed=True)
            if self._active_session_count() >= self._max_sessions:
                raise GameCapacityReached()
            self._sessions[credentials.uin] = session
            self._sessions_by_id[session.session_id] = session
            self._valid_until[session.session_id] = valid_until
            self._latest_by_uin[credentials.uin] = session.session_id
            # Start while holding the registry lock so a concurrent scan for the
            # same UIN observes this session as active and resumes it.
            session.start()
        if old_session is not None:
            await old_session.close()
        return SessionStartResult(session=session, resumed=False)

    async def capacity(self) -> GameCapacity:
        async with self._lock:
            return GameCapacity(
                current_connections=self._active_session_count(),
                max_connections=self._max_sessions,
            )

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise GameSessionNotFound()

        expired_session: GameSession | None = None
        expired = False
        async with self._lock:
            valid_until = self._valid_until.get(session_id)
            if valid_until is None:
                raise GameSessionNotFound()
            if self._now().astimezone(SHANGHAI) >= valid_until:
                expired = True
                expired_session = self._sessions_by_id.pop(session_id, None)
                expired_snapshot = self._finished.pop(session_id, None)
                self._valid_until.pop(session_id, None)
                if (
                    expired_session is not None
                    and self._sessions.get(expired_session.credentials.uin)
                    is expired_session
                ):
                    del self._sessions[expired_session.credentials.uin]
                expired_uin = (
                    expired_session.credentials.uin
                    if expired_session is not None
                    else (
                        expired_snapshot.uin
                        if expired_snapshot is not None
                        else None
                    )
                )
                if (
                    expired_uin is not None
                    and self._latest_by_uin.get(expired_uin) == session_id
                ):
                    del self._latest_by_uin[expired_uin]
            else:
                active = self._sessions_by_id.get(session_id)
                if active is not None:
                    return active.snapshot()
                finished = self._finished.get(session_id)
                if finished is not None:
                    return finished
                raise GameSessionNotFound()

        if expired_session is not None:
            await expired_session.close()
        if expired:
            raise GameSessionExpired()
        raise GameSessionNotFound()

    async def snapshot_for_uin(self, uin: str) -> SessionSnapshot:
        async with self._lock:
            session_id = self._latest_by_uin.get(uin)
        if session_id is None:
            raise GameSessionNotFound()
        return await self.snapshot(session_id)

    async def refresh_online_time(self, session_id: str) -> SessionSnapshot:
        snapshot = await self.snapshot(session_id)
        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            return snapshot
        return await session.refresh_online_time()

    async def refresh_online_time_for_uin(self, uin: str) -> SessionSnapshot:
        async with self._lock:
            session_id = self._latest_by_uin.get(uin)
        if session_id is None:
            raise GameSessionNotFound()
        return await self.refresh_online_time(session_id)

    async def refresh_farm(self, session_id: str) -> FarmSnapshot:
        session = await self._farm_session(session_id)
        return await session.refresh_farm()

    async def harvest_farm(
        self,
        session_id: str,
        ground_id: int | None = None,
    ) -> FarmSnapshot:
        session = await self._farm_session(session_id)
        return await session.harvest_farm(ground_id)

    async def plant_farm(
        self,
        session_id: str,
        ground_id: int,
        seed_id: int,
    ) -> FarmSnapshot:
        session = await self._farm_session(session_id)
        return await session.plant_farm(ground_id, seed_id)

    async def refresh_paradise(self, session_id: str) -> ParadiseSnapshot:
        session = await self._paradise_session(session_id)
        return await session.refresh_paradise()

    async def start_paradise_adventure(
        self,
        session_id: str,
    ) -> ParadiseSnapshot:
        session = await self._paradise_session(session_id)
        return await session.start_paradise_adventure()

    async def claim_paradise_rewards(
        self,
        session_id: str,
    ) -> ParadiseSnapshot:
        session = await self._paradise_session(session_id)
        return await session.claim_paradise_rewards()

    async def start_pickup(
        self,
        session_id: str,
        selected_groups: tuple[str, ...],
    ) -> SessionSnapshot:
        session = await self._pickup_session(session_id)
        return await session.start_pickup(selected_groups)

    async def stop_pickup(self, session_id: str) -> SessionSnapshot:
        session = await self._pickup_session(session_id)
        return await session.stop_pickup()

    async def start_minigame(self, session_id: str) -> SessionSnapshot:
        session = await self._minigame_session(session_id)
        return await session.start_minigame()

    async def stop_minigame(self, session_id: str) -> SessionSnapshot:
        session = await self._minigame_session(session_id)
        return await session.stop_minigame()

    async def disconnect(self, session_id: str) -> SessionSnapshot:
        await self.snapshot(session_id)
        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            raise GameSessionNotFound()
        return await session.stop_minigame_and_disconnect()

    async def wait_for_terminal(self, session_id: str) -> SessionSnapshot:
        snapshot = await self.snapshot(session_id)
        if snapshot.state in {SessionState.CLOSED, SessionState.ERROR}:
            return snapshot

        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            return await self.snapshot(session_id)

        await session.wait_finished()
        return session.snapshot()

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._sessions_by_id.clear()
            self._finished.clear()
            self._valid_until.clear()
            self._latest_by_uin.clear()
        await asyncio.gather(
            *(session.close() for session in sessions), return_exceptions=True
        )
        async with self._lock:
            # Session completion callbacks run during close and may have observed
            # the old cutoff. Shutdown must not leave resumable status records.
            self._finished.clear()
            self._valid_until.clear()
            self._latest_by_uin.clear()

    async def _remove_finished(self, uin: str, session: GameSession) -> None:
        snapshot = session.snapshot()
        async with self._lock:
            if self._sessions.get(uin) is session:
                del self._sessions[uin]
            self._sessions_by_id.pop(session.session_id, None)
            valid_until = self._valid_until.get(session.session_id)
            if (
                valid_until is not None
                and self._now().astimezone(SHANGHAI) < valid_until
            ):
                self._finished[session.session_id] = snapshot
            else:
                self._finished.pop(session.session_id, None)
                self._valid_until.pop(session.session_id, None)
                if self._latest_by_uin.get(uin) == session.session_id:
                    del self._latest_by_uin[uin]

    async def _farm_session(self, session_id: str) -> GameSession:
        await self.snapshot(session_id)
        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            raise FarmOperationError(
                "FARM_SESSION_NOT_READY",
                "挂机会话已经结束，暂时不能操作农场",
            )
        return session

    async def _minigame_session(self, session_id: str) -> GameSession:
        await self.snapshot(session_id)
        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            raise MinigameOperationError(
                "MINIGAME_SESSION_NOT_READY",
                "挂机会话已经结束，暂时不能操作小游戏",
            )
        return session

    async def _paradise_session(self, session_id: str) -> GameSession:
        await self.snapshot(session_id)
        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            raise ParadiseOperationError(
                "PARADISE_SESSION_NOT_READY",
                "游戏会话已经结束，暂时不能操作乐园",
            )
        return session

    async def _pickup_session(self, session_id: str) -> GameSession:
        await self.snapshot(session_id)
        async with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            raise PickupOperationError(
                "PICKUP_SESSION_NOT_READY",
                "游戏会话已经结束，暂时不能进行取物",
            )
        return session

    def _active_session_count(self) -> int:
        return sum(session.is_active for session in self._sessions.values())


def _parse_xml(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise GameAutomationError("game HTTP response was not valid XML") from exc


def _xml_attribute_int(
    element: ET.Element,
    name: str,
    default: int,
) -> int:
    try:
        return int(element.attrib.get(name, str(default)))
    except ValueError:
        return default


def _response_metadata(response: httpx.Response) -> tuple[str, int, str]:
    content = response.content
    content_type = _safe_response_message(
        response.headers.get("content-type", "unknown")
    )
    return (
        content_type,
        len(content),
        hashlib.sha256(content).hexdigest()[:16],
    )


def _safe_response_message(value: str) -> str:
    """Return bounded diagnostic text without user or credential values."""

    normalized = " ".join(value.split())
    normalized = _SENSITIVE_RESPONSE_FIELD_PATTERN.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        normalized,
    )
    normalized = _LONG_SECRET_PATTERN.sub("<redacted>", normalized)
    normalized = _UIN_IN_MESSAGE_PATTERN.sub(
        lambda match: f"***{match.group(1)[-4:]}",
        normalized,
    )
    return normalized[:_MAX_SAFE_RESPONSE_MESSAGE_CHARS]


def _xml_text(root: ET.Element, name: str) -> str:
    node = root.find(name)
    if node is None:
        return ""
    if node.text and node.text.strip():
        return node.text.strip()
    return node.attrib.get("value", "").strip()


def _xml_int(root: ET.Element, name: str, default: int) -> int:
    try:
        return int(_xml_text(root, name))
    except ValueError:
        return default
