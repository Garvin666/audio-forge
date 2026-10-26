"""服务配置对象。

注意：这里**不建目录**（历史版本在 ``__post_init__`` 里 mkdir，导致配置对象带副作用、
测试必须先造临时目录）。要落盘请显式调用 :meth:`ServerConfig.ensure_dirs`。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..guard import Limits

__all__ = ["ServerConfig", "DEFAULT_TRUSTED_PROXIES"]

# 只有来自回环的反代才允许读 X-Forwarded-For
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = ("127.0.0.1", "::1")


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    max_bytes: int = 200 << 20  # 单次请求上限 200 MiB
    ttl: int = 3600  # job 目录存活秒数
    work_dir: Path | None = None  # 默认系统临时目录
    concurrency: int = 4  # 同时转换的请求数
    download_slots: int = 0  # 同时下载数，0 = 自动（max(8, concurrency*2)）
    conn_timeout: int = 60  # 单次 socket 读超时（慢连接即断开）
    max_out_mb: int = 2048  # 单个产物体积上限，0 = 不限制
    ffmpeg: str | None = None
    quiet: bool = False
    auth_user: str | None = None
    auth_pass: str | None = None
    access_token: str | None = None
    trust_proxy: bool = False  # 仅当反代来自 trusted_proxies 时才认 XFF
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES
    limits: Limits = field(default_factory=Limits)
    _root: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        base = Path(self.work_dir) if self.work_dir else Path(tempfile.gettempdir())
        self._root = base / "audioforge"

    @property
    def root(self) -> Path:
        # __post_init__ 一定会赋值；断言只为把 Optional 收窄回 Path
        assert self._root is not None
        return self._root

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    def ensure_dirs(self) -> Path:
        """建好 jobs 目录并返回根路径（由 ``create_server`` 显式调用）。"""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        return self.root

    @property
    def slots_for_download(self) -> int:
        return self.download_slots if self.download_slots > 0 else max(8, self.concurrency * 2)

    @property
    def max_out_bytes(self) -> int:
        """单产物上限（字节）；``0`` 表示不限制。"""
        return max(0, int(self.max_out_mb)) << 20
