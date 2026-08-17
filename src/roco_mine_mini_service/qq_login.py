"""QQ QR login flow ported from the reference Kotlin implementation."""

from __future__ import annotations

import ast
import base64
import hashlib
import html
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)
_REDIRECT_URI = "https://17roco.qq.com/logintarget.html"
_QQ_APP_ID = "716027609"
_QQ_DAID = "383"
_QQ_THIRD_PARTY_APP_ID = "102061779"
_OAUTH_LOGIN_JUMP_URL = "https://graph.qq.com/oauth2.0/login_jump"
_UIN_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")
_LOGIN_VALUE_PATTERN = re.compile(
    r"\b(angel_uin|angel_key|skey|pskey)=([0-9A-Fa-f]+)"
)
_ALERT_PATTERN = re.compile(r"alert\(['\"](.+?)['\"]\)")
_SHANGHAI = ZoneInfo("Asia/Shanghai")

# Modern ptlogin2 login page constants (mirror the xlogin page / login_10.js).
_PTUI_VERSION = "26071711"
_JS_VERSION = "c1987b96"
_PT_STYLE = "40"
_PT_TEA = "2"
_PT_VCODE = "1"

# RSA-2048 public modulus embedded in the official login_10.js bundle.
_RSA_MODULUS_HEX = (
    "e9a815ab9d6e86abbf33a4ac64e9196d5be44a09bd0ed6ae052914e1a865ac8331"
    "fed863de8ea697e9a7f63329e5e23cda09c72570f46775b7e39ea9670086f847"
    "d3c9c51963b131409b1e04265d9747419c635404ca651bbcbc87f99b8008f7f5"
    "824653e3658be4ba73e4480156b390bb73bc1f8b33578e7a4e12440e9396f255"
    "2c1aff1c92e797ebacdc37c109ab7bce2367a19c56a033ee04534723cc2558cb"
    "27368f5b9d32c04d12dbd86bbd68b1d99b7c349a8453ea75d1b2e94491ab30ac"
    "f6c46a36a75b721b312bedf4e7aad21e54e9bcbcf8144c79b6e3c05eb4a15477"
    "50d224c0085d80e6da3907c3d945051c13c7c1dcefd6520ee8379c4f5231ed"
)
_RSA_EXPONENT = 0x10001


class QqLoginError(Exception):
    error_code = "QQ_LOGIN_FAILED"

    def __init__(self, message: str = "QQ login failed") -> None:
        super().__init__(message)


class QrCodeExpired(QqLoginError):
    error_code = "QR_EXPIRED"

    def __init__(self) -> None:
        super().__init__("QR code has expired")


class QrLoginTimeout(QqLoginError):
    error_code = "QR_LOGIN_TIMEOUT"

    def __init__(self) -> None:
        super().__init__("QR login timed out")


@dataclass(frozen=True, slots=True)
class _PasswordCheckResult:
    verifycode: str
    salt: str
    verifysession: str
    is_rand_salt: str
    ptdrvs: str
    sid: str
    need_captcha: bool


@dataclass(slots=True)
class LoginCredentials:
    """Secrets returned by Roco login; repr never exposes credential values."""

    uin: str
    angel_key: str = field(repr=False)
    pskey: str = field(repr=False)
    skey: str = field(repr=False)
    cookie_header: str = field(repr=False, default="")
    login_at: datetime = field(
        default_factory=lambda: datetime.now(_SHANGHAI),
    )


StatusCallback = Callable[[str], Awaitable[None]]


class QQLoginFlow:
    """One isolated cookie jar and OAuth flow for a single QR code."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        poll_interval: float = 2.0,
        timeout: float = 120.0,
        monotonic: Callable[[], float] = time.monotonic,
        timestamp_ms: Callable[[], int] | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=10.0),
            headers={"User-Agent": _USER_AGENT},
        )
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._monotonic = monotonic
        self._timestamp_ms = timestamp_ms or (lambda: int(time.time() * 1000))
        self._xlogin_url = ""
        self._qrsig = ""

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _xlogin_target(self) -> str:
        return str(
            httpx.URL(
                "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                params={
                    "appid": _QQ_APP_ID,
                    "daid": _QQ_DAID,
                    "style": "33",
                    "login_text": "%E7%99%BB%E5%BD%95",
                    "hide_title_bar": "1",
                    "hide_border": "1",
                    "target": "self",
                    "s_url": _OAUTH_LOGIN_JUMP_URL,
                    "pt_3rd_aid": _QQ_THIRD_PARTY_APP_ID,
                    "pt_feedback_link": (
                        "https://support.qq.com/products/77942?"
                        "customInfo=17roco.qq.com.appid102061779"
                    ),
                    "theme": "2",
                },
            )
        )

    async def create_qr_code(self) -> bytes:
        """Prepare QQ OAuth state and return the QR image bytes."""

        pre_authorize = await self._client.get(
            "https://graph.qq.com/oauth2.0/authorize",
            params={
                "response_type": "code",
                "client_id": _QQ_THIRD_PARTY_APP_ID,
                "redirect_uri": _REDIRECT_URI,
                "scope": "all",
            },
            headers={"Referer": "https://17roco.qq.com/"},
        )
        pre_redirect = _redirect_location(pre_authorize)
        pre_response = await self._client.get(
            pre_redirect,
            headers={"Referer": "https://17roco.qq.com/"},
        )
        _require_status(pre_response, 200, "QQ pre-login failed")

        self._xlogin_url = self._xlogin_target()
        xlogin = await self._client.get(self._xlogin_url)
        _require_status(xlogin, 200, "QQ xlogin initialization failed")

        qr_response = await self._client.get(
            "https://xui.ptlogin2.qq.com/ssl/ptqrshow",
            params={
                "appid": _QQ_APP_ID,
                "e": "2",
                "l": "M",
                "s": "3",
                "d": "72",
                "v": "4",
                "t": "0.34028376845074293",
                "daid": _QQ_DAID,
                "pt_3rd_aid": _QQ_THIRD_PARTY_APP_ID,
                "u1": "https://graph.qq.com/oauth2.0/login_jump",
            },
            headers={"Referer": self._xlogin_url},
        )
        _require_status(qr_response, 200, "could not fetch QQ QR code")
        self._qrsig = self._cookie_value("qrsig", "xui.ptlogin2.qq.com")
        if not self._qrsig:
            raise QqLoginError("QQ QR response did not contain qrsig")
        if not qr_response.content:
            raise QqLoginError("QQ QR response was empty")
        return qr_response.content

    async def wait_for_login(
        self,
        on_status: StatusCallback,
    ) -> LoginCredentials:
        """Poll the QR state, complete OAuth, and obtain Roco credentials."""

        if not self._qrsig or not self._xlogin_url:
            raise QqLoginError("QR code has not been created")
        started_at = self._monotonic()
        ptqr_token = calculate_ptqr_token(self._qrsig)
        last_status = ""

        while True:
            if self._monotonic() - started_at >= self._timeout:
                raise QrLoginTimeout()
            poll_response = await self._client.get(
                "https://xui.ptlogin2.qq.com/ssl/ptqrlogin",
                params={
                    "u1": "https://graph.qq.com/oauth2.0/login_jump",
                    "ptqrtoken": str(ptqr_token),
                    "ptredirect": "0",
                    "h": "1",
                    "t": "1",
                    "g": "1",
                    "from_ui": "1",
                    "ptlang": "2052",
                    "action": f"0-0-{self._timestamp_ms()}",
                    "js_ver": "25112611",
                    "js_type": "1",
                    "login_sig": self._cookie_value(
                        "pt_login_sig", "xui.ptlogin2.qq.com"
                    ),
                    "pt_uistyle": "40",
                    "aid": _QQ_APP_ID,
                    "daid": _QQ_DAID,
                    "pt_3rd_aid": _QQ_THIRD_PARTY_APP_ID,
                    "o1vId": self._cookie_value(
                        "pt_guid_sig", "xui.ptlogin2.qq.com"
                    )
                    or "eedc9b0ac9dfecebd48d118dac7ffd9e",
                    "pt_js_version": "42f2bcc1",
                },
                headers={
                    "Accept": "*/*",
                    "Referer": self._xlogin_url,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            _require_status(poll_response, 200, "QQ QR polling failed")
            result = parse_ptui_callback(poll_response.text)
            if result is None:
                raise QqLoginError("QQ returned an invalid QR status response")
            status, redirect_url, _message = result

            if status == "0":
                await on_status("CONFIRMED")
                if not redirect_url:
                    raise QqLoginError("QQ login succeeded without a redirect URL")
                return await self._complete_oauth(redirect_url)
            if status == "65":
                raise QrCodeExpired()
            if status in {"66", "67"}:
                public_status = "WAITING_SCAN" if status == "66" else "SCANNED"
                if public_status != last_status:
                    await on_status(public_status)
                    last_status = public_status
            elif status == "7":
                raise QqLoginError("QQ rejected the QR polling parameters")
            else:
                raise QqLoginError(f"unknown QQ QR status: {status}")
            await _sleep(self._poll_interval)

    async def _complete_oauth(self, check_signature_url: str) -> LoginCredentials:
        await self._collect_p_skey(check_signature_url)
        return await self._finish_roco_oauth()

    async def _collect_p_skey(self, entry_url: str) -> str:
        """Follow redirects from an OAuth entry point until p_skey is set."""

        url = entry_url
        response: httpx.Response | None = None
        for _ in range(8):
            response = await self._client.get(
                url,
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
            )
            p_skey = self._cookie_value("p_skey", "graph.qq.com")
            if p_skey:
                return p_skey
            location = response.headers.get("location")
            if not location:
                break
            url = str(response.url.join(location))
        if response is not None and response.status_code >= 400:
            raise QqLoginError(f"QQ login jump failed ({response.status_code})")
        raise QqLoginError("QQ OAuth did not provide p_skey")

    async def _finish_roco_oauth(self) -> LoginCredentials:
        """Exchange the QQ p_skey for the Roco angel credentials."""

        p_skey = self._cookie_value("p_skey", "graph.qq.com")
        if not p_skey:
            raise QqLoginError("QQ OAuth did not provide p_skey")
        show_url = "https://graph.qq.com/oauth2.0/show"
        show_params = {
            "which": "Login",
            "display": "pc",
            "response_type": "code",
            "client_id": _QQ_THIRD_PARTY_APP_ID,
            "redirect_uri": _REDIRECT_URI,
            "scope": "all",
        }
        show_response = await self._client.get(
            show_url,
            params=show_params,
            headers={"Referer": "https://17roco.qq.com/"},
        )
        _require_status(show_response, 200, "QQ authorization page failed")

        ui_value = self._cookie_value("ui", "graph.qq.com") or str(
            uuid.uuid4()
        ).upper()
        graph_cookie = self._cookie_header_for("graph.qq.com")
        await _sleep(0.1)
        authorize_response = await self._client.post(
            "https://graph.qq.com/oauth2.0/authorize",
            data={
                "response_type": "code",
                "client_id": _QQ_THIRD_PARTY_APP_ID,
                "redirect_uri": _REDIRECT_URI,
                "scope": "all",
                "state": "",
                "switch": "",
                "from_ptlogin": "1",
                "src": "1",
                "update_auth": "1",
                "openapi": "1010",
                "g_tk": str(calculate_gtk(p_skey)),
                "auth_time": str(self._timestamp_ms()),
                "ui": ui_value,
            },
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cache-Control": "max-age=0",
                "Origin": "https://graph.qq.com",
                "Referer": str(httpx.URL(show_url, params=show_params)),
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": "1",
                "Cookie": graph_cookie,
            },
        )
        roco_redirect = _redirect_location(authorize_response)
        if "show.html" in roco_redirect or "/show?" in roco_redirect:
            raise QqLoginError("QQ authorization was rejected")

        login_target = await self._client.get(
            roco_redirect,
            headers={"Referer": "https://graph.qq.com/"},
        )
        _require_status(login_target, 200, "Roco login target failed")
        code = _query_parameter(roco_redirect, "code")
        login_response = await self._client.get(
            "https://web2.17roco.qq.com/fcgi-bin/login3",
            params={"code": code, "platfrom_src": "2"},
            headers={"Referer": "https://17roco.qq.com/"},
        )
        _require_status(login_response, 200, "Roco login failed")
        login_text = login_response.content.decode("gb18030", errors="replace")
        alert_match = _ALERT_PATTERN.search(login_text)
        if alert_match is not None:
            raise QqLoginError(html.unescape(alert_match.group(1)))
        if "night" in login_text.lower():
            raise QqLoginError("Roco service is in night mode")

        values = dict(_LOGIN_VALUE_PATTERN.findall(login_text))
        uin = values.get("angel_uin", "")
        angel_key = values.get("angel_key", "")
        pskey = values.get("pskey", "")
        skey = values.get("skey", "")
        if _UIN_PATTERN.fullmatch(uin) is None:
            raise QqLoginError("Roco login did not return a valid UIN")
        if not angel_key or not pskey or not skey:
            raise QqLoginError("Roco login returned incomplete credentials")
        return LoginCredentials(
            uin=uin,
            angel_key=angel_key,
            pskey=pskey,
            skey=skey,
            cookie_header=self._cookie_header_for("17roco.qq.com"),
        )

    def _cookie_value(self, name: str, host: str) -> str:
        matches = [
            cookie
            for cookie in self._client.cookies.jar
            if cookie.name == name and _domain_matches(host, cookie.domain)
        ]
        if not matches:
            return ""
        return max(matches, key=lambda cookie: len(cookie.domain or "")).value

    def _cookie_header_for(self, host: str) -> str:
        selected: dict[str, object] = {}
        for cookie in self._client.cookies.jar:
            if not cookie.value or not _domain_matches(host, cookie.domain):
                continue
            existing = selected.get(cookie.name)
            if existing is None or len(cookie.domain or "") > len(
                getattr(existing, "domain", "") or ""
            ):
                selected[cookie.name] = cookie
        return "; ".join(
            f"{name}={getattr(cookie, 'value')}"
            for name, cookie in selected.items()
        )


class QQPasswordLoginFlow(QQLoginFlow):
    """Account/password login flow that ends with the same Roco credentials."""

    def __init__(
        self,
        account: str,
        password: str,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 120.0,
    ) -> None:
        if not _UIN_PATTERN.fullmatch(str(account)):
            raise QqLoginError("QQ account must be a valid UIN")
        super().__init__(client=client, timeout=timeout)
        self._account = str(account)
        self._password = password

    async def login(self) -> LoginCredentials:
        """Perform the ptlogin2 password flow and return Roco credentials."""

        self._xlogin_url = self._xlogin_target()
        xlogin = await self._client.get(self._xlogin_url)
        _require_status(xlogin, 200, "QQ xlogin initialization failed")
        login_sig = self._cookie_value("pt_login_sig", "xui.ptlogin2.qq.com")
        if not login_sig:
            raise QqLoginError("QQ xlogin did not provide pt_login_sig")

        check = await self._check_captcha(login_sig)
        if check.need_captcha:
            raise QqLoginError(
                "QQ 要求输入图形验证码，当前无法自动登录，请改用扫码模式"
            )

        redirect_url = await self._submit_password(login_sig, check)
        await self._collect_p_skey(redirect_url)
        return await self._finish_roco_oauth()

    async def _check_captcha(self, login_sig: str) -> _PasswordCheckResult:
        response = await self._client.get(
            "https://ssl.ptlogin2.qq.com/check",
            params={
                "regmaster": "",
                "pt_tea": _PT_TEA,
                "pt_vcode": _PT_VCODE,
                "uin": self._account,
                "appid": _QQ_APP_ID,
                "js_ver": _PTUI_VERSION,
                "js_type": "1",
                "login_sig": login_sig,
                "u1": _OAUTH_LOGIN_JUMP_URL,
                "r": f"0.{uuid.uuid4().hex[:16]}",
                "pt_uistyle": _PT_STYLE,
                "daid": _QQ_DAID,
                "pt_3rd_aid": _QQ_THIRD_PARTY_APP_ID,
                "o1vId": "",
                "pt_js_version": _JS_VERSION,
            },
            headers={"Referer": self._xlogin_url},
        )
        _require_status(response, 200, "QQ captcha check failed")
        raw = response.text.strip()
        logger.info(
            "password_login_check uin=%s callback=%s",
            self._account[-4:],
            raw[:200],
        )
        values = parse_ptui_raw(raw)
        if values is None:
            raise QqLoginError("QQ returned an invalid captcha check response")
        return _PasswordCheckResult(
            verifycode=values[1] if len(values) > 1 else "",
            salt=values[2] if len(values) > 2 else "",
            verifysession=values[3] if len(values) > 3 else "",
            is_rand_salt=values[4] if len(values) > 4 else "0",
            ptdrvs=values[5] if len(values) > 5 else "",
            sid=values[6] if len(values) > 6 else "",
            need_captcha=values[0] not in ("0", "2", "3"),
        )

    async def _submit_password(
        self,
        login_sig: str,
        check: _PasswordCheckResult,
    ) -> str:
        p = _encrypt_password(self._password, check.salt, check.verifycode)
        params = {
            "u": self._account,
            "verifycode": check.verifycode,
            "pt_vcode_v1": "0",
            "pt_verifysession_v1": check.verifysession,
            "p": p,
            "pt_randsalt": check.is_rand_salt or "0",
            "u1": _OAUTH_LOGIN_JUMP_URL,
            "ptredirect": "self",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": _PTUI_VERSION,
            "js_type": "1",
            "login_sig": login_sig,
            "pt_uistyle": _PT_STYLE,
            "aid": _QQ_APP_ID,
            "daid": _QQ_DAID,
            "pt_3rd_aid": _QQ_THIRD_PARTY_APP_ID,
            "ptdrvs": check.ptdrvs,
            "sid": check.sid,
            "o1vId": "",
            "pt_js_version": _JS_VERSION,
        }
        # The official js builds the URL by plain concatenation; keep the
        # base64 characters * - _ literal (httpx would percent-encode them).
        url = "https://ssl.ptlogin2.qq.com/login?" + urlencode(
            params, safe="*-_!."
        )
        response = await self._client.get(url, headers={"Referer": self._xlogin_url})
        if response.status_code == 302:
            # Some ptlogin2 responses redirect on success instead of a callback.
            location = response.headers.get("location")
            if location:
                return str(response.url.join(location))
        _require_status(response, 200, "QQ password login failed")
        values = parse_ptui_raw(response.text)
        if values is None:
            raise QqLoginError("QQ returned an invalid login response")
        status = values[0]
        redirect_url = values[2] if len(values) > 2 else ""
        message = values[4] if len(values) > 4 else ""
        if status != "0":
            raise QqLoginError(message or "QQ password login failed")
        if not redirect_url:
            raise QqLoginError("QQ password login succeeded without a redirect URL")
        return redirect_url


def _encrypt_password(password: str, salt: str, verifycode: str) -> str:
    """Return the pt_randsalt=0 password digest used by modern ptlogin2.

    Mirrors the official getEncryption from login_10.js / c_login_2.js:
      o     = md5(pwd)                          (UPPERCASE hex)
      t_key = md5( raw_bytes(md5(pwd)) + salt )
      data  = o + salt_hex + i_hex + vc_hex
      cipher= TEA_CBC_encrypt(data, t_key)
      p     = base64( RSA2048_PKCS1( r_hex + cipher ) )
    The official md5 emits uppercase hex; TEA is the Tencent CBC variant.
    """

    digest = _js_md5(password.encode("utf-8"))
    salt_bytes = salt.encode("latin1")
    t_key = _js_md5(bytes.fromhex(digest) + salt_bytes)
    vc_hex = verifycode.upper().encode("utf-8").hex()
    i_hex = format(len(vc_hex) // 2, "04x")
    salt_hex = salt_bytes.hex()
    data_hex = digest + salt_hex + i_hex + vc_hex
    tea_cipher = _tea_encrypt(bytes.fromhex(data_hex), bytes.fromhex(t_key))
    r_hex = format(len(tea_cipher) // 2, "04x")
    rsa_bytes = _rsa_pkcs1_v15(
        bytes.fromhex(r_hex + tea_cipher), _RSA_MODULUS_HEX
    )
    p = base64.b64encode(rsa_bytes).decode()
    return p.replace("/", "-").replace("+", "*").replace("=", "_")


def _js_md5(data: bytes) -> str:
    """The official ptlogin md5 wrapper emits UPPERCASE hex."""

    return hashlib.md5(data).hexdigest().upper()


def _tea_encrypt(data: bytes, key: bytes) -> str:
    """Faithful port of the Tencent TEA CBC encrypt from c_login_2.js."""

    def read_uint32(t: list[int], e: int, n: int) -> int:
        n = min(n, 4)
        value = 0
        for index in range(e, e + n):
            value = (value << 8) | t[index]
        return value & 0xFFFFFFFF

    def write_uint32(t: list[int], e: int, n: int) -> None:
        t[e + 3] = n & 0xFF
        t[e + 2] = (n >> 8) & 0xFF
        t[e + 1] = (n >> 16) & 0xFF
        t[e] = (n >> 24) & 0xFF

    k0, k1, k2, k3 = (
        int.from_bytes(key[i : i + 4], "big") for i in range(0, 16, 4)
    )
    mask = 0xFFFFFFFF

    def tea_round(block: list[int]) -> list[int]:
        n = read_uint32(block, 0, 4)
        o = read_uint32(block, 4, 4)
        u = 0
        for _ in range(16):
            # Official js: u is advanced BEFORE n; every ^ operand is int32.
            u = (u + 0x9E3779B9) & mask
            t1 = (((o << 4) & mask) + k0) & mask
            t2 = (o + u) & mask
            t3 = ((o >> 5) + k1) & mask
            n = (n + (t1 ^ t2 ^ t3)) & mask
            t4 = (((n << 4) & mask) + k2) & mask
            t5 = (n + u) & mask
            t6 = ((n >> 5) + k3) & mask
            o = (o + (t4 ^ t5 ^ t6)) & mask
        out = [0] * 8
        write_uint32(out, 0, n)
        write_uint32(out, 4, o)
        return out

    state = {"i": [0] * 8, "c": [0] * 8, "u": 0, "s": 0, "p": True, "a": 0}

    def encrypt_block() -> None:
        for t in range(8):
            state["i"][t] ^= (
                state["c"][t] if state["p"] else state["f"][state["s"] + t]
            )
        enc = tea_round(state["i"])
        for t in range(8):
            state["f"][state["u"] + t] = enc[t] ^ state["c"][t]
            state["c"][t] = state["i"][t]
        state["s"] = state["u"]
        state["u"] += 8
        state["a"] = 0
        state["p"] = False

    length = len(data)
    pad = (length + 10) % 8
    if pad != 0:
        pad = 8 - pad
    state["f"] = [0] * (length + pad + 10)
    state["i"][0] = (random.randint(0, 255) & 0xF8) | pad
    for index in range(1, pad + 1):
        state["i"][index] = random.randint(0, 255)
    state["a"] = pad + 1
    n = 1
    while n <= 2:
        if state["a"] < 8:
            state["i"][state["a"]] = random.randint(0, 255)
            state["a"] += 1
            n += 1
        # Official js: `a<8&&(fill),8==a&&v()` — block runs when a hits 8.
        if state["a"] == 8:
            encrypt_block()
    offset = 0
    remaining = length
    while remaining > 0:
        if state["a"] < 8:
            state["i"][state["a"]] = data[offset]
            offset += 1
            remaining -= 1
            state["a"] += 1
        if state["a"] == 8:
            encrypt_block()
    n = 1
    while n <= 7:
        if state["a"] < 8:
            state["i"][state["a"]] = 0
            state["a"] += 1
            n += 1
        if state["a"] == 8:
            encrypt_block()
    return bytes(state["f"]).hex()


def _rsa_pkcs1_v15(data: bytes, modulus_hex: str, exponent: int = _RSA_EXPONENT) -> bytes:
    n = int(modulus_hex, 16)
    key_bytes = (n.bit_length() + 7) // 8
    if len(data) > key_bytes - 11:
        raise ValueError("message too long for RSA")
    ps_len = key_bytes - 3 - len(data)
    ps = b""
    while len(ps) < ps_len:
        chunk = os.urandom(ps_len - len(ps))
        ps += bytes(b for b in chunk if b != 0)
    em = b"\x00\x02" + ps + b"\x00" + data
    message = int.from_bytes(em, "big")
    return pow(message, exponent, n).to_bytes(key_bytes, "big")


def calculate_ptqr_token(qrsig: str) -> int:
    value = 0
    for character in qrsig:
        value = (value * 33 + ord(character)) & 0x7FFF_FFFF
    return value


def calculate_gtk(p_skey: str) -> int:
    value = 5381
    for character in p_skey:
        value += (value << 5) + ord(character)
    return value & 0x7FFF_FFFF


def parse_ptui_raw(body: str) -> tuple[str, ...] | None:
    """Return the raw ptuiCB / ptui_checkVC callback arguments as strings."""

    match = re.search(r"(?:ptuiCB|ptui_checkVC)\((.+)\)", body)
    if match is None:
        return None
    try:
        values = ast.literal_eval(f"({match.group(1)})")
    except (SyntaxError, ValueError):
        return None
    if not isinstance(values, tuple) or not values:
        return None
    return tuple(
        str(value) if value is not None else ""
        for value in values
    )


def parse_ptui_callback(body: str) -> tuple[str, str | None, str] | None:
    values = parse_ptui_raw(body)
    if values is None:
        return None
    status = values[0]
    redirect = values[2] if len(values) > 2 else ""
    message = values[4] if len(values) > 4 else ""
    return status, redirect or None, message


def _domain_matches(host: str, cookie_domain: str | None) -> bool:
    domain = (cookie_domain or "").lstrip(".")
    return bool(domain) and (host == domain or host.endswith(f".{domain}"))


def _redirect_location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    if not location:
        raise QqLoginError(f"expected redirect from {response.url.host}")
    return str(response.url.join(location))


def _require_status(response: httpx.Response, status: int, message: str) -> None:
    if response.status_code != status:
        raise QqLoginError(f"{message} ({response.status_code})")


def _query_parameter(url: str, name: str) -> str:
    values = parse_qs(urlparse(url).query).get(name, [])
    if not values or not values[0]:
        raise QqLoginError(f"redirect URL did not contain {name}")
    return values[0]


async def _sleep(seconds: float) -> None:
    # Kept as a seam so protocol tests can use a zero interval.
    import asyncio

    await asyncio.sleep(seconds)
