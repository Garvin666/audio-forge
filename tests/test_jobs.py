"""``JobRegistry`` 自测：进度快照、TTL 淘汰、容量上限。

只关心「不会无限增长」和「快照字段对得上」两件事 —— 前端拿它渲染进度，
字段改名不会报错，只会让界面静默空白，所以字段名也在断言里锁住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge.jobs import JobRegistry  # noqa: E402


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.jobs = JobRegistry(clock=self.clock)
        self.jobs.start("j1", total=3)

    def test_initial_snapshot(self):
        snap = self.jobs.get("j1")
        self.assertEqual(snap["job"], "j1")
        self.assertEqual(snap["phase"], "parsing")
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["done"], 0)
        self.assertFalse(snap["finished"])
        self.assertEqual(snap["items"], [])

    def test_add_counts_failures_too(self):
        self.jobs.add("j1", {"source": "a.ncm", "ok": True, "name": "a.mp3"})
        self.jobs.add("j1", {"source": "b.ncm", "ok": False, "error": "boom"})
        snap = self.jobs.get("j1")
        self.assertEqual(snap["done"], 2)  # 失败也算「处理完一个」
        self.assertEqual(snap["failed"], 1)
        self.assertEqual([i["ok"] for i in snap["items"]], [True, False])

    def test_finish_freezes_elapsed(self):
        self.clock.advance(1.5)
        self.jobs.finish("j1")
        snap = self.jobs.get("j1")
        self.assertEqual(snap["phase"], "done")
        self.assertTrue(snap["finished"])
        self.assertEqual(snap["elapsed"], 1.5)

        self.clock.advance(100)  # 完成后再看，耗时不跟着涨
        self.assertEqual(self.jobs.get("j1")["elapsed"], 1.5)

    def test_finish_with_error(self):
        self.jobs.finish("j1", error="MultipartError: bad boundary")
        snap = self.jobs.get("j1")
        self.assertEqual(snap["phase"], "error")
        self.assertEqual(snap["error"], "MultipartError: bad boundary")

    def test_missing_key_is_none_not_crash(self):
        self.assertIsNone(self.jobs.get("nope"))
        # 对未知 key 的写入必须是静默无操作（转换中途超时之类）
        self.jobs.add("nope", {"ok": True})
        self.jobs.phase("nope", "converting")
        self.jobs.finish("nope")
        self.assertEqual(self.jobs.size(), 1)

    def test_snapshot_is_a_copy(self):
        self.jobs.add("j1", {"source": "a.ncm", "ok": True})
        snap = self.jobs.get("j1")
        snap["items"].append({"source": "fake"})
        snap["done"] = 99
        again = self.jobs.get("j1")
        self.assertEqual(again["done"], 1)
        self.assertEqual(len(again["items"]), 1)

    def test_discard(self):
        self.jobs.discard("j1")
        self.assertIsNone(self.jobs.get("j1"))
        self.assertEqual(self.jobs.size(), 0)


class TestReclaim(unittest.TestCase):
    """不回收的话，长期运行的服务会把这些 dict 攒到 OOM。"""

    def test_expired_entries_dropped_on_next_start(self):
        clock = Clock()
        jobs = JobRegistry(clock=clock, ttl=60)
        jobs.start("old", total=1)
        jobs.finish("old")
        clock.advance(61)
        jobs.start("new", total=1)
        self.assertIsNone(jobs.get("old"))
        self.assertIsNotNone(jobs.get("new"))
        self.assertEqual(jobs.size(), 1)

    def test_unfinished_entries_are_not_expired(self):
        clock = Clock()
        jobs = JobRegistry(clock=clock, ttl=60)
        jobs.start("running", total=1)
        clock.advance(10_000)  # 卡住的转换：不能因为「旧」就丢
        jobs.start("new", total=1)
        self.assertIsNotNone(jobs.get("running"))

    def test_limit_drops_oldest_finished_first(self):
        clock = Clock()
        jobs = JobRegistry(clock=clock, ttl=10_000, limit=3)
        jobs.start("running", total=1)  # 未完成，最「旧」
        for name in ("a", "b", "c"):
            clock.advance(1)
            jobs.start(name, total=1)
            jobs.finish(name)
        self.assertEqual(jobs.size(), 3)  # a/b/c + running 触发了淘汰

        names = [n for n in ("running", "a", "b", "c") if jobs.get(n) is not None]
        # 淘汰优先级：已完成且最旧 → a 先走；未完成的 running 留着
        self.assertNotIn("a", names)
        self.assertIn("running", names)


if __name__ == "__main__":
    unittest.main()
