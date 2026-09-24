"""武汉科技大学校园网守护：Windows 图形界面与后台监控。"""

from __future__ import annotations

from datetime import datetime
from dataclasses import replace
import logging
from logging.handlers import RotatingFileHandler
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.error

from keeper_core import (
    DATA_DIR, Config, autostart_enabled, detect_nas_id, discover_nas_id,
    get_portal_status, internet_works, is_campus_client, load_config,
    parse_nas_id, save_config, set_autostart, submit_login,
)


POLL_SECONDS = 20
BACKOFF_SECONDS = (30, 60, 120, 300)


def build_logger() -> logging.Logger:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wifi_keeper")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(DATA_DIR / "keeper.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger


class KeeperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("武科大校园网自动认证助手")
        self.root.geometry("520x440")
        self.root.minsize(480, 420)
        self.logger = build_logger()
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.check_now = threading.Event()
        self.manual_check = threading.Event()
        self.config: Config | None = None
        self.worker: threading.Thread | None = None

        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.nas_var = tk.StringVar()
        self.autostart_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请填写配置并保存")
        self._build_ui()
        self._load_existing()
        self.root.after(150, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        for row, (label, variable, show) in enumerate((
            ("学号", self.user_var, None),
            ("认证密码", self.password_var, "•"),
            ("网关 ID 或页面地址", self.nas_var, None),
        )):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=variable, show=show or "").grid(
                row=row, column=1, sticky="ew", padx=(12, 0), pady=5,
            )
        ttk.Button(frame, text="自动获取", command=self._detect_nas).grid(
            row=2, column=2, padx=(8, 0), pady=5,
        )
        ttk.Checkbutton(frame, text="登录 Windows 后自动启动", variable=self.autostart_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 2),
        )
        ttk.Label(frame, text="认证服务器使用 HTTP；请只在可信的校园网络上使用。",
                  foreground="#975500").grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="保存并开始监控", command=self._save).pack(side="left")
        ttk.Button(buttons, text="立即检测并尝试认证", command=self._check).pack(side="left", padx=8)
        ttk.Label(frame, textvariable=self.status_var, wraplength=470).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(14, 8),
        )
        self.log_box = tk.Text(frame, height=10, state="disabled", wrap="word")
        self.log_box.grid(row=7, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(7, weight=1)
        ttk.Label(frame, text=f"日志与配置：{DATA_DIR}", foreground="#666666").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(8, 0),
        )

    def _load_existing(self) -> None:
        try:
            config = load_config()
            if config:
                self.user_var.set(config.username)
                self.password_var.set(config.password)
                self.nas_var.set(config.nas_id)
                self.autostart_var.set(autostart_enabled())
                self.config = config
                self._start_worker()
                self._emit("已加载配置，开始监控")
        except Exception as exc:
            self._emit(f"配置读取失败，请重新填写：{type(exc).__name__}")

    def _save(self) -> None:
        values = tuple(v.get().strip() for v in (
            self.user_var, self.password_var, self.nas_var,
        ))
        if not all(values):
            messagebox.showwarning("配置不完整", "请填写学号、密码和网关 ID。")
            return
        nas_id = parse_nas_id(values[2])
        if nas_id is None:
            messagebox.showwarning("网关 ID 无效", "请输入 nasId 数字，或粘贴浏览器中带 nasId= 的认证页面地址。")
            return
        self.nas_var.set(nas_id)
        config = Config(*values[:2], nas_id, autostart=self.autostart_var.get())
        try:
            save_config(config)
            set_autostart(config.autostart)
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存设置：{type(exc).__name__}: {exc}")
            return
        self.config = config
        self._start_worker()
        self.check_now.set()
        self._emit("配置已保存，正在检查网络")

    def _start_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._monitor, daemon=True)
        self.worker.start()

    def _check(self) -> None:
        if not self.config:
            messagebox.showinfo("请先保存", "请先填写配置并点击“保存并开始监控”。")
            return
        self.manual_check.set()
        self.check_now.set()
        self._emit("正在检测；若门户显示未认证，将立即提交保存的学号和密码")

    def _detect_nas(self) -> None:
        self._emit("正在读取当前校园网的网关 ID")

        def detect() -> None:
            nas_id, reason = detect_nas_id()
            if nas_id:
                self.events.put(("nas", nas_id))
            else:
                self._emit(f"自动获取失败：{reason}。可粘贴浏览器中的认证页地址后保存")

        threading.Thread(target=detect, daemon=True).start()

    def _emit(self, message: str) -> None:
        self.events.put(("status", message))
        self.logger.info(message)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "nas":
                    self.nas_var.set(message)
                    message = f"已获取当前网关 ID：{message}；请保存配置"
                self.status_var.set(message)
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"[{stamp}] {message}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_events)

    def _monitor(self) -> None:
        offline_count = 0
        auth_failures = 0
        next_auth_at = 0.0
        auth_error: str | None = None
        credential_verified = False
        last_config: Config | None = None
        while not self.stop_event.is_set():
            forced = self.manual_check.is_set()
            self.manual_check.clear()
            self.check_now.clear()
            config = self.config
            if config is None:
                break
            if config != last_config:
                offline_count = auth_failures = 0
                next_auth_at = 0.0
                auth_error = None
                credential_verified = False
                last_config = config
            try:
                portal = get_portal_status()
                if portal.kind == "online":
                    offline_count = auth_failures = 0
                    if auth_error:
                        self._emit(f"门户显示在线，但{auth_error}")
                    elif portal.username != config.username:
                        self._emit("门户显示其他账号在线；所填账号和密码未通过验证")
                    elif not internet_works():
                        self._emit("门户显示当前账号在线，但外网检测失败；不会重复提交登录")
                    elif credential_verified:
                        self._emit("门户确认当前账号在线，外网正常")
                    else:
                        next_auth_at = 0
                        self._emit("门户显示当前账号在线；所填密码尚未重新验证")
                elif portal.kind == "offline":
                    offline_count += 1
                    if offline_count < 2 and not forced:
                        self._emit("门户显示未认证，等待再次确认")
                    elif time.monotonic() < next_auth_at and not forced:
                        self._emit("门户仍显示未认证，等待下次重试")
                    else:
                        detected_nas = discover_nas_id()
                        login_config = replace(config, nas_id=detected_nas) if detected_nas else config
                        if not is_campus_client(login_config.nas_id):
                            next_auth_at = time.monotonic() + 60
                            self._emit("门户未确认当前设备处于校园内网，跳过登录")
                            self.check_now.wait(POLL_SECONDS)
                            continue
                        self._emit("门户确认未认证，正在提交登录")
                        result = submit_login(login_config)
                        if result.kind == "accepted":
                            # 业务 code=0 后再次核对门户状态和外网。
                            if self.stop_event.wait(3):
                                break
                            after = get_portal_status()
                            if after.kind == "online" and after.username == config.username:
                                offline_count = auth_failures = 0
                                next_auth_at = 0
                                auth_error = None
                                credential_verified = True
                                if internet_works():
                                    self._emit("门户确认认证成功，外网已恢复")
                                else:
                                    self._emit("门户确认认证成功，但外网仍不通；请检查运营商或学校出口")
                            else:
                                auth_failures += 1
                                next_auth_at = time.monotonic() + BACKOFF_SECONDS[
                                    min(auth_failures - 1, len(BACKOFF_SECONDS) - 1)
                                ]
                                self._emit("登录接口返回成功，但门户未确认当前账号在线；稍后复查")
                        elif result.kind == "rejected":
                            auth_error = "上次认证被服务器拒绝；请检查学号和密码"
                            next_auth_at = float("inf")
                            self._emit(f"{auth_error}。已暂停自动重试")
                        elif result.kind == "captcha":
                            auth_error = "上次认证需要验证码；请在网页登录"
                            next_auth_at = float("inf")
                            self._emit(f"{auth_error}。已暂停自动重试")
                        elif result.kind == "account_mismatch":
                            auth_error = "服务器返回的在线账号与所填学号不一致"
                            next_auth_at = float("inf")
                            self._emit(f"{auth_error}。未将其判定为认证成功")
                        else:
                            auth_failures += 1
                            next_auth_at = time.monotonic() + BACKOFF_SECONDS[
                                min(auth_failures - 1, len(BACKOFF_SECONDS) - 1)
                            ]
                            if result.kind == "http_error":
                                self._emit(f"认证请求失败（HTTP {result.http_status}），稍后重试")
                            else:
                                self._emit("认证服务器返回未知结果，未判定为成功；稍后重试")
                else:
                    offline_count = 0
                    self._emit("无法判断校园网认证状态，本次不提交账号密码")
            except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                auth_failures += 1
                next_auth_at = time.monotonic() + BACKOFF_SECONDS[
                    min(auth_failures - 1, len(BACKOFF_SECONDS) - 1)
                ]
                self._emit(f"网络检查或认证请求失败：{type(exc).__name__}，稍后重试")

            self.check_now.wait(POLL_SECONDS)

    def _close(self) -> None:
        self.stop_event.set()
        self.check_now.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    KeeperApp(root)
    root.mainloop()
