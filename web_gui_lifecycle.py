"""
Malody BPM Web GUI 生命周期与安全退出子模块 (web_gui_lifecycle.py)

职责：
1. 无命令行窗口模式 (--noconsole / pythonw) 标准输出/错误空流防崩溃兜底。
2. 主界面顶栏右上角「退出程序」按钮与深色确认模态框无侵入注入。
3. 浏览器原生 beforeunload 离开提醒联动（运行期拦截误关，确认退出或后端超时后自动解除）。
4. 启动后严格满 3 小时 (10800 秒) 自动清理临时文件并强制终止自身进程 (os._exit)。
5. 单实例守护 (SingleInstanceGuard)：基于系统级文件锁保证同一时刻最多一个进程，
   新实例启动时按锁记录中的 PID 强杀老实例（与老实例监听哪个端口无关）。
6. 标签页关闭即退出：页面 pagehide 时 sendBeacon 通知后端，2.5 秒宽限期内无新页面加载
   （刷新会重新加载并打招呼）则退出进程；任一标签页关闭即触发。
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

DEFAULT_AUTO_EXIT_SECONDS: float = 3 * 3600.0  # 严格 3 小时 (10800 秒)
ENV_AUTO_EXIT_SECONDS: str = "MALODY_BPM_AUTO_EXIT_SECONDS"
# 标签页关闭后的宽限期：期间若有新页面加载（刷新）则不退出
DEFAULT_TAB_LEAVE_GRACE_SECONDS: float = 2.5

_DEVNULL_HANDLES: list[Any] = []


def ensure_windowless_stdio() -> None:
    """在无命令行窗口打包环境下，若 sys.stdout / sys.stderr 为 None，则重定向至 os.devnull。"""
    if sys.stdout is None:
        f_out = open(os.devnull, "w", encoding="utf-8", errors="ignore")
        _DEVNULL_HANDLES.append(f_out)
        sys.stdout = f_out
    if sys.stderr is None:
        f_err = open(os.devnull, "w", encoding="utf-8", errors="ignore")
        _DEVNULL_HANDLES.append(f_err)
        sys.stderr = f_err


def _resolve_auto_exit_seconds(explicit: float | None = None) -> float:
    if explicit is not None:
        val = float(explicit)
        if math.isfinite(val) and val > 0.0:
            return val
        return DEFAULT_AUTO_EXIT_SECONDS

    raw = os.environ.get(ENV_AUTO_EXIT_SECONDS, "").strip()
    if raw:
        try:
            parsed = float(raw)
            if math.isfinite(parsed) and parsed > 0.0:
                return parsed
        except ValueError:
            pass
    return DEFAULT_AUTO_EXIT_SECONDS


def _is_loopback_remote(remote_addr: str | None) -> bool:
    if not remote_addr:
        return False
    host = remote_addr.strip()
    if host.lower() == "localhost":
        return True
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip_obj.is_loopback:
        return True
    ipv4_mapped = getattr(ip_obj, "ipv4_mapped", None)
    if ipv4_mapped is not None and ipv4_mapped.is_loopback:
        return True
    return False


class LifecycleManager:
    """管理进程级 3 小时硬性超时守护、手动安全退出、临时文件清理与防跨站退出令牌。"""

    def __init__(
        self,
        timeout_seconds: float | None = None,
        hard_exit_fn: Callable[[int], None] = os._exit,
        started_monotonic: float | None = None,
    ) -> None:
        self.timeout_seconds: float = _resolve_auto_exit_seconds(timeout_seconds)
        self.started_monotonic: float = (
            time.monotonic() if started_monotonic is None else float(started_monotonic)
        )
        self.shutdown_token: str = secrets.token_urlsafe(24)
        self.instance_id: str = secrets.token_hex(8)
        self._hard_exit_fn: Callable[[int], None] = hard_exit_fn

        self._lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._cleanup_callbacks: list[Callable[[], Any]] = []
        self._watchdog_timer: threading.Timer | None = None
        self._shutting_down: bool = False
        self._shutdown_reason: str | None = None
        # 标签页关闭宽限计时器：收到页面「离开」信号后启动，期间有新页面加载 (hello) 即视为刷新而取消
        self._leave_timer: threading.Timer | None = None
        # 最近打招呼的页面：{页面加载随机 ID: monotonic 时间}，用于处理 hello 先于 beacon 到达的刷新竞态
        self._page_hellos: dict[str, float] = {}

    @property
    def shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down

    @property
    def shutdown_reason(self) -> str | None:
        with self._lock:
            return self._shutdown_reason

    def remaining_ms(self, now: float | None = None) -> int:
        with self._lock:
            if self._shutting_down:
                return 0
        cur = time.monotonic() if now is None else float(now)
        rem_sec = max(0.0, self.timeout_seconds - (cur - self.started_monotonic))
        return int(round(rem_sec * 1000.0))

    def bind_runtime(
        self,
        stop_event: threading.Event | None = None,
        cleanup_fn: Callable[[], Any] | None = None,
    ) -> None:
        with self._lock:
            if stop_event is not None:
                self._stop_event = stop_event
            if cleanup_fn is not None and cleanup_fn not in self._cleanup_callbacks:
                self._cleanup_callbacks.append(cleanup_fn)

    def register_cleanup(self, cleanup_fn: Callable[[], Any] | None) -> None:
        if cleanup_fn is None:
            return
        with self._lock:
            if cleanup_fn not in self._cleanup_callbacks:
                self._cleanup_callbacks.append(cleanup_fn)

    def start_watchdog(self) -> None:
        """启动进程级硬性超时自动退出守护定时器（幂等）。"""
        with self._lock:
            if self._shutting_down:
                return
            if self._watchdog_timer is not None and self._watchdog_timer.is_alive():
                return
            delay = max(0.0, self.timeout_seconds - (time.monotonic() - self.started_monotonic))
            timer = threading.Timer(delay, self._on_watchdog_expired)
            timer.daemon = True
            timer.name = "MalodyBPMAutoExitWatchdog"
            self._watchdog_timer = timer
            timer.start()

    def cancel_watchdog(self) -> None:
        """仅在非强杀退出阶段取消睡眠中的定时器（供受控停止服务器或单元测试收尾使用）。"""
        with self._lock:
            if self._shutting_down:
                return
            timer = self._watchdog_timer
            self._watchdog_timer = None
        if timer is not None:
            timer.cancel()

    def _on_watchdog_expired(self) -> None:
        self.trigger_shutdown(reason="auto_timeout", exit_delay_sec=0.05)

    def schedule_leave_shutdown(
        self,
        page_id: str = "",
        grace_sec: float = DEFAULT_TAB_LEAVE_GRACE_SECONDS,
    ) -> bool:
        """
        收到浏览器标签页「离开」信号 (pagehide beacon) 后调用：启动宽限计时，到期即退出进程。
        刷新同样会触发 pagehide，因此判定为刷新的两种情形都不会退出：
          1) 宽限期内有「其他页面」打招呼 (note_page_hello) —— 取消计时；
          2) 刷新时新页面的 hello 可能先于旧页面的 beacon 到达，故若在本信号之前 grace_sec 内
             已有「其他页面」打过招呼，则直接视为刷新，不启动计时。
        page_id 为发出离开信号的页面在每次加载时生成的随机 ID（自身的 hello 不算刷新）。
        重复调用会以最新一次信号为准重新计时。返回是否启动了计时。
        """
        grace = max(0.0, float(grace_sec))
        now = time.monotonic()
        with self._lock:
            if self._shutting_down:
                return False
            # 清理过期的打招呼记录，防止字典无限增长
            self._page_hellos = {
                pid: ts for pid, ts in self._page_hellos.items() if now - ts <= max(grace, 1.0) * 4
            }
            if any(pid != page_id and now - ts <= grace for pid, ts in self._page_hellos.items()):
                return False
            old = self._leave_timer
            timer = threading.Timer(grace, self._on_leave_expired)
            timer.daemon = True
            timer.name = "MalodyBPMTabLeaveGrace"
            self._leave_timer = timer
        if old is not None:
            old.cancel()
        timer.start()
        return True

    def note_page_hello(self, page_id: str = "") -> bool:
        """页面加载/从往返缓存恢复时调用：记录打招呼时间，并取消待执行的标签页关闭退出（视为刷新）。"""
        with self._lock:
            self._page_hellos[str(page_id)] = time.monotonic()
        return self.cancel_leave_shutdown()

    def cancel_leave_shutdown(self) -> bool:
        """取消待执行的标签页关闭退出。返回是否确实取消了一个计时。"""
        with self._lock:
            timer = self._leave_timer
            self._leave_timer = None
        if timer is not None:
            timer.cancel()
            return True
        return False

    def _on_leave_expired(self) -> None:
        with self._lock:
            # 计时器已被取消/替换时不执行（防止 cancel 与到期回调竞态）
            if self._leave_timer is None or threading.current_thread() is not self._leave_timer:
                return
            self._leave_timer = None
        self.trigger_shutdown(reason="tab_closed", exit_delay_sec=0.05)

    def trigger_shutdown(
        self,
        reason: str = "manual_button",
        exit_delay_sec: float = 0.35,
    ) -> bool:
        """执行清理回调、唤醒主循环停止事件，并在宽限延迟后强制终止自身进程。"""
        with self._lock:
            if self._shutting_down:
                return False
            self._shutting_down = True
            self._shutdown_reason = reason
            timer = self._watchdog_timer
            self._watchdog_timer = None
            # 进入退出流程后，一并作废待执行的标签页关闭计时
            leave_timer = self._leave_timer
            self._leave_timer = None
            callbacks = list(self._cleanup_callbacks)
            stop_ev = self._stop_event

        if timer is not None:
            timer.cancel()
        if leave_timer is not None:
            leave_timer.cancel()

        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

        if stop_ev is not None:
            try:
                stop_ev.set()
            except Exception:
                pass

        delay = max(0.0, float(exit_delay_sec))

        def _force_kill_worker() -> None:
            if delay > 0.0:
                time.sleep(delay)
            for cb in callbacks:
                try:
                    cb()
                except Exception:
                    pass
            self._hard_exit_fn(0)

        killer = threading.Thread(
            target=_force_kill_worker,
            name="MalodyBPMForceExit",
            daemon=True,
        )
        killer.start()
        return True


_DEFAULT_MANAGER = LifecycleManager()


def get_default_lifecycle_manager() -> LifecycleManager:
    return _DEFAULT_MANAGER


def bind_lifecycle_runtime(
    stop_event: threading.Event | None = None,
    cleanup_fn: Callable[[], Any] | None = None,
) -> LifecycleManager:
    _DEFAULT_MANAGER.bind_runtime(stop_event=stop_event, cleanup_fn=cleanup_fn)
    return _DEFAULT_MANAGER


def start_lifecycle_watchdog() -> LifecycleManager:
    _DEFAULT_MANAGER.start_watchdog()
    return _DEFAULT_MANAGER


def cancel_lifecycle_watchdog() -> None:
    _DEFAULT_MANAGER.cancel_watchdog()
    # 受控停止服务时一并取消标签页关闭计时，避免停止后仍被计时器强制退出进程
    _DEFAULT_MANAGER.cancel_leave_shutdown()


# ---------------------------------------------------------------------------
# 单实例守护 (SingleInstanceGuard)
# ---------------------------------------------------------------------------
# 设计说明：
# - instance.lock：仅用于持有系统级排他文件锁（Windows: msvcrt.locking；POSIX: fcntl.flock）。
#   进程无论正常退出还是被强杀，操作系统都会自动释放该锁，因此「能抢到锁 = 不存在存活的老实例」，
#   不会出现按过期 PID 误杀无关进程的问题。
# - instance.json：记录当前持锁实例的 pid / port / url 等信息。之所以与锁文件分开，
#   是因为 Windows 下被 msvcrt 锁定的字节区域无法被其他进程读取。
# - 目录：Windows 为 %LOCALAPPDATA%\MalodyBPM，macOS 为 ~/Library/Application Support/MalodyBPM，
#   其他 POSIX 为 $XDG_STATE_HOME(或 ~/.local/state)/MalodyBPM；均不可写时回退到系统临时目录。

_SINGLE_INSTANCE_DIR_NAME: str = "MalodyBPM"
_SINGLE_INSTANCE_LOCK_NAME: str = "instance.lock"
_SINGLE_INSTANCE_INFO_NAME: str = "instance.json"


def _resolve_instance_dir_candidates() -> list[str]:
    """按优先级返回单实例锁目录候选列表（首选系统标准用户数据目录，末位兜底为临时目录）。"""
    if sys.platform == "win32":
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state"
        )

    import tempfile

    return [
        os.path.join(base, _SINGLE_INSTANCE_DIR_NAME),
        os.path.join(tempfile.gettempdir(), _SINGLE_INSTANCE_DIR_NAME),
    ]


def _force_kill_pid(pid: int) -> bool:
    """强杀指定 PID（Windows 连同子进程树一起结束）。返回是否成功发出终止指令。"""
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True,
                timeout=3.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode == 0
        os.kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except Exception:
        return False


class SingleInstanceGuard:
    """保证本程序同一时刻最多只有一个进程：抢锁失败时强杀锁记录中的老实例后重试。"""

    def __init__(self, instance_dir: str | None = None) -> None:
        # instance_dir 仅供调试/测试显式指定；正常运行时按候选目录自动选择
        self._explicit_dir = instance_dir
        self.instance_dir: str | None = None
        self._lock_fh: Any = None
        self._held: bool = False
        # 抢锁最终失败时，记录仍存活老实例的信息（供调用方打开其地址）
        self.blocking_info: dict[str, Any] = {}
        # 本次启动是否强杀过老实例
        self.killed_pid: int | None = None

    # -- 路径与文件 ---------------------------------------------------------

    @property
    def info_path(self) -> str:
        return os.path.join(self.instance_dir or "", _SINGLE_INSTANCE_INFO_NAME)

    def _open_lock_file(self) -> bool:
        """在首个可写的候选目录中打开（必要时创建）锁文件，成功返回 True。"""
        candidates = [self._explicit_dir] if self._explicit_dir else _resolve_instance_dir_candidates()
        for cand in candidates:
            try:
                os.makedirs(cand, exist_ok=True)
                # "a+b"：不截断、不存在则创建；Python 默认以共享读写方式打开，多进程可同时打开
                self._lock_fh = open(os.path.join(cand, _SINGLE_INSTANCE_LOCK_NAME), "a+b")
                self.instance_dir = cand
                return True
            except OSError:
                continue
        return False

    def _try_lock(self) -> bool:
        """非阻塞尝试获取排他锁。"""
        fh = self._lock_fh
        if fh is None:
            return False
        try:
            if sys.platform == "win32":
                import msvcrt

                # 锁定第 0 字节（Windows 允许锁定超出文件末尾的区域，空文件也可锁）
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def read_info(self) -> dict[str, Any]:
        """读取 instance.json；文件缺失或损坏时返回空字典。"""
        try:
            with open(self.info_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_info(self, port: int | None = None, url: str | None = None) -> None:
        """写入当前实例信息（先写临时文件再原子替换，失败则退回直接覆盖写）。"""
        payload = {
            "pid": os.getpid(),
            "port": port,
            "url": url,
            "instance_id": _DEFAULT_MANAGER.instance_id,
            "started_at": time.time(),
        }
        text = json.dumps(payload, ensure_ascii=False)
        tmp_path = self.info_path + f".{os.getpid()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, self.info_path)
            return
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        try:
            with open(self.info_path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    # -- 对外接口 -----------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._held

    def acquire(
        self,
        kill_wait_sec: float = 3.0,
        on_killed: Callable[[], Any] | None = None,
    ) -> bool | None:
        """
        获取单实例锁。
        返回值：
          True  —— 已持有锁（若存在老实例则已将其强杀）；
          False —— 强杀后等待 kill_wait_sec 仍抢不到锁，老实例信息见 self.blocking_info；
          None  —— 锁文件无法创建（目录均不可写），无法保证单实例，调用方可降级继续运行。
        on_killed：确实强杀过老实例且随后成功抢到锁时调用（用于清理老实例遗留的临时缓存）。
        """
        if self._held:
            return True
        if self._lock_fh is None and not self._open_lock_file():
            return None

        # 1) 直接抢锁成功：不存在存活的老实例
        if self._try_lock():
            self._held = True
            self._write_info()
            return True

        # 2) 锁被占用：按记录的 PID 强杀老实例
        info = self.read_info()
        pid_raw = info.get("pid")
        killed = False
        if isinstance(pid_raw, int) and pid_raw > 0 and pid_raw != os.getpid():
            killed = _force_kill_pid(pid_raw)
            if killed:
                self.killed_pid = pid_raw

        # 3) 轮询等待操作系统释放老实例持有的锁
        deadline = time.monotonic() + max(0.1, float(kill_wait_sec))
        while time.monotonic() < deadline:
            if self._try_lock():
                self._held = True
                self._write_info()
                if killed and on_killed is not None:
                    try:
                        on_killed()
                    except Exception:
                        pass
                return True
            time.sleep(0.05)

        # 4) 仍失败：保留老实例信息供调用方兜底（如打开其页面）
        self.blocking_info = self.read_info() or info
        return False

    def update_endpoint(self, port: int, url: str) -> None:
        """HTTP 服务绑定端口后调用，记录实际监听端口与访问地址。"""
        if self._held:
            self._write_info(port=int(port), url=str(url))

    def release(self) -> None:
        """主动释放锁（进程退出/被杀时系统也会自动释放，此处仅为正常退出路径的收尾）。"""
        fh = self._lock_fh
        self._lock_fh = None
        if fh is None:
            return
        if self._held:
            self._held = False
            # 仅当记录仍属于自己时才删除，避免误删新实例写入的信息
            if self.read_info().get("pid") == os.getpid():
                try:
                    os.remove(self.info_path)
                except OSError:
                    pass
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass


def register_lifecycle_routes(
    app: Flask,
    cleanup_fn: Callable[[], Any] | None = None,
    manager: LifecycleManager | None = None,
) -> LifecycleManager:
    active_mgr = _DEFAULT_MANAGER if manager is None else manager
    active_mgr.register_cleanup(cleanup_fn)

    @app.route("/api/lifecycle/status", methods=["GET"])
    def api_lifecycle_status() -> Response:
        # 页面加载/从往返缓存恢复时带 hello=<本实例 instance_id>：视为刷新完成，取消待执行的标签页关闭退出。
        # 其他标签页的普通状态同步不带该参数，不会取消计时（实现「任一标签页关闭即退出」）；
        # 校验 instance_id 可避免被替换掉的旧实例残留页面误取消计时。
        if request.args.get("hello") == active_mgr.instance_id:
            active_mgr.note_page_hello(request.args.get("page", "")[:64])
        shutting = active_mgr.shutting_down
        resp = jsonify(
            {
                "alive": not shutting,
                "shutting_down": shutting,
                "reason": active_mgr.shutdown_reason,
                "remaining_ms": active_mgr.remaining_ms(),
                "auto_exit_seconds": active_mgr.timeout_seconds,
                "instance_id": active_mgr.instance_id,
            }
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/lifecycle/shutdown", methods=["POST"])
    def api_lifecycle_shutdown() -> tuple[Response, int] | Response:
        if not _is_loopback_remote(request.remote_addr):
            return jsonify({"ok": False, "error": "仅允许本机回环地址发起退出请求"}), 403

        provided_token = request.headers.get("X-Malody-Shutdown-Token", "").strip()
        if not provided_token:
            body = request.get_json(silent=True)
            if isinstance(body, dict) and isinstance(body.get("token"), str):
                provided_token = body["token"].strip()

        if not provided_token or not secrets.compare_digest(
            provided_token, active_mgr.shutdown_token
        ):
            return jsonify({"ok": False, "error": "无效的退出安全令牌"}), 403

        active_mgr.trigger_shutdown(reason="manual_button", exit_delay_sec=0.35)
        resp = jsonify(
            {
                "ok": True,
                "status": "shutting_down",
                "instance_id": active_mgr.instance_id,
            }
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/lifecycle/leave", methods=["POST"])
    def api_lifecycle_leave() -> tuple[Response, int] | Response:
        """
        标签页卸载 (pagehide) 时由 navigator.sendBeacon 调用：启动 DEFAULT_TAB_LEAVE_GRACE_SECONDS 宽限计时。
        sendBeacon 无法自定义请求头且只能用 CORS 安全的 text/plain，故令牌以 JSON 文本放在请求体中。
        """
        if not _is_loopback_remote(request.remote_addr):
            return jsonify({"ok": False, "error": "仅允许本机回环地址发起离开通知"}), 403

        provided_token = ""
        page_id = ""
        try:
            body = json.loads(request.get_data(as_text=True) or "{}")
            if isinstance(body, dict):
                if isinstance(body.get("token"), str):
                    provided_token = body["token"].strip()
                if isinstance(body.get("page"), str):
                    page_id = body["page"][:64]
        except ValueError:
            pass

        if not provided_token or not secrets.compare_digest(
            provided_token, active_mgr.shutdown_token
        ):
            return jsonify({"ok": False, "error": "无效的退出安全令牌"}), 403

        scheduled = active_mgr.schedule_leave_shutdown(page_id=page_id)
        resp = jsonify({"ok": True, "scheduled": scheduled})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return active_mgr


_LIFECYCLE_CSS = r"""
  /* 顶栏退出按钮与已退出静态覆盖层样式 (web_gui_lifecycle.py) */
  .header-ctrl-divider {
    width: 1px;
    height: 20px;
    background: var(--border);
    margin: 0 2px;
    flex-shrink: 0;
  }
  .btn.btn-exit-app,
  .btn-exit-app {
    height: 36px;
    padding: 0 13px;
    border-radius: 999px;
    background: #121212;
    color: #FFFFFF; /* 2026-11-11：默认文字（及跟随 currentColor 的图标）由灰色 var(--muted) 改为纯白 */
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    font-size: 14px;
    font-weight: 500;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    transition: background 0.16s ease, color 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
  }
  .btn.btn-exit-app:hover:not(:disabled),
  .btn-exit-app:hover:not(:disabled) {
    background: rgba(248, 81, 73, 0.18);
    color: #FFFFFF;
    border-color: rgba(248, 81, 73, 0.65);
    box-shadow: 0 0 0 3px rgba(248, 81, 73, 0.10);
  }
  .btn-exit-app:hover:not(:disabled) .header-btn-icon {
    color: var(--red);
  }
  .btn.btn-exit-app:active:not(:disabled),
  .btn-exit-app:active:not(:disabled) {
    background: rgba(248, 81, 73, 0.26);
  }
  .btn-exit-app:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .modal-backdrop.lifecycle-confirm-layer {
    z-index: 1300;
  }
  .lifecycle-exited-overlay {
    position: fixed;
    inset: 0;
    z-index: 9999;
    background: var(--bg);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .lifecycle-exited-overlay.open {
    display: flex;
  }
  .lifecycle-exited-card {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 20px;
    max-width: 460px;
    width: 100%;
    padding: 32px 28px;
    text-align: center;
    box-shadow: 0 20px 56px rgba(0, 0, 0, 0.55);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
  }
  .lifecycle-exited-badge {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
    font-weight: 700;
    background: rgba(63, 185, 80, 0.14);
    color: var(--green);
    border: 1px solid rgba(63, 185, 80, 0.38);
  }
  .lifecycle-exited-badge.timeout {
    background: rgba(210, 153, 34, 0.14);
    color: var(--amber);
    border-color: rgba(210, 153, 34, 0.4);
  }
  .lifecycle-exited-title {
    font-size: 19px;
    font-weight: 600;
    color: var(--text);
  }
  .lifecycle-exited-desc {
    font-size: 14px;
    line-height: 1.65;
    color: var(--muted);
  }
"""

_HEADER_BUTTON_ANCHOR = (
    '<button type="button" class="header-combo-btn" id="openSettingsBtn" title="查看当前测算模式并打开参数设置">\n'
    '      <span class="combo-status-dot" aria-hidden="true"></span>\n'
    '      <span class="current-mode-pill" id="currentModePill"><span>当前模式：<b>全部</b></span></span>\n'
    '      <span class="combo-sep" aria-hidden="true"></span>\n'
    # 2026-11-11：与 web_gui_settings._SETTINGS_HEADER_BTN_HTML 同步改为线条齿轮图标（锚点须逐字一致）
    '      <span class="combo-action"><svg class="header-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg><span>点按以打开程序设置</span></span>\n'
    '    </button>'
)
_HEADER_EXIT_BUTTON_HTML = (
    _HEADER_BUTTON_ANCHOR
    + '\n    <span class="header-ctrl-divider" aria-hidden="true"></span>\n'
    + '    <button type="button" class="btn btn-exit-app" id="exitAppBtn">'
    + '<svg class="header-btn-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2.2v5.1"/><path d="M4.6 4.1a5.5 5.5 0 1 0 6.8 0"/></svg>'
    + '<span>退出程序</span></button>'
)

_LIFECYCLE_DOM_HTML = """
<!-- 退出程序二次确认弹窗 (web_gui_lifecycle.py) -->
<div class="modal-backdrop confirm-layer lifecycle-confirm-layer" id="confirmExitModal" role="dialog" aria-modal="true" aria-labelledby="confirmExitModalTitle">
  <div class="modal-dialog confirm-dialog">
    <div class="modal-header">
      <h2 id="confirmExitModalTitle">退出程序</h2>
    </div>
    <div class="modal-body">
      <p id="confirmExitModalDesc" style="font-size:14px; line-height:1.65; color:#FFFFFF;">确认退出程序？（后续您可能需要再次手动关闭标签页或浏览器窗口。）</p>
    </div>
    <div class="modal-footer" style="justify-content:flex-end;">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-tonal" id="cancelExitAppBtn">取消</button>
        <button type="button" class="m3-btn m3-btn-danger" id="confirmExitAppBtn">确认退出</button>
      </div>
    </div>
  </div>
</div>

<!-- 服务已退出全屏静态覆盖层 (web_gui_lifecycle.py) -->
<div class="lifecycle-exited-overlay" id="lifecycleExitedOverlay" role="status" aria-live="polite">
  <div class="lifecycle-exited-card">
    <div class="lifecycle-exited-badge" id="lifecycleExitedBadge">✓</div>
    <div class="lifecycle-exited-title" id="lifecycleExitedTitle">程序已安全退出</div>
    <div class="lifecycle-exited-desc" id="lifecycleExitedDesc">本地 BPM 测算后台服务已停止，现在可以直接关闭此标签页或浏览器窗口。</div>
  </div>
</div>
"""

_LIFECYCLE_JS_TEMPLATE = r"""
<script>
(() => {
  const SHUTDOWN_TOKEN = __SHUTDOWN_TOKEN_JSON__;
  const INSTANCE_ID = __INSTANCE_ID_JSON__;
  const MAX_TIMER_MS = 2147483000;
  // 每次页面加载生成的随机 ID：后端据此区分「本页离开」与「新页面加载（刷新）」
  const PAGE_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);

  const exitAppBtn = document.getElementById("exitAppBtn");
  const confirmExitModal = document.getElementById("confirmExitModal");
  const cancelExitAppBtn = document.getElementById("cancelExitAppBtn");
  const confirmExitAppBtn = document.getElementById("confirmExitAppBtn");

  const exitedOverlay = document.getElementById("lifecycleExitedOverlay");
  const exitedBadge = document.getElementById("lifecycleExitedBadge");
  const exitedTitle = document.getElementById("lifecycleExitedTitle");
  const exitedDesc = document.getElementById("lifecycleExitedDesc");

  let lifecycleExited = false;
  let exitInFlight = false;
  let autoExitTimerId = null;

  const bc = typeof BroadcastChannel === "function"
    ? new BroadcastChannel("malody_bpm_lifecycle")
    : null;

  function handleBeforeUnload(e) {
    if (lifecycleExited) return;
    e.preventDefault();
    e.returnValue = "";
    return "";
  }

  window.addEventListener("beforeunload", handleBeforeUnload);

  function disarmBeforeUnload() {
    lifecycleExited = true;
    window.removeEventListener("beforeunload", handleBeforeUnload);
  }

  function clearAutoExitTimer() {
    if (autoExitTimerId !== null) {
      window.clearTimeout(autoExitTimerId);
      autoExitTimerId = null;
    }
  }

  function openExitModal() {
    if (lifecycleExited || exitInFlight || !confirmExitModal) return;
    confirmExitModal.classList.add("open");
  }

  function closeExitModal() {
    if (exitInFlight || !confirmExitModal) return;
    confirmExitModal.classList.remove("open");
  }

  function enterExitedScreen(mode) {
    disarmBeforeUnload();
    clearAutoExitTimer();

    if (confirmExitModal) confirmExitModal.classList.remove("open");
    const settingsModalEl = document.getElementById("settingsModal");
    if (settingsModalEl) settingsModalEl.classList.remove("open");
    const confirmClearModalEl = document.getElementById("confirmClearModal");
    if (confirmClearModalEl) confirmClearModalEl.classList.remove("open");

    const offsetViewEl = document.getElementById("offsetSecondaryView");
    const offsetBackBtnEl = document.getElementById("offsetBackBtn");
    if (offsetViewEl && offsetViewEl.classList.contains("active") && offsetBackBtnEl) {
      try {
        offsetBackBtnEl.click();
      } catch (_) {}
    }

    if (mode === "auto_timeout") {
      if (exitedBadge) {
        exitedBadge.textContent = "⏱";
        exitedBadge.classList.add("timeout");
      }
      if (exitedTitle) exitedTitle.textContent = "已达 3 小时运行上限，服务已自动退出";
      if (exitedDesc) {
        exitedDesc.textContent = "程序已满启动后 3 小时自动关闭时限，后台进程已终止。现在可以直接关闭此标签页或浏览器窗口。";
      }
    } else {
      if (exitedBadge) {
        exitedBadge.textContent = "✓";
        exitedBadge.classList.remove("timeout");
      }
      if (exitedTitle) exitedTitle.textContent = "程序已安全退出";
      if (exitedDesc) {
        exitedDesc.textContent = "本地 BPM 测算后台服务已停止，现在可以直接关闭此标签页或浏览器窗口。";
      }
    }

    document.title = "已退出 - 音频 BPM 深度测算仪";
    if (exitedOverlay) exitedOverlay.classList.add("open");
  }

  if (bc) {
    bc.addEventListener("message", (ev) => {
      const msg = ev && ev.data;
      if (!msg || msg.instance !== INSTANCE_ID) return;
      if (msg.type === "exited") {
        enterExitedScreen(msg.mode || "manual");
      }
    });
  }

  async function executeManualShutdown() {
    if (exitInFlight || lifecycleExited) return;
    exitInFlight = true;

    if (confirmExitAppBtn) {
      confirmExitAppBtn.disabled = true;
      confirmExitAppBtn.textContent = "正在退出...";
    }
    if (cancelExitAppBtn) cancelExitAppBtn.disabled = true;
    if (exitAppBtn) exitAppBtn.disabled = true;

    disarmBeforeUnload();

    if (bc) {
      try {
        bc.postMessage({ type: "exited", instance: INSTANCE_ID, mode: "manual" });
      } catch (_) {}
    }

    const ctrl = typeof AbortController === "function" ? new AbortController() : null;
    const abortTimer = ctrl ? window.setTimeout(() => ctrl.abort(), 1500) : null;
    try {
      await fetch("/api/lifecycle/shutdown", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Malody-Shutdown-Token": SHUTDOWN_TOKEN
        },
        body: JSON.stringify({ token: SHUTDOWN_TOKEN }),
        keepalive: true,
        signal: ctrl ? ctrl.signal : undefined
      });
    } catch (_) {
      // 后端快速退出关闭套接字时可能直接中断连接，属正常现象
    } finally {
      if (abortTimer !== null) window.clearTimeout(abortTimer);
    }

    enterExitedScreen("manual");
    try {
      window.close();
    } catch (_) {}
  }

  // 本地估算的 3 小时自动退出时刻（毫秒时间戳），用于网络断开时区分「超时退出」与「其他原因退出」
  let expectedAutoExitAt = null;

  // 网络不可达时推断退出原因：已过（或临近）3 小时时限判为超时，否则视为已安全退出
  // （标签页关闭退出、被新启动的实例替换等均属后者）
  function inferOfflineExitMode() {
    if (expectedAutoExitAt !== null && Date.now() >= expectedAutoExitAt - 1500) {
      return "auto_timeout";
    }
    return "manual";
  }

  // sayHello=true：页面加载/从往返缓存恢复时调用，通知后端「页面已重新加载」，取消标签页关闭宽限计时
  async function syncLifecycleStatus(treatNetworkErrorAsExited, sayHello) {
    if (lifecycleExited || exitInFlight) return;
    clearAutoExitTimer();

    try {
      const statusUrl = sayHello
        ? "/api/lifecycle/status?hello=" + encodeURIComponent(INSTANCE_ID)
          + "&page=" + encodeURIComponent(PAGE_ID)
        : "/api/lifecycle/status";
      const resp = await fetch(statusUrl, {
        method: "GET",
        cache: "no-store"
      });
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data || typeof data !== "object") return;

      if (data.instance_id && data.instance_id !== INSTANCE_ID) {
        // 端口已被新实例接管（本实例已被替换）
        enterExitedScreen("manual");
        return;
      }
      if (!data.alive || data.shutting_down || Number(data.remaining_ms) <= 0) {
        // 超时（含后端尚未标记原因但时限已到）显示超时文案；手动按钮/标签页关闭等显示「已安全退出」
        const isTimeout = data.reason === "auto_timeout"
          || (!data.reason && Number(data.remaining_ms) <= 0);
        enterExitedScreen(isTimeout ? "auto_timeout" : "manual");
        return;
      }

      expectedAutoExitAt = Date.now() + Number(data.remaining_ms);
      const remMs = Math.max(200, Number(data.remaining_ms) + 250);
      autoExitTimerId = window.setTimeout(() => {
        syncLifecycleStatus(true);
      }, Math.min(remMs, MAX_TIMER_MS));
    } catch (_) {
      if (treatNetworkErrorAsExited && !lifecycleExited) {
        enterExitedScreen(inferOfflineExitMode());
      }
    }
  }

  if (exitAppBtn) {
    exitAppBtn.addEventListener("click", openExitModal);
  }
  if (cancelExitAppBtn) {
    cancelExitAppBtn.addEventListener("click", closeExitModal);
  }
  if (confirmExitModal) {
    confirmExitModal.addEventListener("click", (e) => {
      if (e.target === confirmExitModal) closeExitModal();
    });
  }
  if (confirmExitAppBtn) {
    confirmExitAppBtn.addEventListener("click", executeManualShutdown);
  }

  window.addEventListener(
    "keydown",
    (e) => {
      if (lifecycleExited) {
        if (e.key === " " || e.key === "ArrowUp" || e.key === "ArrowDown" || e.key === "Escape") {
          e.preventDefault();
          e.stopImmediatePropagation();
        }
        return;
      }
      if (confirmExitModal && confirmExitModal.classList.contains("open")) {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopImmediatePropagation();
          closeExitModal();
          return;
        }
        if (e.key === "ArrowUp" || e.key === "ArrowDown" || e.key === " ") {
          e.stopImmediatePropagation();
        }
      }
    },
    true
  );

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !lifecycleExited && !exitInFlight) {
      syncLifecycleStatus(true);
    }
  });

  // 标签页关闭/刷新/跳走时（用户已通过 beforeunload 确认离开）发送「离开」信号，
  // 后端启动 2.5 秒宽限计时；若是刷新，新页面加载时的 hello 会取消该计时。
  // 已进入退出状态（手动退出/超时/被替换）时无需再通知。
  // 注意：刷新同样会触发 pagehide，因此此处不通过 BroadcastChannel 通知其他标签页。
  window.addEventListener("pagehide", () => {
    if (lifecycleExited || exitInFlight) return;
    try {
      if (navigator.sendBeacon) {
        // sendBeacon 仅允许 CORS 安全的 text/plain，令牌以 JSON 文本放入请求体
        const payload = new Blob(
          [JSON.stringify({ token: SHUTDOWN_TOKEN, page: PAGE_ID })],
          { type: "text/plain" }
        );
        navigator.sendBeacon("/api/lifecycle/leave", payload);
      }
    } catch (_) {}
  });

  // 从往返缓存 (bfcache) 恢复（如后退键返回）时：重新打招呼取消计时；若后端已退出则显示退出遮罩
  window.addEventListener("pageshow", (ev) => {
    if (ev && ev.persisted && !lifecycleExited && !exitInFlight) {
      syncLifecycleStatus(true, true);
    }
  });

  // 页面首次加载：打招呼（若为刷新则取消上一页面触发的关闭计时）
  syncLifecycleStatus(false, true);
})();
</script>
"""


def inject_lifecycle_ui(
    index_html: str,
    manager: LifecycleManager | None = None,
) -> str:
    """将顶栏退出按钮、退出确认弹窗、静态退出覆盖层及浏览器提醒联动脚本无侵入注入 INDEX_HTML。"""
    active_mgr = _DEFAULT_MANAGER if manager is None else manager
    html = index_html

    if "</style>" not in html:
        raise RuntimeError("inject_lifecycle_ui 未找到 </style> 锚点，拒绝静默注入失败")
    html = html.replace("</style>", _LIFECYCLE_CSS + "\n</style>", 1)

    if _HEADER_BUTTON_ANCHOR not in html:
        raise RuntimeError("inject_lifecycle_ui 未找到顶栏 #openSettingsBtn 锚点，拒绝静默注入失败")
    html = html.replace(_HEADER_BUTTON_ANCHOR, _HEADER_EXIT_BUTTON_HTML, 1)

    if "</body>" not in html:
        raise RuntimeError("inject_lifecycle_ui 未找到 </body> 锚点，拒绝静默注入失败")

    js_block = _LIFECYCLE_JS_TEMPLATE.replace(
        "__SHUTDOWN_TOKEN_JSON__", json.dumps(active_mgr.shutdown_token)
    ).replace(
        "__INSTANCE_ID_JSON__", json.dumps(active_mgr.instance_id)
    )
    html = html.replace("</body>", _LIFECYCLE_DOM_HTML + "\n" + js_block + "\n</body>", 1)
    return html
