"""Headless password-login mode: no GUI, API on 8000, logs under logs/."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from .config import AppConfig
from .logging_setup import configure_application_logging
from .server import create_app
from .service import AlreadyConnected, ServiceClosed, SingleUserGameService

SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


def run_headless(config: AppConfig) -> int:
    """Run the whole service in the background until 23:00 or Ctrl+C."""

    root = Path.cwd().resolve()
    log_file = root / "logs" / "roco-mini-service.log"
    os.environ["ROCO_LOG_FILE"] = str(log_file)
    configure_application_logging()

    logger.info(
        "headless_mode_started account=%s log_file=%s",
        _masked_account(config.account),
        log_file,
    )
    try:
        asyncio.run(_main(config))
    except KeyboardInterrupt:
        logger.info("headless_mode_interrupted")
    return 0


async def _main(config: AppConfig) -> None:
    service = SingleUserGameService(
        preferred_seed_id=config.preferred_seed_id,
        fallback_seed_ids=config.fallback_seed_ids,
    )
    try:
        # If another instance of this service already owns the API port (e.g.
        # the GUI started it), do not start a second one: two instances would
        # fight over the same account.
        if _is_our_service_running(config.host, config.port):
            logger.info(
                "headless_service_already_running host=%s port=%d "
                "本服务已在运行，headless 模式退出（如需重启请先关闭现有服务进程）",
                config.host,
                config.port,
            )
            return

        try:
            status = await service.login_with_password(config.account, config.password)
            logger.info(
                "headless_login_result status=%s message=%s",
                status.status,
                status.message,
            )
        except AlreadyConnected:
            logger.info("headless_already_connected")
        except ServiceClosed:
            # 23:00-00:00 service window: refuse to log in, nothing else to do.
            logger.warning(
                "headless_service_closed_at_23_login_skipped "
                "(服务窗口 23:00-00:00 关闭，00:00 后可再次启动)"
            )
            return
        except Exception as exc:
            logger.error(
                "headless_login_failed error_type=%s error=%s",
                type(exc).__name__,
                _safe_error(exc),
            )
            return

        # 启动前先完成一轮收菜 / 播种 / 乐园探险（小游戏挂机进行中
        # 游戏协议不允许操作农场，因此必须在这之前处理）。
        if config.auto_farm or config.auto_paradise or config.auto_start_hang:
            await _initial_farm_and_paradise(service, config)

        if config.auto_farm or config.auto_paradise or config.auto_start_hang:
            try:
                await service.start_automation(
                    farm=config.auto_farm,
                    paradise=config.auto_paradise,
                    hang=config.auto_start_hang,
                    log_interval=config.auto_log_interval,
                    farm_interval=config.farm_interval,
                    paradise_interval=config.paradise_interval,
                    hang_minutes=config.hang_minutes,
                    hang_cooldown_minutes=config.hang_cooldown_minutes,
                    preferred_seed_id=config.preferred_seed_id,
                    fallback_seed_ids=config.fallback_seed_ids,
                )
                logger.info(
                    "headless_automation_started farm=%s paradise=%s hang=%s",
                    config.auto_farm,
                    config.auto_paradise,
                    config.auto_start_hang,
                )
            except Exception as exc:
                logger.warning(
                    "headless_automation_start_failed error_type=%s",
                    type(exc).__name__,
                )

        server = uvicorn.Server(
            uvicorn.Config(
                create_app(service),
                host=config.host,
                port=config.port,
                log_level="warning",
            )
        )
        if not _port_bindable(config.host, config.port):
            # Not our service (checked above) but the port is busy.
            logger.warning(
                "headless_port_in_use host=%s port=%d "
                "端口被其他程序占用，本模式继续运行（登录/自动化不受影响）但"
                "不提供 HTTP 服务；如需使用 8000 请关闭占用进程，"
                "或修改 config.yaml 的 port",
                config.host,
                config.port,
            )
            # Keep the automation running; Ctrl+C stops everything.
            await asyncio.Event().wait()
            return
        logger.info(
            "headless_server_starting host=%s port=%d",
            config.host,
            config.port,
        )
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(server.serve(), name="uvicorn-serve")
        ]
        if config.auto_exit_at_23:
            tasks.append(
                asyncio.create_task(
                    _auto_exit_watcher(server),
                    name="auto-exit-watcher",
                )
            )
        try:
            await asyncio.gather(*tasks)
        except OSError as exc:
            # Bind failure, e.g. another service already owns the port.
            logger.error(
                "headless_server_bind_failed host=%s port=%d error=%s "
                "(请先关闭占用该端口的进程，例如 netsh winsock reset 后重启，"
                "或检查是否有残留服务进程)",
                config.host,
                config.port,
                _safe_error(exc),
            )
    finally:
        await service.close()
        logger.info("headless_mode_stopped")


async def _auto_exit_watcher(server: uvicorn.Server) -> None:
    """Set uvicorn should_exit once the clock passes 23:00 Asia/Shanghai."""

    while True:
        current = datetime.now(SHANGHAI)
        if current.hour == 23:
            logger.info("auto_exit_at_23 triggered")
            server.should_exit = True
            return
        await asyncio.sleep(30)


def _masked_account(account: str) -> str:
    if len(account) <= 4:
        return "****"
    return f"***{account[-4:]}"


def _port_bindable(host: str, port: int) -> bool:
    """True when nothing else is listening on (host, port)."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _is_our_service_running(host: str, port: int) -> bool:
    """True when the /health endpoint of this service answers on the port."""

    try:
        response = httpx.get(
            f"http://{host}:{port}/health", timeout=1.5
        )
        return response.status_code == 200 and response.json().get("status") == "ok"
    except Exception:
        return False


async def _initial_farm_and_paradise(
    service: SingleUserGameService,
    config: AppConfig,
) -> None:
    """One farm + paradise pass before the minigame starts (best effort)."""

    await _wait_connected(service, timeout=30.0)
    if config.auto_farm:
        try:
            await service.harvest_farm_for_bot()
        except Exception as exc:
            logger.warning(
                "headless_initial_harvest_failed error_type=%s",
                type(exc).__name__,
            )
        try:
            result = await service.plant_auto_for_bot()
            logger.info("headless_initial_plant message=%s", result.message)
        except Exception as exc:
            logger.warning(
                "headless_initial_plant_failed error_type=%s",
                type(exc).__name__,
            )
    if config.auto_paradise:
        try:
            result = await service.start_paradise_for_bot()
            logger.info(
                "headless_initial_adventure status=%s message=%s",
                result.status,
                result.message,
            )
        except Exception as exc:
            logger.warning(
                "headless_initial_adventure_failed error_type=%s",
                type(exc).__name__,
            )


async def _wait_connected(
    service: SingleUserGameService,
    *,
    timeout: float,
) -> None:
    """Wait until the game session is connected (best effort)."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            status = await service.status_for_bot()
        except Exception:
            return
        if status.status != "connecting":
            return
        await asyncio.sleep(2.0)


def _safe_error(exc: BaseException) -> str:
    message = str(exc) or type(exc).__name__
    return " ".join(message.split())[:200]
