"""武汉科技大学校园网认证的 Windows 后台逻辑。仅使用 Python 标准库。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


APP_NAME = "WUSTWifiKeeper"
PORTAL_URL = "http://59.68.177.9/"
PORTAL_CONFIG_URL = "http://59.68.177.9/api/config"
PORTAL_STATUS_URL = "http://59.68.177.9/api/account/status"
LOGIN_URL = "http://59.68.177.9/api/account/login"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
PROBES = (
    ("http://www.msftconnecttest.com/connecttest.txt", 200, b"Microsoft Connect Test"),
    ("http://connectivitycheck.gstatic.com/generate_204", 204, b""),
)
_CAMPUS_PRIVATE_NETS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_campus_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    nas_id: str
    autostart: bool = False


@dataclass(frozen=True)
class LoginResult:
    kind: str  # accepted / rejected / captcha / account_mismatch / unknown / http_error
    http_status: int


@dataclass(frozen=True)
class PortalStatus:
    kind: str  # online / offline / unknown
    username: str | None = None
    dial_code: str | None = None


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("密码加密仅支持 Windows")
    source = ctypes.create_string_buffer(data)
    input_blob = DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, 0,
            ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob), APP_NAME, None, None, None, 0,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def load_config() -> Config | None:
    if not CONFIG_PATH.exists():
        return None
    import base64

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    password = _dpapi(base64.b64decode(data["password_dpapi"], validate=True), True)
    return Config(
        username=data["username"], password=password.decode("utf-8"),
        nas_id=data["nas_id"],
        autostart=bool(data.get("autostart", False)),
    )


def save_config(config: Config) -> None:
    import base64

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "username": config.username,
        "password_dpapi": base64.b64encode(_dpapi(config.password.encode("utf-8"), False)).decode("ascii"),
        "nas_id": config.nas_id,
        "autostart": config.autostart,
    }
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def internet_works() -> bool:
    """任一独立探针通过即认为联网，避免单一检测站点故障造成误报。"""
    for url, expected_status, expected_body in PROBES:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "WUSTWifiKeeper/1.0"})
            with urllib.request.urlopen(request, timeout=4) as response:
                if response.status == expected_status and (
                    not expected_body or response.read(128).strip() == expected_body
                ):
                    return True
        except (OSError, urllib.error.URLError, TimeoutError):
            continue
    return False


def _campus_open(request: str | urllib.request.Request, timeout: int):
    """校园内网地址直连，避免系统代理劫走认证请求。"""
    return _campus_opener.open(request, timeout=timeout)


def _extract_nas_id(text: str) -> str | None:
    text = html.unescape(urllib.parse.unquote(text))
    match = re.search(r"(?i)nasId[\"']?\s*[:=]\s*[\"']?(\d{1,4})\b", text)
    return match.group(1) if match else None


def parse_nas_id(value: str) -> str | None:
    """支持直接输入数字，或粘贴带 nasId 的认证页地址。"""
    value = value.strip()
    return value if value.isdecimal() and 1 <= len(value) <= 4 else _extract_nas_id(value)


def detect_nas_id() -> tuple[str | None, str]:
    """按门户自身的流程读取 /api/config，再回退到页面解析。"""
    api_reason = "配置接口没有返回 default_nas"
    try:
        with _campus_open(PORTAL_CONFIG_URL, timeout=6) as response:
            if urllib.parse.urlparse(response.geturl()).hostname == "59.68.177.9":
                payload = json.loads(response.read(131_072))
                nas_value = payload.get("data", {}).get("config", {}).get("default_nas")
                nas_id = parse_nas_id(str(nas_value)) if nas_value is not None else None
                if nas_id:
                    return nas_id, ""
            else:
                api_reason = "配置接口跳转到了其他站点"
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
        api_reason = f"配置接口读取失败（{type(exc).__name__}）"

    try:
        with _campus_open(PORTAL_URL, timeout=6) as response:
            url = urllib.parse.urlparse(response.geturl())
            if url.hostname != "59.68.177.9":
                return None, f"{api_reason}；门户跳转到了其他站点"
            nas_id = _extract_nas_id(response.geturl())
            if nas_id:
                return nas_id, ""
            # 有些网络由页面脚本跳转，geturl() 仍是根地址。
            body = response.read(131_072).decode("utf-8", errors="ignore")
            nas_id = _extract_nas_id(body)
            return (nas_id, "") if nas_id else (None, f"{api_reason}；门户页面没有提供 nasId")
    except urllib.error.HTTPError as exc:
        return None, f"{api_reason}；门户返回 HTTP {exc.code}"
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return None, f"{api_reason}；无法连接校园网门户（{type(exc).__name__}）"


def discover_nas_id() -> str | None:
    return detect_nas_id()[0]


def get_portal_status() -> PortalStatus:
    """按学校登录页使用的 /api/account/status 读取当前设备认证状态。"""
    with _campus_open(PORTAL_STATUS_URL, timeout=6) as response:
        if response.status != 200:
            return PortalStatus("unknown")
        result = json.loads(response.read(131_072))
    if not isinstance(result, dict):
        return PortalStatus("unknown")
    code = result.get("code")
    if code == 0:
        online = result.get("online")
        username = online.get("Username") if isinstance(online, dict) else None
        if not username:
            return PortalStatus("unknown")
        return PortalStatus("online", str(username).strip(), str(result.get("dialCode", "")))
    if code == 1:
        return PortalStatus("offline")
    return PortalStatus("unknown")


def is_campus_client(nas_id: str) -> bool:
    """认证前确认门户将本机识别为校园内网客户端。"""
    if not parse_nas_id(nas_id) == nas_id:
        return False
    with _campus_open(f"http://59.68.177.9/api/r/{nas_id}", timeout=6) as response:
        url = urllib.parse.urlparse(response.geturl())
    if url.hostname != "59.68.177.9" or not url.path.startswith("/tpl/"):
        return False
    query = urllib.parse.parse_qs(url.query)
    if query.get("nasId", [None])[0] != nas_id:
        return False
    client_ip = query.get("ip", [None])[0] or query.get("wlanuserip", [None])[0]
    try:
        address = ipaddress.ip_address(client_ip)
        return isinstance(address, ipaddress.IPv4Address) and any(
            address in network for network in _CAMPUS_PRIVATE_NETS
        )
    except ValueError:
        return False


def submit_login(config: Config) -> LoginResult:
    """按学校页面的业务 code 判断认证结果；HTTP 200 本身不代表成功。"""
    payload = urllib.parse.urlencode({
        "username": config.username,
        "password": config.password,
        "nasId": config.nas_id,
    }).encode("utf-8")
    request = urllib.request.Request(
        LOGIN_URL, data=payload, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    try:
        with _campus_open(request, timeout=6) as response:
            body = response.read(4096)
            if response.status != 200:
                return LoginResult("http_error", response.status)
            try:
                result = json.loads(body)
                code = result.get("code") if isinstance(result, dict) else None
            except (ValueError, UnicodeError):
                result = None
                code = None
            if code == 0:
                online = result.get("online") if isinstance(result, dict) else None
                online_user = online.get("Username") if isinstance(online, dict) else None
                if online_user is None:
                    return LoginResult("unknown", response.status)
                if str(online_user).strip() != config.username:
                    return LoginResult("account_mismatch", response.status)
            kind = {0: "accepted", 1: "rejected", 2: "captcha"}.get(code, "unknown")
            return LoginResult(kind, response.status)
    except urllib.error.HTTPError as exc:
        return LoginResult("http_error", exc.code)


def set_autostart(enabled: bool) -> None:
    """当前用户开机登录时启动，无需管理员权限。"""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            interpreter = Path(sys.executable)
            if interpreter.name.lower() in ("python.exe", "pythonw.exe"):
                interpreter = interpreter.with_name("pythonw.exe")
                command = subprocess.list2cmdline([str(interpreter), str(Path(__file__).with_name("wifi_keeper.pyw"))])
            else:
                command = subprocess.list2cmdline([str(interpreter)])
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
