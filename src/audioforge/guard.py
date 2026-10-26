"""访问控制：可选鉴权 + 速率/配额/磁盘闸门。

**为什么放在应用层而不是只靠反代**：反代（Nginx）能挡鉴权，但挡不住「一个通过认证的
人连续传 500 个 200 MB 文件把磁盘写满」。配额必须知道「一次请求里有几个文件、多少字节」，
这个信息只有应用自己清楚。所以这里做完整的闸门，反代只负责 TLS 和粗粒度限流。

三种能力可单独或组合：

* **Basic 认证** —— 浏览器原生弹框，适合人用。配置 ``user`` / ``password``。
* **Bearer 令牌** —— 适合脚本与 CI。配置 ``token``，也接受 ``X-Auth-Token`` 头。
* 两者都不配 —— 不鉴权（默认），此时只应监听 ``127.0.0.1``。

配额按客户端 IP 记账，固定窗口（分钟 / 日），进程内存储，重启清零。单机工具够用；
要做多实例共享配额得换 Redis，不在本项目范围内。

静态资源（``/``、``/app.js``、``/style.css``）**不鉴权**：它们不含数据，且 Bearer 模式下
页面必须先加载出来，JS 才有机会从 ``?token=`` 里取出令牌。
"""

from __future__ import annotations

import base64
import hmac
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Denied", "Limits", "Guard"]

MINUTE = 60.0
DAY = 86400.0


class Denied(Exception):
    """请求被拒绝。``status`` 直接作为 HTTP 状态码。"""

    def __init__(self, status: int, message: str, headers: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass
class Limits:
    """配额阈值。数值 ``<= 0`` 表示该项不限制。"""

    per_minute: int = 120          # 每 IP 每分钟请求数
    files_per_day: int = 2000      # 每 IP 每日转换文件数
    mb_per_day: int = 20 * 1024    # 每 IP 每日上传总量（MiB）
    disk_mb: int = 20 * 1024       # jobs 目录总量上限（MiB），超了先清最旧

    @classmethod
    def unlimited(cls) -> "Limits":
        return cls(0, 0, 0, 0)

    def as_dict(self) -> dict:
        return {
            "per_minute": self.per_minute,
            "files_per_day": self.files_per_day,
            "mb_per_day": self.mb_per_day,
            "disk_mb": self.disk_mb,
        }


@dataclass
class _Bucket:
    """单个 IP 的计数器。分钟窗口与日窗口各自独立重置。"""

    minute_start: float = 0.0
    minute_count: int = 0
    day_start: float = 0.0
    day_files: int = 0
    day_bytes: int = 0


@dataclass
class Guard:
    user: str | None = None
    password: str | None = None
    token: str | None = None
    limits: Limits = field(default_factory=Limits)
    jobs_dir: Path | None = None
    clock: object = time.time          # 测试注入
    _buckets: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    last_prune: int = 0

    # ---------------------------------------------------------------- #
    # 鉴权
    # ---------------------------------------------------------------- #

    @property
    def has_basic(self) -> bool:
        return bool(self.user and self.password is not None)

    @property
    def requires_auth(self) -> bool:
        return self.has_basic or bool(self.token)

    @property
    def mode(self) -> str:
        if self.has_basic and self.token:
            return "basic+bearer"
        if self.has_basic:
            return "basic"
        if self.token:
            return "bearer"
        return "none"

    def _challenge(self) -> dict:
        if self.has_basic:
            return {"WWW-Authenticate": 'Basic realm="audio-forge", charset="UTF-8"'}
        return {}

    def _check_basic(self, header: str) -> bool:
        if not self.has_basic or not header[:6].lower() == "basic ":
            return False
        try:
            raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        name, sep, secret = raw.partition(":")
        if not sep:
            return False
        # 两条都要比，避免用「用户名对但密码空」试探
        ok_user = hmac.compare_digest(name, self.user or "")
        ok_pass = hmac.compare_digest(secret, self.password or "")
        return ok_user and ok_pass

    def _check_bearer(self, headers) -> bool:
        if not self.token:
            return False
        header = headers.get("Authorization", "") or ""
        candidate = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not candidate:
            candidate = (headers.get("X-Auth-Token", "") or "").strip()
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def verify(self, headers) -> None:
        """不通过就抛 ``Denied(401)``。"""
        if not self.requires_auth:
            return
        header = headers.get("Authorization", "") or ""
        if self._check_basic(header) or self._check_bearer(headers):
            return
        raise Denied(401, "需要身份验证", self._challenge())

    # ---------------------------------------------------------------- #
    # 速率与配额
    # ---------------------------------------------------------------- #

    def _bucket(self, ip: str) -> _Bucket:
        b = self._buckets.get(ip)
        if b is None:
            now = self.clock()  # type: ignore[operator]
            b = _Bucket(minute_start=now, day_start=now)
            self._buckets[ip] = b
        return b

    def check_rate(self, ip: str) -> None:
        limit = self.limits.per_minute
        if limit <= 0:
            return
        now = self.clock()  # type: ignore[operator]
        with self._lock:
            b = self._bucket(ip)
            if now - b.minute_start >= MINUTE:
                b.minute_start, b.minute_count = now, 0
            if b.minute_count >= limit:
                retry = max(1, int(MINUTE - (now - b.minute_start)) + 1)
                raise Denied(429, f"请求过于频繁：每 IP 每分钟上限 {limit} 次",
                             {"Retry-After": str(retry)})
            b.minute_count += 1

    def check_quota(self, ip: str, n_bytes: int = 0, n_files: int = 1) -> None:
        limits = self.limits
        if limits.files_per_day <= 0 and limits.mb_per_day <= 0:
            return
        now = self.clock()  # type: ignore[operator]
        with self._lock:
            b = self._bucket(ip)
            if now - b.day_start >= DAY:
                b.day_start, b.day_files, b.day_bytes = now, 0, 0
            if limits.files_per_day > 0 and b.day_files + n_files > limits.files_per_day:
                raise Denied(429, f"今日配额已用尽：每 IP 每天 {limits.files_per_day} 个文件")
            if limits.mb_per_day > 0:
                cap = limits.mb_per_day * (1 << 20)
                if b.day_bytes + n_bytes > cap:
                    raise Denied(429, f"今日配额已用尽：每 IP 每天 {limits.mb_per_day} MB")

    def record(self, ip: str, n_files: int, n_bytes: int) -> None:
        with self._lock:
            b = self._bucket(ip)
            b.day_files += n_files
            b.day_bytes += n_bytes

    def usage(self, ip: str) -> dict:
        with self._lock:
            b = self._bucket(ip)
        return {
            "files_today": b.day_files,
            "mb_today": round(b.day_bytes / (1 << 20), 2),
            "requests_this_minute": b.minute_count,
        }

    # ---------------------------------------------------------------- #
    # 磁盘
    # ---------------------------------------------------------------- #

    def disk_usage_mb(self) -> float:
        if not self.jobs_dir or not self.jobs_dir.is_dir():
            return 0.0
        total = 0
        for path in self.jobs_dir.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total / (1 << 20)

    def ensure_disk(self) -> int:
        """超过磁盘上限时按 mtime 从旧到新删 job 目录，返回释放的 MiB。"""
        cap = self.limits.disk_mb
        if cap <= 0 or not self.jobs_dir or not self.jobs_dir.is_dir():
            return 0
        used = self.disk_usage_mb()
        if used <= cap:
            return 0

        import shutil

        freed = 0.0
        jobs = []
        for d in self.jobs_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                jobs.append((d.stat().st_mtime, d))
            except OSError:
                continue
        jobs.sort()
        target = cap * 0.8                     # 留 20% 余量，避免每来一个请求就清一次
        for _, d in jobs:
            if used - freed <= target:
                break
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
            freed += size / (1 << 20)
        return round(freed, 2)
