"""HTTP API designed to be called by a QQ bot."""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .game_sessions import MinigameOperationError
from .qq_login import QqLoginError
from .service import (
    AlreadyConnected,
    AutomationOperationError,
    BOT_COMMANDS,
    BotStatus,
    OnlineTimeStatus,
    QrNotFound,
    ScanInProgress,
    ServiceClosed,
    SingleUserGameService,
    help_message,
)


def create_app(service: SingleUserGameService | None = None) -> FastAPI:
    game = service or SingleUserGameService()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await game.close()

    app = FastAPI(title="Roco Mine Mini Service", version="0.3.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/scan")
    async def scan(request: Request) -> Response:
        try:
            ticket = await game.create_scan()
        except ServiceClosed:
            return _error(503, "SERVICE_CLOSED", "23:00 至 00:00 暂停服务")
        except ScanInProgress:
            return _error(409, "SCAN_IN_PROGRESS", "已有二维码正在等待扫码")
        except AlreadyConnected:
            return _error(409, "ALREADY_CONNECTED", "当前已经登录，请先断开连接")
        except QqLoginError as exc:
            return _error(502, exc.error_code, str(exc))
        public_base_url = os.getenv("ROCO_PUBLIC_BASE_URL", "").rstrip("/")
        qr_url = (
            f"{public_base_url}/api/v1/qr/{ticket.token}"
            if public_base_url
            else str(request.url_for("qr_image", token=ticket.token))
        )
        return JSONResponse(
            {
                "status": "waiting_scan",
                "message": "请在两分钟内扫码",
                "qr_url": qr_url,
                "expires_in": ticket.expires_in,
            }
        )

    @app.get(
        "/api/v1/qr/{token}",
        name="qr_image",
        responses={200: {"content": {"image/png": {}}}},
    )
    async def qr_image(token: str) -> Response:
        try:
            image = game.qr_image(token)
        except QrNotFound:
            return _error(404, "QR_NOT_FOUND", "二维码不存在或已经过期")
        return Response(
            image,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/hang")
    async def hang() -> JSONResponse:
        try:
            return _bot_response(await game.start_hang_for_bot())
        except MinigameOperationError as exc:
            return _error(409, exc.error_code, str(exc))

    @app.post("/api/v1/hang/stop")
    async def hang_stop() -> JSONResponse:
        try:
            return _bot_response(await game.stop_minigame_for_bot())
        except MinigameOperationError as exc:
            return _error(409, exc.error_code, str(exc))

    @app.post("/api/v1/disconnect")
    async def disconnect() -> JSONResponse:
        return _bot_response(await game.disconnect_for_bot())

    @app.post("/api/v1/login")
    async def login(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return _error(400, "INVALID_PAYLOAD", "请求体必须是 JSON")
        account = str(payload.get("account", "")).strip()
        password = str(payload.get("password", ""))
        if not account or not password:
            return _error(400, "CREDENTIALS_REQUIRED", "account 和 password 不能为空")
        try:
            return _bot_response(await game.login_with_password(account, password))
        except ServiceClosed:
            return _error(503, "SERVICE_CLOSED", "23:00 至 00:00 暂停服务")
        except ScanInProgress:
            return _error(409, "SCAN_IN_PROGRESS", "已有扫码任务正在进行")
        except AlreadyConnected:
            return _error(409, "ALREADY_CONNECTED", "当前已经登录，请先断开连接")
        except QqLoginError as exc:
            return _error(502, exc.error_code, str(exc))

    @app.get("/api/v1/farm")
    async def farm_status() -> JSONResponse:
        return JSONResponse(asdict(await game.farm_status_for_bot()))

    @app.post("/api/v1/farm/harvest")
    async def farm_harvest() -> JSONResponse:
        return JSONResponse(asdict(await game.harvest_farm_for_bot()))

    @app.post("/api/v1/farm/plant")
    async def farm_plant(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            seed_id = int(payload.get("seed_id"))
        except (TypeError, ValueError):
            seed_id = None
        fallback_seed_ids = _optional_seed_list(payload.get("fallback_seed_ids"))
        return JSONResponse(
            asdict(
                await game.plant_auto_for_bot(
                    seed_id=seed_id,
                    fallback_seed_ids=fallback_seed_ids,
                )
            )
        )

    @app.get("/api/v1/paradise")
    async def paradise_status() -> JSONResponse:
        return JSONResponse(asdict(await game.paradise_status_for_bot()))

    @app.post("/api/v1/paradise/start")
    async def paradise_start() -> JSONResponse:
        return JSONResponse(asdict(await game.start_paradise_for_bot()))

    @app.post("/api/v1/paradise/claim")
    async def paradise_claim() -> JSONResponse:
        return JSONResponse(asdict(await game.claim_paradise_for_bot()))

    @app.post("/api/v1/automation/start")
    async def automation_start(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            status = await game.start_automation(
                farm=_optional_bool(payload.get("farm"), True),
                paradise=_optional_bool(payload.get("paradise"), True),
                hang=_optional_bool(payload.get("hang"), True),
                log_interval=_optional_int(payload.get("log_interval"), 5),
                farm_interval=_optional_int(payload.get("farm_interval"), 60),
                paradise_interval=_optional_int(payload.get("paradise_interval"), 15),
                hang_minutes=_optional_int(payload.get("hang_minutes"), 30),
                hang_cooldown_minutes=_optional_int(
                    payload.get("hang_cooldown_minutes"), 5
                ),
                preferred_seed_id=_optional_int(payload.get("preferred_seed_id")),
                fallback_seed_ids=_optional_seed_list(
                    payload.get("fallback_seed_ids")
                ),
            )
        except AutomationOperationError as exc:
            return _error(409, exc.error_code, str(exc))
        return JSONResponse(asdict(status))

    @app.post("/api/v1/automation/stop")
    async def automation_stop() -> JSONResponse:
        return JSONResponse(asdict(await game.stop_automation()))

    @app.get("/api/v1/automation/status")
    async def automation_status() -> JSONResponse:
        return JSONResponse(asdict(game.automation_status()))

    @app.get("/api/v1/status")
    async def status() -> JSONResponse:
        return _bot_response(await game.status_for_bot())

    @app.get("/api/v1/online-time")
    async def online_time() -> JSONResponse:
        return _online_time_response(await game.online_time_for_bot())

    @app.get("/api/v1/help")
    async def help_info() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "message": help_message(),
                "commands": [
                    {"command": command, "effect": effect}
                    for command, effect in BOT_COMMANDS
                ],
            }
        )

    return app


def _bot_response(status: BotStatus) -> JSONResponse:
    return JSONResponse(asdict(status))


def _online_time_response(status: OnlineTimeStatus) -> JSONResponse:
    return JSONResponse(asdict(status))


def _error(http_status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"status": "error", "code": code, "message": message},
        status_code=http_status,
    )


def _optional_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _optional_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_seed_list(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    seeds = []
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            seeds.append(parsed)
    return tuple(seeds)


def main() -> None:
    # GUI-launched and standalone processes both write business logs.
    from .logging_setup import configure_application_logging

    configure_application_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
