"""服务装配：``AppServer`` 持有配置/闸门/信号量/进度表，另含启动与过期清理。

``serve()`` 的横幅里会打印鉴权模式、配额、连接超时和反代信任状态 —— 这几项都是
「配错了不会报错、只会静默不安全」的参数，启动时打出来最省事。
"""

from __future__ import annotations

import shutil
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import __version__, transcode
from ..guard import Guard, Limits
from ..jobs import JobRegistry
from .base import BaseHandler
from .config import ServerConfig

__all__ = ["AppServer", "create_server", "serve"]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class AppServer(ThreadingHTTPServer):
    """带业务状态的 HTTP 服务（替代旧的 ``type("Handler", ...)`` 动态类写法）。"""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, cfg: ServerConfig, guard: Guard) -> None:
        # 这两个字段必须在 super().__init__() 之前就位：父类绑定端口失败时会回调
        # self.server_close()，而 server_close 要读 _cleaner_stop。否则真正的错误
        # （比如 Errno 98 端口被占）会被 AttributeError 盖掉，排查时看到的是
        # 「'AppServer' object has no attribute '_cleaner_stop'」这种假线索。
        self._cleaner_stop = threading.Event()
        self._cleaner: threading.Thread | None = None
        super().__init__((cfg.host, cfg.port), BaseHandler)
        self.cfg = cfg
        self.guard = guard
        self.web_dir = WEB_DIR
        self.slots = threading.Semaphore(max(1, cfg.concurrency))
        self.dl_slots = threading.Semaphore(cfg.slots_for_download)
        self.jobs = JobRegistry()
        # ffmpeg 探测只做一次：health 曾因此每次请求都扫 PATH + 起子进程
        self.ffmpeg = cfg.ffmpeg or transcode.find_ffmpeg()
        self.ffmpeg_version = transcode.ffmpeg_version(self.ffmpeg) if self.ffmpeg else None

    def start_cleaner(self) -> None:
        if self._cleaner is not None:
            return
        self._cleaner = threading.Thread(
            target=_cleaner, args=(self.cfg, self.guard, self._cleaner_stop), daemon=True,
        )
        self._cleaner.start()

    def server_close(self) -> None:
        self._cleaner_stop.set()
        super().server_close()


def _cleaner(cfg: ServerConfig, guard: Guard, stop: threading.Event) -> None:
    """后台清过期 job 目录（TTL + 宽限一个检查周期）。"""
    interval = 300
    while not stop.wait(interval):
        cutoff = time.time() - cfg.ttl
        freed = 0
        try:
            entries = list(cfg.jobs_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                    continue
                freed += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
        if freed:
            guard.note_disk(-freed)


def create_server(cfg: ServerConfig, *, start_cleaner: bool = True) -> AppServer:
    cfg.ensure_dirs()
    guard = Guard(
        user=cfg.auth_user,
        password=cfg.auth_pass,
        token=cfg.access_token,
        limits=cfg.limits,
        jobs_dir=cfg.jobs_dir,
    )
    httpd = AppServer(cfg, guard)
    if start_cleaner:
        httpd.start_cleaner()
    return httpd


def _describe_limits(limits: Limits) -> str:
    bits = []
    if limits.per_minute > 0:
        bits.append(f"{limits.per_minute} 次/分")
    if limits.files_per_day > 0:
        bits.append(f"{limits.files_per_day} 文件/日")
    if limits.mb_per_day > 0:
        bits.append(f"{limits.mb_per_day / 1024:.0f} GB/日")
    if limits.disk_mb > 0:
        bits.append(f"磁盘 {limits.disk_mb / 1024:.0f} GB")
    return " · ".join(bits) if bits else "不限制"


def serve(cfg: ServerConfig) -> int:
    httpd = create_server(cfg)
    guard: Any = httpd.guard
    shown = cfg.host if cfg.host not in ("0.0.0.0", "") else "127.0.0.1"
    print(f"audio-forge {__version__}  http://{shown}:{cfg.port}")
    print(f"  临时目录 : {cfg.root}")
    print(f"  上传上限 : {cfg.max_bytes / (1 << 20):.0f} MB")
    print(f"  通用转码 : {httpd.ffmpeg_version or '不可用（未找到 ffmpeg，仅 NCM 通道可用）'}")
    if guard.mode == "none":
        print("  鉴权     : 未启用 —— 任何能访问该端口的人都能上传，只应在本机使用")
    else:
        who = f"用户 {cfg.auth_user}" if guard.has_basic else ""
        tok = "Bearer 令牌" if cfg.access_token else ""
        print(f"  鉴权     : {guard.mode}（{' + '.join(x for x in (who, tok) if x)}）")
    print(f"  配额     : {_describe_limits(cfg.limits)}")
    print(f"  连接超时 : {cfg.conn_timeout}s（慢连接会被断开）")
    if cfg.trust_proxy:
        proxies = ", ".join(cfg.trusted_proxies) or "（空）"
        print(f"  反代信任 : 开，仅接受来自 {proxies} 的 X-Forwarded-For")
    else:
        print("  反代信任 : 关（所有请求按直连 IP 记账；放在反代后面请开 --trust-proxy）")
    print("  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
