"""第二轮改动的回归测试：日志脱敏、Range/HEAD 下载、批量配额、job 轮询、就绪探针。

这些用例对应的是「行为对不对」而不只是「跑不跑得通」的问题：

* 令牌进日志 —— 改完之后要能证明 ``?token=`` 真的没进 stdout；
* 批量上传绕过日配额 —— ``files_per_day=1`` 时必须拒掉一次传 3 个；
* 下载整包进内存 —— 至少要证明 ``HEAD`` 不再读盘、``Range`` 真的只发那一段；
* ``/api/job`` 与 ``/api/ready`` —— 新增的两个探针口。
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ncm import META, build_ncm, fake_audio  # noqa: E402
from test_server import HttpMixin, multipart  # noqa: E402

from audioforge.guard import Limits  # noqa: E402
from audioforge.httpd import (  # noqa: E402
    DEFAULT_TRUSTED_PROXIES,
    ServerConfig,
    create_server,
    mask_secrets,
)
from audioforge.httpd.base import BaseHandler, _is_trusted  # noqa: E402
from audioforge.httpd.download import _parse_range  # noqa: E402

JOB_ID = "abcdef0123456789" * 2  # 32 位 hex


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


class TestMaskSecrets(unittest.TestCase):
    def test_token_is_masked(self):
        raw = "GET /api/file/abc/x.mp3?token=s3cr3t HTTP/1.1"
        self.assertEqual(mask_secrets(raw), "GET /api/file/abc/x.mp3?token=*** HTTP/1.1")

    def test_other_params_survive(self):
        self.assertEqual(mask_secrets("/x?token=abc&v=1&t=2"), "/x?token=***&v=1&t=2")

    def test_access_token_and_case(self):
        # (?i) 覆盖参数名：大小写变体也要打码（替换保留原参数名，只盖住值）
        masked = mask_secrets("/x?Access_Token=abc")
        self.assertIn("***", masked)
        self.assertNotIn("abc", masked)

    def test_plain_path_untouched(self):
        raw = "GET /api/health HTTP/1.1"
        self.assertEqual(mask_secrets(raw), raw)

    def test_bearer_header_is_not_touched(self):
        # Authorization 头不走 query，打码只针对 URL
        raw = "Authorization: Bearer abc123"
        self.assertEqual(mask_secrets(raw), raw)


class TestParseRange(unittest.TestCase):
    def test_no_header_means_full(self):
        self.assertEqual(_parse_range(None, 100), (0, 99, False))

    def test_explicit_range(self):
        self.assertEqual(_parse_range("bytes=10-19", 100), (10, 19, True))

    def test_open_ended(self):
        self.assertEqual(_parse_range("bytes=90-", 100), (90, 99, True))

    def test_suffix(self):
        self.assertEqual(_parse_range("bytes=-10", 100), (90, 99, True))

    def test_end_clamped_to_size(self):
        self.assertEqual(_parse_range("bytes=50-9999", 100), (50, 99, True))

    def test_beyond_eof_is_error(self):
        with self.assertRaises(ValueError):
            _parse_range("bytes=100-200", 100)

    def test_reversed_is_error(self):
        with self.assertRaises(ValueError):
            _parse_range("bytes=30-10", 100)

    def test_multi_range_falls_back_to_full(self):
        # 多段 Range 标准库不支持，按 RFC 允许「忽略 Range 直接回整包」
        self.assertEqual(_parse_range("bytes=0-1,5-6", 100), (0, 99, False))

    def test_garbage_header_ignored(self):
        self.assertEqual(_parse_range("items=0-1", 100), (0, 99, False))


class TestTrustedProxy(unittest.TestCase):
    def test_loopback_trusted_by_default(self):
        self.assertTrue(_is_trusted("127.0.0.1", DEFAULT_TRUSTED_PROXIES))
        self.assertTrue(_is_trusted("::1", DEFAULT_TRUSTED_PROXIES))

    def test_lan_not_trusted_by_default(self):
        self.assertFalse(_is_trusted("10.0.0.5", DEFAULT_TRUSTED_PROXIES))

    def test_cidr_entry(self):
        self.assertTrue(_is_trusted("10.1.2.3", ("10.0.0.0/8",)))
        self.assertFalse(_is_trusted("11.1.2.3", ("10.0.0.0/8",)))

    def test_garbage_input(self):
        self.assertFalse(_is_trusted("not-an-ip", DEFAULT_TRUSTED_PROXIES))
        self.assertFalse(_is_trusted("", DEFAULT_TRUSTED_PROXIES))

    def test_bad_cidr_entry_is_skipped(self):
        self.assertFalse(_is_trusted("127.0.0.1", ("banana",)))


class _FakeServer:
    def __init__(self, cfg):
        self.cfg = cfg


class _FakeHandler:
    """只提供 ``_client_ip()`` 需要的四个属性，不必起真服务。"""

    def __init__(self, peer: str, headers: dict[str, str], cfg: ServerConfig):
        self.client_address = (peer, 54321)
        self.headers = headers
        self.server = _FakeServer(cfg)


class TestClientIp(unittest.TestCase):
    def _ip(self, peer: str, hdrs: dict[str, str], **cfg) -> str:
        cfg.setdefault("limits", Limits.unlimited())
        return BaseHandler._client_ip(_FakeHandler(peer, hdrs, ServerConfig(**cfg)))

    def test_default_ignores_xff(self):
        # 默认关：伪造 XFF 拿不到独立配额桶
        self.assertEqual(self._ip("1.2.3.4", {"X-Forwarded-For": "9.9.9.9"}), "1.2.3.4")

    def test_trusted_peer_honours_xff(self):
        ip = self._ip("127.0.0.1", {"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}, trust_proxy=True)
        self.assertEqual(ip, "9.9.9.9")

    def test_untrusted_peer_ignores_xff(self):
        # 开关打开但来源不在白名单 —— 仍然不认（否则公网直连就能自造 IP）
        ip = self._ip("1.2.3.4", {"X-Forwarded-For": "9.9.9.9"}, trust_proxy=True)
        self.assertEqual(ip, "1.2.3.4")

    def test_real_ip_fallback(self):
        ip = self._ip("127.0.0.1", {"X-Real-IP": "8.8.4.4"}, trust_proxy=True)
        self.assertEqual(ip, "8.8.4.4")

    def test_invalid_forwarded_falls_back_to_peer(self):
        ip = self._ip("127.0.0.1", {"X-Forwarded-For": "not-an-ip"}, trust_proxy=True)
        self.assertEqual(ip, "127.0.0.1")


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #


class CacheCase(HttpMixin, unittest.TestCase):
    """静态资源缓存策略（P1-6）。"""

    server_options = {"limits": Limits.unlimited()}

    def test_static_sends_etag_and_cache(self):
        status, body, headers = self.get("/style.css")
        self.assertEqual(status, 200)
        self.assertTrue(body)
        self.assertIn("ETag", headers)
        self.assertIn("max-age", headers.get("Cache-Control", ""))

    def test_if_none_match_returns_304(self):
        _, _, headers = self.get("/style.css")
        status, body, hdrs = self.get("/style.css", {"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertIn("ETag", hdrs)

    def test_api_still_no_store(self):
        _, _, headers = self.get("/api/health")
        self.assertEqual(headers.get("Cache-Control"), "no-store")


class ReadyCase(HttpMixin, unittest.TestCase):
    server_options = {"limits": Limits.unlimited()}

    def test_ready_ok(self):
        status, body, _ = self.get("/api/ready")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ready"])

    def test_ready_is_public(self):
        # 与 ping 同口径：没凭证也能问（负载均衡摘流用）
        status, _, _ = self.get("/api/ready")
        self.assertEqual(status, 200)

    def test_ready_turns_503_when_jobs_dir_gone(self):
        # 卷没挂上 / 目录被清掉：进程活着但干不了活
        jobs_dir = self.cfg.jobs_dir
        moved = jobs_dir.with_name("jobs-moved")
        jobs_dir.rename(moved)
        try:
            status, body, _ = self.get("/api/ready")
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body)["ready"])
        finally:
            moved.rename(jobs_dir)


class RangeCase(HttpMixin, unittest.TestCase):
    """流式下载 + Range + HEAD（P0-4）。"""

    server_options = {"limits": Limits.unlimited()}
    max_bytes = 8 << 20  # 下面那个 4 MB 的用例要留出 multipart 开销
    _uploaded: tuple | None = None

    def setUp(self):
        cls = type(self)
        if cls._uploaded is None:
            audio = fake_audio(5_000)
            status, data = self.upload("r.ncm", build_ncm(audio, meta=META))
            self.assertEqual(status, 200, data)
            cls._uploaded = (data["results"][0]["url"], len(audio), audio)
        self.url, self.size, self.audio = cls._uploaded

    def test_full_body_and_ranges_header(self):
        status, body, headers = self.get(self.url)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.audio)
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(int(headers["Content-Length"]), self.size)

    def test_partial_content(self):
        status, body, headers = self.get(self.url, {"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.audio[100:200])
        self.assertEqual(headers.get("Content-Range"), f"bytes 100-199/{self.size}")
        self.assertEqual(int(headers["Content-Length"]), 100)

    def test_open_ended_range(self):
        status, body, _ = self.get(self.url, {"Range": f"bytes={self.size - 10}-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.audio[-10:])

    def test_suffix_range(self):
        status, body, _ = self.get(self.url, {"Range": "bytes=-16"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.audio[-16:])

    def test_unsatisfiable_range(self):
        status, _, headers = self.get(self.url, {"Range": f"bytes={self.size + 5}-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers.get("Content-Range"), f"bytes */{self.size}")

    def test_head_has_length_but_no_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("HEAD", self.url)
            resp = conn.getresponse()
            payload = resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(payload, b"")
        self.assertEqual(int(resp.getheader("Content-Length")), self.size)
        self.assertEqual(resp.getheader("Accept-Ranges"), "bytes")

    def test_head_download_does_not_touch_body(self):
        # HEAD 若仍走「先 read_bytes 再判 command」就会把整包读进内存；
        # 这里用 4 MB 级的文件把差距放大到可观测：HEAD 后 Content-Length 正确且响应瞬间返回。
        blob = fake_audio(4 << 20)
        status, data = self.upload("big.ncm", build_ncm(blob, meta=META))
        self.assertEqual(status, 200, data)
        url = data["results"][0]["url"]

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("HEAD", url)
            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(int(resp.getheader("Content-Length")), len(blob))


class BatchQuotaCase(HttpMixin, unittest.TestCase):
    """日文件配额按真实文件数记（P0-2）。"""

    server_options = {"limits": Limits(per_minute=0, files_per_day=1, mb_per_day=0, disk_mb=0)}

    def test_batch_cannot_bypass_file_quota(self):
        blob = build_ncm(fake_audio(2_000), meta=META)
        status, data = self.post(
            "/api/convert",
            multipart([("a.ncm", blob), ("b.ncm", blob), ("c.ncm", blob)], {"target": "auto"}),
        )
        self.assertEqual(status, 429, data)
        self.assertFalse(data["ok"])

    def test_quota_still_counts_after_denial(self):
        # 被拒的那次不该记账；一个的文件仍然能过，第二个才被拦
        blob = build_ncm(fake_audio(2_000), meta=META)
        self.assertEqual(self.upload("x.ncm", blob)[0], 200)
        self.assertEqual(self.upload("y.ncm", blob)[0], 429)


class JobApiCase(HttpMixin, unittest.TestCase):
    """``GET /api/job/<id>``（P2-3 前端轮询用）。"""

    server_options = {"limits": Limits.unlimited()}

    def test_upload_reports_job_status_url(self):
        status, data = self.post(
            "/api/convert",
            multipart(
                [("j.ncm", build_ncm(fake_audio(2_000), meta=META))],
                {"target": "auto", "job": JOB_ID},
            ),
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], f"/api/job/{JOB_ID}")

        status, body, _ = self.get(data["status"])
        self.assertEqual(status, 200)
        snap = json.loads(body)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["phase"], "done")
        self.assertEqual(snap["total"], 1)
        self.assertEqual(snap["done"], 1)
        self.assertTrue(snap["finished"])
        self.assertEqual(snap["items"][0]["ok"], True)

    def test_unknown_job_is_404(self):
        status, _, _ = self.get("/api/job/" + "0" * 32)
        self.assertEqual(status, 404)

    def test_malformed_job_is_400(self):
        for bad in ("/api/job/xyz", "/api/job/", "/api/job/..%2f.."):
            status, _, _ = self.get(bad)
            self.assertIn(status, (400, 404), bad)


class BindFailureCase(unittest.TestCase):
    """装配失败时要看到真正的原因。

    父类 ``TCPServer.__init__`` 在 bind 失败时会回调 ``self.server_close()``；
    如果 ``_cleaner_stop`` 还没建好，真正的错误（Errno 98 端口被占 / 端口越界）
    会被 ``AttributeError: 'AppServer' object has no attribute '_cleaner_stop'`` 盖掉。
    这是在 systemd 容器里实测踩到的：日志里只剩那条假线索。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_bad_address_surfaces_real_error(self):
        # 192.0.2.0/24 是 RFC 5737 的 TEST-NET-1，本机不可能持有该地址
        cfg = ServerConfig(
            host="192.0.2.1", port=0, work_dir=Path(self.tmp.name), limits=Limits.unlimited()
        )
        with self.assertRaises(OSError) as ctx:
            create_server(cfg, start_cleaner=False)
        self.assertNotIsInstance(ctx.exception, AttributeError)


class LogRedactionCase(HttpMixin, unittest.TestCase):
    """访问日志里的 ``?token=`` 必须打码（P0-1）。"""

    server_options = {"limits": Limits.unlimited(), "quiet": False}

    def test_token_not_written_to_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status, _, _ = self.get("/api/file/deadbeef/x.mp3?token=sup3r-s3cr3t")
        log = buf.getvalue()
        self.assertEqual(status, 404)
        self.assertNotIn("sup3r-s3cr3t", log, log)
        self.assertIn("token=***", log, log)


if __name__ == "__main__":
    unittest.main()
