"""进程内的 job 进度表，供 ``GET /api/job/<id>`` 轮询。

只存内存、重启即失。完成的条目保留 ``ttl`` 秒（够前端补最后一次轮询），
条目数超过 ``limit`` 时按「已完成且最旧」优先清掉，避免长期运行后无限增长。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

__all__ = ["JobRegistry"]


@dataclass
class JobRegistry:
    clock: Callable[[], float] = time.time
    ttl: float = 300.0
    limit: int = 1000
    _items: dict[str, dict] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---------------------------------------------------------------- #

    def start(self, key: str, total: int) -> None:
        """登记一次上传；``total`` 是解析出的文件数（未知时先给 0）。"""
        now = self.clock()
        with self._lock:
            self._prune_locked(now)
            self._items[key] = {
                "job": key,
                "phase": "parsing",
                "total": total,
                "done": 0,
                "failed": 0,
                "started_at": now,
                "finished_at": None,
                "error": None,
                "items": [],
            }

    def phase(self, key: str, name: str) -> None:
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                item["phase"] = name

    def add(self, key: str, result: dict) -> None:
        """记录一个文件的转换结果（成功或失败都算「处理完一个」）。"""
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return
            item["done"] += 1
            if not result.get("ok"):
                item["failed"] += 1
            item["items"].append(
                {
                    "source": result.get("source"),
                    "ok": bool(result.get("ok")),
                    "name": result.get("name"),
                    "error": result.get("error"),
                }
            )

    def finish(self, key: str, error: str | None = None) -> None:
        now = self.clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return
            item["phase"] = "error" if error else "done"
            item["error"] = error
            item["finished_at"] = now

    def get(self, key: str) -> dict | None:
        now = self.clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            snap = dict(item)
            snap["items"] = list(item["items"])
        snap["finished"] = snap["finished_at"] is not None
        end = snap["finished_at"] or now
        snap["elapsed"] = round(end - snap["started_at"], 2)
        snap.pop("started_at", None)
        snap.pop("finished_at", None)
        return snap

    def discard(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    # ---------------------------------------------------------------- #

    def _prune_locked(self, now: float) -> None:
        dead = [
            k
            for k, v in self._items.items()
            if v["finished_at"] is not None and now - v["finished_at"] > self.ttl
        ]
        for k in dead:
            self._items.pop(k, None)

        overflow = len(self._items) - self.limit + 1
        if overflow > 0:
            ordered = sorted(
                self._items.items(),
                key=lambda kv: (kv[1]["finished_at"] is None, kv[1]["started_at"]),
            )
            for k, _ in ordered[:overflow]:
                self._items.pop(k, None)
