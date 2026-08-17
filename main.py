"""洛克王国 · 自动挂机控制台（PySide6 GUI / 配置驱动入口）"""

import json
import os
import subprocess
import sys
import threading
import time

import httpx

SERVICE_URL = os.getenv("ROCO_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
SERVER_MODULE = "src.roco_mine_mini_service.server"


def _format_duration_compact(value) -> str:
    """Format seconds as 1h19min / 2min / 45sec; pass through non-numbers."""

    try:
        seconds = max(0, int(value))
    except (TypeError, ValueError):
        return str(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}min" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}min" if seconds == 0 else f"{minutes}min{seconds}sec"
    return f"{seconds}sec"


def launch_gui() -> int:
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtGui import QFont, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    class SignalBridge(QObject):
        log = Signal(str)
        service_state = Signal(bool)
        api_result = Signal(str, int)
        qr_image = Signal(bytes)
        login_state = Signal(bool, str)
        status_data = Signal(dict)
        farm_data = Signal(dict)
        paradise_data = Signal(dict)
        automation_data = Signal(dict)

    class RocoGUI(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("洛克王国 · 自动挂机控制台")
            self.resize(1180, 920)
            self.setMinimumSize(1060, 840)

            self.bridge = SignalBridge()
            self.server_process = None
            self.server_started_by_gui = False
            self.request_lock = threading.Lock()
            self.login_polling = False
            self.service_closed_by_user = False

            self.build_ui()
            self.connect_signals()

            self.log(f"API 地址：{SERVICE_URL}")
            self.log("正在检查本地服务……")

            threading.Thread(target=self.ensure_service, daemon=True).start()

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.periodic_check)
            self.timer.start(5000)

        def build_ui(self):
            self.setStyleSheet("""
                QMainWindow { background: #f5f7fa; }
                QGroupBox {
                    background: white;
                    border: 1px solid #e1e5eb;
                    border-radius: 12px;
                    margin-top: 12px;
                    padding: 12px;
                    font-weight: bold;
                    color: #20242a;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 14px;
                    padding: 0 5px;
                }
                QPushButton {
                    min-height: 32px;
                    border: none;
                    border-radius: 9px;
                    background: #eef1f5;
                    color: #20242a;
                    font-size: 13px;
                }
                QPushButton:hover { background: #e2e6eb; }
                QPushButton:pressed { background: #d8dde3; }
                QPushButton:disabled { color: #a7adb5; background: #f1f2f4; }
                QTextEdit {
                    border: 1px solid #e1e5eb;
                    border-radius: 9px;
                    background: #fbfcfd;
                }
            """)

            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(18, 18, 18, 18)
            root.setSpacing(14)

            # 头部栏
            header = QFrame()
            header.setFixedHeight(80)
            header.setStyleSheet("""
                QFrame { background: #20252b; border-radius: 14px; }
                QLabel { color: white; }
            """)
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(20, 10, 20, 10)

            title_layout = QVBoxLayout()
            title = QLabel("洛克王国")
            title.setFont(QFont("Microsoft YaHei UI", 20, QFont.Bold))
            subtitle = QLabel("本地自动挂机控制台（挂机 / 农场 / 乐园 / 全自动）")
            subtitle.setStyleSheet("color:#aeb7c2;font-size:12px;")
            title_layout.addWidget(title)
            title_layout.addWidget(subtitle)

            header_layout.addLayout(title_layout)
            header_layout.addStretch()

            self.lbl_service_top = QLabel("● 服务检查中")
            self.lbl_service_top.setStyleSheet("color:#ffd666;font-weight:bold;font-size:14px;")
            header_layout.addWidget(self.lbl_service_top)
            root.addWidget(header)

            # 状态栏
            status_group = QGroupBox("账号状态")
            status_layout = QGridLayout(status_group)
            status_layout.setHorizontalSpacing(35)
            status_layout.setVerticalSpacing(10)

            self.lbl_service = QLabel("● 本地服务：检查中")
            self.lbl_login = QLabel("● QQ 登录：未检测")
            self.lbl_game = QLabel("● 游戏状态：未知")
            self.lbl_status = QLabel("当前状态：未知")
            self.lbl_credits = QLabel("学分：-")
            self.lbl_coins = QLabel("洛克贝：-")
            self.lbl_online = QLabel("在线时间：-")
            self.lbl_paradise = QLabel("乐园次数：-")
            self.lbl_countdown = QLabel("本次探险：-")
            self.lbl_farm = QLabel("农场：-")
            self.lbl_seeds = QLabel("种子背包：-")
            self.lbl_automation = QLabel("全自动：未运行")

            for label in (self.lbl_service, self.lbl_login, self.lbl_game):
                label.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))

            status_layout.addWidget(self.lbl_service, 0, 0)
            status_layout.addWidget(self.lbl_login, 0, 1)
            status_layout.addWidget(self.lbl_game, 0, 2)
            status_layout.addWidget(self.lbl_status, 1, 0)
            status_layout.addWidget(self.lbl_credits, 1, 1)
            status_layout.addWidget(self.lbl_coins, 1, 2)
            status_layout.addWidget(self.lbl_online, 2, 0)
            status_layout.addWidget(self.lbl_paradise, 2, 1)
            status_layout.addWidget(self.lbl_countdown, 2, 2)
            status_layout.addWidget(self.lbl_farm, 3, 0, 1, 2)
            status_layout.addWidget(self.lbl_automation, 3, 2)
            status_layout.addWidget(self.lbl_seeds, 4, 0, 1, 3)
            root.addWidget(status_group)

            # 中间区域
            middle = QHBoxLayout()
            middle.setSpacing(14)

            # 1. 操作面板（两列）
            operation_group = QGroupBox("操作")
            operation_layout = QGridLayout(operation_group)
            operation_layout.setSpacing(8)

            self.btn_start_service = self.create_button("▶ 启动服务", "blue")
            self.btn_stop_service = self.create_button("■ 关闭服务", "red")
            self.btn_scan = self.create_button("扫码登录", "blue")
            self.btn_password = self.create_button("密码登录", "blue")
            self.btn_hang = self.create_button("开始挂机", "green")
            self.btn_hang_stop = self.create_button("停止挂机")
            self.btn_disconnect = self.create_button("断开账号")
            self.btn_status = self.create_button("查询状态")
            self.btn_online = self.create_button("在线时间")
            self.btn_farm = self.create_button("农场状态")
            self.btn_harvest = self.create_button("收菜")
            self.btn_plant = self.create_button("播种")
            self.btn_paradise = self.create_button("乐园状态")
            self.btn_adventure = self.create_button("开始探险")
            self.btn_claim = self.create_button("领取奖励")
            self.btn_auto_start = self.create_button("全自动开", "green")
            self.btn_auto_stop = self.create_button("全自动关", "red")
            self.btn_help = self.create_button("使用说明")

            buttons = [
                (self.btn_start_service, 0, 0),
                (self.btn_stop_service, 0, 1),
                (self.btn_scan, 1, 0),
                (self.btn_password, 1, 1),
                (self.btn_hang, 2, 0),
                (self.btn_hang_stop, 2, 1),
                (self.btn_disconnect, 3, 0),
                (self.btn_status, 3, 1),
                (self.btn_online, 4, 0),
                (self.btn_farm, 4, 1),
                (self.btn_harvest, 5, 0),
                (self.btn_plant, 5, 1),
                (self.btn_paradise, 6, 0),
                (self.btn_adventure, 6, 1),
                (self.btn_claim, 7, 0),
                (self.btn_auto_start, 7, 1),
                (self.btn_auto_stop, 8, 0),
                (self.btn_help, 8, 1),
            ]
            for button, row, column in buttons:
                operation_layout.addWidget(button, row, column)

            operation_group.setFixedWidth(300)
            middle.addWidget(operation_group)

            # 2. 登录面板
            login_group = QGroupBox("登录")
            login_layout = QVBoxLayout(login_group)
            login_layout.setSpacing(10)

            self.lbl_login_hint = QLabel("点击左侧「扫码登录」获取二维码")
            self.lbl_login_hint.setAlignment(Qt.AlignCenter)
            self.lbl_login_hint.setWordWrap(True)
            self.lbl_login_hint.setFixedHeight(40)
            self.lbl_login_hint.setStyleSheet("color:#7b8490;font-size:13px;")
            login_layout.addWidget(self.lbl_login_hint)

            qr_container = QFrame()
            qr_container.setMinimumSize(220, 220)
            qr_container.setStyleSheet("""
                QFrame {
                    background:#fafbfc;
                    border:1px solid #e1e5eb;
                    border-radius:12px;
                }
            """)
            qr_layout = QVBoxLayout(qr_container)
            qr_layout.setContentsMargins(10, 10, 10, 10)

            self.lbl_qr = QLabel("二维码将在这里显示")
            self.lbl_qr.setAlignment(Qt.AlignCenter)
            self.lbl_qr.setStyleSheet("border:none;color:#8c959f;font-size:14px;")
            qr_layout.addWidget(self.lbl_qr)

            login_layout.addWidget(qr_container, 1)

            self.btn_login_hang = QPushButton("登录成功 · 开始挂机")
            self.btn_login_hang.setFixedHeight(40)
            self.btn_login_hang.setEnabled(False)
            self.btn_login_hang.setStyleSheet("""
                QPushButton { background:#52c41a; color:white; font-weight:bold; border-radius:9px; }
                QPushButton:hover { background:#389e0d; }
                QPushButton:disabled { background:#e5e7eb; color:#a0a5ab; }
            """)
            login_layout.addWidget(self.btn_login_hang)
            middle.addWidget(login_group, 1)

            # 3. API 返回
            api_group = QGroupBox("API 返回")
            api_layout = QVBoxLayout(api_group)
            self.txt_api = QTextEdit()
            self.txt_api.setReadOnly(True)
            self.txt_api.setFont(QFont("Consolas", 9))
            api_layout.addWidget(self.txt_api)
            api_group.setFixedWidth(320)
            middle.addWidget(api_group)

            root.addLayout(middle, 1)

            # 日志面板
            log_group = QGroupBox("运行日志")
            log_layout = QVBoxLayout(log_group)
            self.txt_log = QTextEdit()
            self.txt_log.setReadOnly(True)
            self.txt_log.setFont(QFont("Consolas", 9))
            log_layout.addWidget(self.txt_log)
            root.addWidget(log_group, 1)

            self.update_service_state(False)

        def create_button(self, text, style="normal"):
            button = QPushButton(text)
            if style == "blue":
                button.setStyleSheet("""
                    QPushButton { background:#1677ff; color:white; font-weight:bold; }
                    QPushButton:hover { background:#4096ff; }
                """)
            elif style == "green":
                button.setStyleSheet("""
                    QPushButton { background:#52c41a; color:white; font-weight:bold; }
                    QPushButton:hover { background:#73d13d; }
                    QPushButton:disabled { background:#e5e7eb; color:#a0a5ab; }
                """)
            elif style == "red":
                button.setStyleSheet("""
                    QPushButton { background:#fff1f0; color:#cf1322; font-weight:bold; }
                    QPushButton:hover { background:#ffccc7; }
                    QPushButton:disabled { background:#f5f5f5; color:#d9d9d9; }
                """)
            return button

        def connect_signals(self):
            self.bridge.log.connect(self.log)
            self.bridge.service_state.connect(self.update_service_state)
            self.bridge.api_result.connect(self.update_api_result)
            self.bridge.qr_image.connect(self.show_qr)
            self.bridge.login_state.connect(self.update_login_state)
            self.bridge.status_data.connect(self.update_status)
            self.bridge.farm_data.connect(self.update_farm)
            self.bridge.paradise_data.connect(self.update_paradise)
            self.bridge.automation_data.connect(self.update_automation)

            self.btn_start_service.clicked.connect(self.start_service_clicked)
            self.btn_stop_service.clicked.connect(self.stop_service_clicked)
            self.btn_scan.clicked.connect(self.scan_clicked)
            self.btn_password.clicked.connect(self.password_login_clicked)
            self.btn_hang.clicked.connect(self.hang_clicked)
            self.btn_hang_stop.clicked.connect(self.hang_stop_clicked)
            self.btn_login_hang.clicked.connect(self.hang_clicked)
            self.btn_status.clicked.connect(self.status_clicked)
            self.btn_online.clicked.connect(self.online_clicked)
            self.btn_disconnect.clicked.connect(self.disconnect_clicked)
            self.btn_farm.clicked.connect(self.farm_clicked)
            self.btn_harvest.clicked.connect(self.harvest_clicked)
            self.btn_plant.clicked.connect(self.plant_clicked)
            self.btn_paradise.clicked.connect(self.paradise_clicked)
            self.btn_adventure.clicked.connect(self.adventure_clicked)
            self.btn_claim.clicked.connect(self.claim_clicked)
            self.btn_auto_start.clicked.connect(self.auto_start_clicked)
            self.btn_auto_stop.clicked.connect(self.auto_stop_clicked)
            self.btn_help.clicked.connect(self.help_clicked)

        def log(self, text):
            self.txt_log.append(time.strftime("[%H:%M:%S] ") + str(text))
            scrollbar = self.txt_log.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        def update_api_result(self, raw_text, status_code):
            try:
                formatted = json.dumps(json.loads(raw_text), indent=2, ensure_ascii=False)
            except Exception:
                formatted = raw_text
            self.txt_api.setText(f"HTTP Status: {status_code}\n\n{formatted}")

        def update_status(self, data):
            state = data.get("status", "unknown")
            self.lbl_status.setText(f"当前状态：{state}")
            self.lbl_credits.setText(f"学分：{data.get('credits', '-')}")
            self.lbl_coins.setText(f"洛克贝：{data.get('rock_coins', '-')}")
            online = data.get("online_time_seconds")
            if online is None:
                online = data.get("online_time", "-")
            self.lbl_online.setText(f"在线时间：{_format_duration_compact(online)}")

        def update_farm(self, data):
            self.lbl_farm.setText(
                f"农场：{data.get('message', '-')}"
            )
            seeds = data.get("seeds") or []
            if seeds:
                parts = [
                    f"0x{int(s.get('seed_id', 0)):08X}×{s.get('count', 0)}"
                    for s in seeds[:8]
                ]
                if len(seeds) > 8:
                    parts.append(f"…共{len(seeds)}种")
                self.lbl_seeds.setText("种子背包：" + "，".join(parts))
            else:
                self.lbl_seeds.setText("种子背包：-")

        def update_paradise(self, data):
            remaining = data.get("remaining", -1)
            countdown = data.get("countdown", -1)
            self.lbl_paradise.setText(
                f"乐园次数：{data.get('times', '-')}/{data.get('limit', '-')}"
                + (f"（剩 {remaining}）" if remaining >= 0 else "")
            )
            if countdown == 0:
                self.lbl_countdown.setText("本次探险：可领奖")
            elif countdown < 0:
                self.lbl_countdown.setText("本次探险：空闲")
            else:
                self.lbl_countdown.setText(f"本次探险：剩余 {countdown} 秒")

        def update_automation(self, data):
            active = data.get("active", False)
            self.lbl_automation.setText("全自动：运行中" if active else "全自动：未运行")

        def update_login_state(self, is_logged_in, message=""):
            if is_logged_in:
                self.lbl_login.setText("● QQ 登录：已登录")
                self.lbl_login.setStyleSheet("color:#52c41a;font-weight:bold;")
                self.btn_login_hang.setEnabled(True)
            else:
                self.lbl_login.setText(f"● QQ 登录：{message or '未登录'}")
                self.lbl_login.setStyleSheet("color:#ff4d4f;font-weight:bold;")
                self.btn_login_hang.setEnabled(False)

        def service_alive(self):
            try:
                response = httpx.get(f"{SERVICE_URL}/api/v1/status", timeout=1.5)
                return response.status_code < 500
            except Exception:
                return False

        def periodic_check(self):
            if self.service_closed_by_user:
                return
            alive = self.service_alive()
            self.bridge.service_state.emit(alive)
            if alive:
                self.request_api_light("GET", "/api/v1/paradise", self.bridge.paradise_data)
                self.request_api_light("GET", "/api/v1/automation/status", self.bridge.automation_data)

        def request_api_light(self, method, path, signal):
            def worker():
                try:
                    response = httpx.get(f"{SERVICE_URL}{path}", timeout=3)
                    signal.emit(response.json())
                except Exception:
                    pass
            threading.Thread(target=worker, daemon=True).start()

        def ensure_service(self):
            if self.service_alive():
                self.server_started_by_gui = False
                self.bridge.log.emit("检测到 FastAPI 已经运行。")
                self.bridge.service_state.emit(True)
                self.check_login()
                return

            self.bridge.log.emit("FastAPI 未运行，准备自动启动。")
            self.start_service_internal()

        def start_service_internal(self):
            if self.service_alive():
                self.bridge.service_state.emit(True)
                return

            try:
                root = os.path.dirname(os.path.abspath(__file__))
                os.makedirs(os.path.join(root, "logs"), exist_ok=True)
                log_path = os.path.join(root, "logs", "gui-server.log")
                log_file = open(log_path, "a", encoding="utf-8", buffering=1)

                command = [sys.executable, "-m", SERVER_MODULE]
                creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

                self.server_process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                self.server_started_by_gui = True

                self.bridge.log.emit("正在启动 FastAPI……")
                self.bridge.log.emit(f"PID：{self.server_process.pid}")

                for _ in range(30):
                    if self.service_alive():
                        self.bridge.log.emit("✓ FastAPI 启动成功。")
                        self.bridge.service_state.emit(True)
                        self.check_login()
                        return
                    time.sleep(0.5)

                self.bridge.log.emit("✗ FastAPI 启动超时。")
                self.bridge.service_state.emit(False)

            except Exception as exc:
                self.bridge.log.emit(f"启动服务失败：{exc}")
                self.bridge.service_state.emit(False)

        def start_service_clicked(self):
            self.service_closed_by_user = False
            self.btn_start_service.setEnabled(False)
            self.lbl_service_top.setText("● 正在启动服务")
            self.lbl_service_top.setStyleSheet("color:#ffd666;font-weight:bold;")
            self.bridge.log.emit("用户请求启动服务。")
            threading.Thread(target=self.start_service_internal, daemon=True).start()

        def stop_service_clicked(self):
            answer = QMessageBox.question(
                self,
                "关闭服务",
                "确定关闭 FastAPI 服务吗？\n\n关闭后可以通过「启动服务」重新启动。",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

            self.service_closed_by_user = True

            def worker():
                if self.server_started_by_gui and self.server_process:
                    try:
                        if self.server_process.poll() is None:
                            self.bridge.log.emit("正在关闭 FastAPI……")
                            self.server_process.terminate()
                            try:
                                self.server_process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                self.server_process.kill()
                    except Exception as exc:
                        self.bridge.log.emit(f"关闭服务异常：{exc}")
                else:
                    self.bridge.log.emit("当前服务不是 GUI 启动的，不会强制关闭外部服务。")

                self.server_process = None
                self.server_started_by_gui = False
                self.bridge.service_state.emit(False)
                self.bridge.log.emit("FastAPI 服务已关闭。")

            threading.Thread(target=worker, daemon=True).start()

        def update_service_state(self, running):
            if running:
                self.lbl_service.setText("● 本地服务：运行中")
                self.lbl_service.setStyleSheet("color:#52c41a;font-weight:bold;")
                self.lbl_service_top.setText("● 服务运行中")
                self.lbl_service_top.setStyleSheet("color:#95de64;font-weight:bold;")
                self.set_service_buttons(True)
                self.btn_start_service.setEnabled(False)
                self.btn_stop_service.setEnabled(True)
            else:
                self.lbl_service.setText("● 本地服务：已停止")
                self.lbl_service.setStyleSheet("color:#ff4d4f;font-weight:bold;")
                self.lbl_service_top.setText("● 服务已停止")
                self.lbl_service_top.setStyleSheet("color:#ff7875;font-weight:bold;")
                self.set_service_buttons(False)
                self.btn_start_service.setEnabled(True)
                self.btn_stop_service.setEnabled(False)

        def set_service_buttons(self, enabled):
            for btn in (
                self.btn_scan, self.btn_password, self.btn_hang,
                self.btn_hang_stop, self.btn_status,
                self.btn_online, self.btn_disconnect, self.btn_farm,
                self.btn_harvest, self.btn_plant, self.btn_paradise,
                self.btn_adventure, self.btn_claim,
                self.btn_auto_start, self.btn_auto_stop, self.btn_help,
            ):
                btn.setEnabled(enabled)
            if not enabled:
                self.btn_login_hang.setEnabled(False)

        def request_api(self, method, path, description, body=None):
            if not self.request_lock.acquire(blocking=False):
                self.log("上一个请求还没有完成，请稍候。")
                return

            def worker():
                try:
                    url = f"{SERVICE_URL}{path}"
                    self.bridge.log.emit(f"请求：{method} {path}")
                    if method == "GET":
                        response = httpx.get(url, timeout=15)
                    else:
                        response = httpx.post(url, json=body, timeout=60)

                    self.bridge.api_result.emit(response.text, response.status_code)

                    try:
                        data = response.json()
                    except Exception:
                        data = {}

                    if isinstance(data, dict):
                        if "/farm" in path or path == "/api/v1/farm":
                            self.bridge.farm_data.emit(data)
                        elif "/paradise" in path or path == "/api/v1/paradise":
                            self.bridge.paradise_data.emit(data)
                        elif "/automation" in path:
                            self.bridge.automation_data.emit(data)
                        else:
                            self.bridge.status_data.emit(data)

                    if response.status_code >= 400:
                        detail = ""
                        if isinstance(data, dict):
                            detail = str(data.get("message", ""))
                        self.bridge.log.emit(
                            f"{description}失败：HTTP {response.status_code}"
                            + (f"：{detail}" if detail else "")
                        )
                    else:
                        self.bridge.log.emit(f"{description}成功。")
                except Exception as exc:
                    self.bridge.log.emit(f"{description}失败：{type(exc).__name__}: {exc}")
                finally:
                    self.request_lock.release()

            threading.Thread(target=worker, daemon=True).start()

        def check_login(self):
            self.request_api("GET", "/api/v1/status", "获取状态")

        def _read_config_credentials(self):
            try:
                import yaml

                with open("config.yaml", encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                return str(data.get("account", "")).strip(), str(data.get("password", ""))
            except Exception:
                return "", ""

        def scan_clicked(self):
            self.clear_qr()
            self.lbl_login_hint.setText("正在获取二维码……")
            self.btn_login_hang.setEnabled(False)

            def worker():
                try:
                    response = httpx.post(f"{SERVICE_URL}/api/v1/scan", timeout=15)
                    self.bridge.api_result.emit(response.text, response.status_code)
                    if response.status_code != 200:
                        self.bridge.log.emit(f"扫码接口返回 HTTP {response.status_code}")
                        return

                    data = response.json()
                    qr_url = data.get("qr_url")
                    if not qr_url:
                        self.bridge.log.emit("扫码接口没有返回 qr_url。")
                        return

                    self.bridge.log.emit("二维码地址获取成功。")
                    self.download_qr(qr_url)
                except Exception as exc:
                    self.bridge.log.emit(f"扫码失败：{exc}")
                    self.bridge.login_state.emit(False, "获取二维码失败")

            threading.Thread(target=worker, daemon=True).start()

        def password_login_clicked(self):
            account, password = self._read_config_credentials()
            if not account or not password:
                account, ok1 = QInputDialog.getText(self, "密码登录", "QQ 账号：")
                if not ok1 or not account.strip():
                    return
                password, ok2 = QInputDialog.getText(self, "密码登录", "QQ 密码：")
                if not ok2 or not password:
                    return
            self.request_api(
                "POST",
                "/api/v1/login",
                "密码登录",
                body={"account": account.strip(), "password": password},
            )

        def download_qr(self, qr_url):
            def worker():
                try:
                    response = httpx.get(qr_url, timeout=10)
                    if response.status_code != 200:
                        self.bridge.log.emit(f"二维码下载失败：HTTP {response.status_code}")
                        return

                    self.bridge.qr_image.emit(response.content)
                    self.bridge.log.emit("✓ 二维码显示成功。")
                    self.start_login_polling()
                except Exception as exc:
                    self.bridge.log.emit(f"二维码下载失败：{exc}")

            threading.Thread(target=worker, daemon=True).start()

        def clear_qr(self):
            self.lbl_qr.clear()
            self.lbl_qr.setText("正在获取二维码……")

        def show_qr(self, image_data):
            pixmap = QPixmap()
            if not pixmap.loadFromData(image_data):
                self.lbl_qr.setText("二维码加载失败")
                return

            size = self.lbl_qr.size()
            pixmap = pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_qr.setPixmap(pixmap)
            self.lbl_login_hint.setText("请使用手机 QQ 扫描二维码\n扫码成功后，系统会自动检测登录状态")

        def start_login_polling(self):
            if self.login_polling:
                return
            self.login_polling = True

            def worker():
                self.bridge.log.emit("开始等待扫码登录结果……")
                try:
                    for _ in range(60):
                        if self.service_closed_by_user:
                            return
                        try:
                            response = httpx.get(f"{SERVICE_URL}/api/v1/status", timeout=5)
                            data = response.json()
                            self.bridge.status_data.emit(data)

                            if data.get("status") == "online":
                                self.bridge.login_state.emit(True, "登录成功")
                                self.bridge.log.emit("✓ 检测到账号已成功登录！")
                                break
                        except Exception:
                            pass
                        time.sleep(2)
                finally:
                    self.login_polling = False

            threading.Thread(target=worker, daemon=True).start()

        def hang_clicked(self):
            self.request_api("POST", "/api/v1/hang", "开始挂机")

        def hang_stop_clicked(self):
            self.request_api("POST", "/api/v1/hang/stop", "停止挂机")

        def status_clicked(self):
            self.request_api("GET", "/api/v1/status", "查询状态")

        def online_clicked(self):
            self.request_api("GET", "/api/v1/online-time", "查询在线时间")

        def farm_clicked(self):
            self.request_api("GET", "/api/v1/farm", "查询农场状态")

        def harvest_clicked(self):
            self.request_api("POST", "/api/v1/farm/harvest", "收菜")

        def plant_clicked(self):
            self.request_api("POST", "/api/v1/farm/plant", "播种", body={})

        def paradise_clicked(self):
            self.request_api("GET", "/api/v1/paradise", "查询乐园状态")

        def adventure_clicked(self):
            self.request_api("POST", "/api/v1/paradise/start", "开始探险")

        def claim_clicked(self):
            self.request_api("POST", "/api/v1/paradise/claim", "领取奖励")

        def auto_start_clicked(self):
            self.request_api(
                "POST",
                "/api/v1/automation/start",
                "启动全自动",
                body={"log_interval": 5},
            )

        def auto_stop_clicked(self):
            self.request_api("POST", "/api/v1/automation/stop", "停止全自动")

        def disconnect_clicked(self):
            self.request_api("POST", "/api/v1/disconnect", "断开账号")

        def help_clicked(self):
            QMessageBox.information(
                self,
                "使用说明",
                "1. 确认本地服务处于「运行中」状态。\n"
                "2. 登录：点击「扫码登录」用手机 QQ 扫码，或点击「密码登录」使用 config.yaml 中的账号密码。\n"
                "3. 点击「开始挂机」发起小游戏挂机；「停止挂机」只停挂机不掉线（之后可收菜/播种），\n"
                "   「断开账号」则停止挂机并断开连接（需重新登录）。\n"
                "4. 农场：点击「农场状态」查看土地/种子，点「收菜」收获作物，点「播种」自动补种。\n"
                "5. 乐园：点击「乐园状态」查看探险进度，点「开始探险」/「领取奖励」。\n"
                "6. 密码登录后点击「全自动开」：每 5 秒记录洛克贝/在线时间/乐园次数与倒计时，\n"
                "   自动收菜、自动播种、自动探险与领奖。\n"
                "7. config.yaml 中配置了 account + password 时，直接运行 gui.py 会全后台执行，不显示本界面。",
            )

    app = QApplication(sys.argv)
    window = RocoGUI()
    window.show()
    return app.exec()


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root, "src"))
    from roco_mine_mini_service.launcher import run

    sys.exit(run())
