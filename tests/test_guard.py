"""访问控制自测：Basic / Bearer / 限速 / 日配额 / 磁盘闸门。

全部用注入的假时钟，不依赖真实等待。
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge.guard import Denied, Guard, Limits  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def basic(user: str, pwd: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


def make(**kw) -> tuple[Guard, FakeClock]:
    clock = FakeClock()
    kw.setdefault("limits", Limits.unlimited())
    return Guard(clock=clock, **kw), clock


class TestAuthModes(unittest.TestCase):
    def test_no_auth_by_default(self):
        g, _ = make()
        self.assertEqual(g.mode, "none")
        self.assertFalse(g.requires_auth)
        g.verify({})                      # 不抛即通过

    def test_basic_mode(self):
        g, _ = make(user="alice", password="s3cret")
        self.assertEqual(g.mode, "basic")
        g.verify({"Authorization": basic("alice", "s3cret")})

    def test_basic_wrong_password(self):
        g, _ = make(user="alice", password="s3cret")
        with self.assertRaises(Denied) as ctx:
            g.verify({"Authorization": basic("alice", "wrong")})
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("WWW-Authenticate", ctx.exception.headers)

    def test_basic_wrong_user(self):
        g, _ = make(user="alice", password="s3cret")
        with self.assertRaises(Denied):
            g.verify({"Authorization": basic("bob", "s3cret")})

    def test_basic_missing_header(self):
        g, _ = make(user="alice", password="s3cret")
        with self.assertRaises(Denied) as ctx:
            g.verify({})
        self.assertIn("charset=\"UTF-8\"", ctx.exception.headers["WWW-Authenticate"])

    def test_basic_malformed_base64(self):
        g, _ = make(user="alice", password="s3cret")
        with self.assertRaises(Denied):
            g.verify({"Authorization": "Basic !!!not-base64!!!"})

    def test_basic_without_colon(self):
        g, _ = make(user="alice", password="s3cret")
        payload = base64.b64encode(b"aliceonly").decode()
        with self.assertRaises(Denied):
            g.verify({"Authorization": "Basic " + payload})

    def test_password_may_contain_colon(self):
        g, _ = make(user="alice", password="a:b:c")
        g.verify({"Authorization": basic("alice", "a:b:c")})

    def test_bearer_mode(self):
        g, _ = make(token="tok123")
        self.assertEqual(g.mode, "bearer")
        g.verify({"Authorization": "Bearer tok123"})
        g.verify({"X-Auth-Token": "tok123"})
        g.verify({"Authorization": "bearer tok123"})     # 前缀大小写不敏感

    def test_bearer_wrong(self):
        g, _ = make(token="tok123")
        with self.assertRaises(Denied) as ctx:
            g.verify({"Authorization": "Bearer nope"})
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.headers, {})      # 没有 basic 就不发挑战头

    def test_combined_is_or(self):
        g, _ = make(user="alice", password="pw", token="tok")
        self.assertEqual(g.mode, "basic+bearer")
        g.verify({"Authorization": basic("alice", "pw")})
        g.verify({"Authorization": "Bearer tok"})
        g.verify({"X-Auth-Token": "tok"})
        with self.assertRaises(Denied):
            g.verify({"Authorization": "Bearer bad"})

    def test_empty_token_does_not_enable(self):
        g, _ = make(token="")
        self.assertEqual(g.mode, "none")


class TestRate(unittest.TestCase):
    def test_limit_and_reset(self):
        g, clock = make(limits=Limits(per_minute=2))
        g.check_rate("1.1.1.1")
        g.check_rate("1.1.1.1")
        with self.assertRaises(Denied) as ctx:
            g.check_rate("1.1.1.1")
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("Retry-After", ctx.exception.headers)

        clock.advance(61)
        g.check_rate("1.1.1.1")           # 窗口滚动后可继续

    def test_per_ip_isolation(self):
        g, _ = make(limits=Limits(per_minute=1))
        g.check_rate("1.1.1.1")
        g.check_rate("2.2.2.2")           # 不同 IP 不共享额度
        with self.assertRaises(Denied):
            g.check_rate("1.1.1.1")

    def test_zero_means_unlimited(self):
        g, _ = make(limits=Limits(per_minute=0))
        for _ in range(50):
            g.check_rate("x")


class TestQuota(unittest.TestCase):
    def test_files_per_day(self):
        g, clock = make(limits=Limits(files_per_day=2))
        g.check_quota("ip", n_files=1)
        g.record("ip", 1, 0)
        g.check_quota("ip", n_files=1)
        g.record("ip", 1, 0)
        with self.assertRaises(Denied) as ctx:
            g.check_quota("ip", n_files=1)
        self.assertEqual(ctx.exception.status, 429)
        clock.advance(86401)
        g.check_quota("ip", n_files=1)    # 跨日重置

    def test_bytes_per_day(self):
        g, _ = make(limits=Limits(mb_per_day=10))
        one_mb = 1 << 20
        g.record("ip", 1, 9 * one_mb)
        g.check_quota("ip", n_bytes=one_mb)
        with self.assertRaises(Denied):
            g.check_quota("ip", n_bytes=2 * one_mb)

    def test_usage_snapshot(self):
        g, _ = make(limits=Limits(files_per_day=10, mb_per_day=100))
        g.record("ip", 3, 5 << 20)
        snap = g.usage("ip")
        self.assertEqual(snap["files_today"], 3)
        self.assertEqual(snap["mb_today"], 5.0)

    def test_limits_dict(self):
        self.assertEqual(Limits.unlimited().as_dict(),
                         {"per_minute": 0, "files_per_day": 0, "mb_per_day": 0, "disk_mb": 0})


class TestDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jobs.mkdir()

    def _job(self, name: str, mb: int) -> Path:
        d = self.jobs / name
        d.mkdir()
        (d / "blob.bin").write_bytes(b"x" * (mb << 20))
        return d

    def test_noop_under_limit(self):
        self._job("a", 1)
        g, _ = make(limits=Limits(disk_mb=10), jobs_dir=self.jobs)
        self.assertAlmostEqual(g.disk_usage_mb(), 1.0, places=1)
        self.assertEqual(g.ensure_disk(), 0)

    def test_prunes_oldest_first(self):
        import os
        import time

        old = self._job("old", 2)
        time.sleep(0.02)
        mid = self._job("mid", 2)
        time.sleep(0.02)
        new = self._job("new", 2)
        # 让 mtime 明确有先后，避免文件系统精度问题
        base = time.time() - 100
        for i, d in enumerate((old, mid, new)):
            os.utime(d, (base + i * 10, base + i * 10))

        g, _ = make(limits=Limits(disk_mb=4), jobs_dir=self.jobs)
        freed = g.ensure_disk()
        self.assertGreater(freed, 0)
        self.assertFalse(old.exists())          # 最旧的一定先走
        self.assertTrue(new.exists())           # 最新的留着
        self.assertLessEqual(g.disk_usage_mb(), 4)

    def test_disabled_when_zero(self):
        self._job("a", 3)
        g, _ = make(limits=Limits(disk_mb=0), jobs_dir=self.jobs)
        self.assertEqual(g.ensure_disk(), 0)
        self.assertTrue((self.jobs / "a").exists())


if __name__ == "__main__":
    unittest.main()
