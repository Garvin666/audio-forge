"""HTTP 层端到端自测：静态资源、探活、能力探测、上传转换、下载、鉴权、限速、错误码。

每个测试类在随机端口上起一个真实服务（``port=0`` 由系统分配），互不干扰。
通用转码用例在检测不到 ffmpeg 时自动跳过。
"""

from __future__ import annotations

import base64
import http.client
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ncm import META, build_ncm, fake_audio  # noqa: E402

from audioforge.guard import Limits  # noqa: E402
from audioforge.server import ServerConfig, create_server  # noqa: E402
from audioforge.transcode import find_ffmpeg  # noqa: E402

BOUNDARY = "----audioforgeHttpTest"


def multipart(files: list[tuple[str, bytes]], fields: dict[str, str]) -> bytes:
    out = bytearray()
    for name, value in fields.items():
        out += b"--" + BOUNDARY.encode() + b"\r\n"
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for filename, data in files:
        out += b"--" + BOUNDARY.encode() + b"\r\n"
        out += (
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        out += data + b"\r\n"
    out += b"--" + BOUNDARY.encode() + b"--\r\n"
    return bytes(out)


def basic(user: str, pwd: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


class HttpMixin:
    """起服务 + 发请求的公共部分。子类用 ``server_options`` 配置鉴权与配额。"""

    max_bytes = 4 << 20
    server_options: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        opts = dict(max_bytes=cls.max_bytes, ttl=600, quiet=True,
                    work_dir=Path(cls.tmp.name))
        opts.update(cls.server_options)
        cls.cfg = ServerConfig(port=0, **opts)
        cls.httpd = create_server(cls.cfg)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path: str, headers: dict | None = None) -> tuple[int, bytes, dict]:
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, path: str, body: bytes, headers: dict | None = None) -> tuple[int, dict]:
        hdrs = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    def upload(self, name: str, blob: bytes, target: str = "auto",
               headers: dict | None = None) -> tuple[int, dict]:
        return self.post("/api/convert", multipart([(name, blob)], {"target": target}), headers)


class ServerCase(HttpMixin, unittest.TestCase):
    """默认配置：无鉴权、不限速。"""

    server_options = {"limits": Limits.unlimited()}

    # ---------- 静态与能力 ---------- #

    def test_index_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"audio-forge", body)

    def test_static_assets(self):
        for name in ("/app.js", "/style.css"):
            status, body, _ = self.get(name)
            self.assertEqual(status, 200, name)
            self.assertTrue(body, name)

    def test_health_reports_ncm(self):
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["capabilities"]["ncm"])
        self.assertIn("mp3", data["formats"])
        self.assertEqual(data["auth"]["mode"], "none")

    def test_unknown_route_404(self):
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)

    # ---------- 上传转换 ---------- #

    def test_ncm_upload_and_download(self):
        audio = fake_audio(60_000)
        blob = build_ncm(audio, meta=META, cover=b"")
        status, data = self.upload("test.ncm", blob)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["results"]), 1)

        row = data["results"][0]
        self.assertTrue(row["ok"], row)
        self.assertEqual(row["name"], "test.mp3")
        self.assertEqual(row["size"], len(audio))
        self.assertEqual(row["meta"]["title"], "测试曲目")

        status, payload, headers = self.get(row["url"])
        self.assertEqual(status, 200)
        self.assertEqual(payload, audio)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_two_files_at_once(self):
        a = build_ncm(fake_audio(20_000, seed=1), meta=META)
        b = build_ncm(fake_audio(30_000, seed=2), meta=META)
        status, data = self.post(
            "/api/convert",
            multipart([("a.ncm", a), ("b.ncm", b)], {"target": "auto"}),
        )
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in data["results"]], ["a.mp3", "b.mp3"])

    def test_bad_file_reports_per_file_error(self):
        good = build_ncm(fake_audio(10_000), meta=META)
        status, data = self.post(
            "/api/convert",
            multipart([("bad.ncm", b"definitely not ncm"), ("ok.ncm", good)], {"target": "auto"}),
        )
        self.assertEqual(status, 200)
        rows = {r["source"]: r for r in data["results"]}
        self.assertFalse(rows["bad.ncm"]["ok"])
        self.assertIn("NcmError", rows["bad.ncm"]["error"])
        self.assertTrue(rows["ok.ncm"]["ok"])

    def test_missing_files_field(self):
        status, data = self.post("/api/convert", multipart([], {"target": "mp3"}))
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_oversized_request_rejected(self):
        # 服务端读完 Content-Length 就拒绝，不会再读 body。客户端此时可能拿到 413，
        # 也可能在继续发送时被断开（Linux 上更常见），两种都算「拒绝」。
        body = multipart([("big.ncm", b"\x00" * (self.max_bytes + 1))], {"target": "auto"})
        try:
            status, data = self.post("/api/convert", body)
        except (urllib.error.URLError, ConnectionResetError, BrokenPipeError):
            return
        self.assertEqual(status, 413)
        self.assertFalse(data["ok"])

    # ---------- 下载安全 ---------- #

    def test_download_traversal_blocked(self):
        status, _, _ = self.get("/api/file/abc/..%2f..%2fbody.part")
        self.assertEqual(status, 404)

    def test_download_unknown_job(self):
        status, _, _ = self.get("/api/file/deadbeef/x.mp3")
        self.assertEqual(status, 404)

    # ---------- 可选：通用转码 ---------- #

    @unittest.skipUnless(find_ffmpeg(), "未安装 ffmpeg，跳过通用转码用例")
    def test_transcode_requires_target(self):
        wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
        status, data = self.upload("x.wav", wav, target="auto")
        self.assertEqual(status, 200)
        self.assertFalse(data["results"][0]["ok"])
        self.assertIn("目标格式", data["results"][0]["error"])


class AuthCase(HttpMixin, unittest.TestCase):
    """同时启用 Basic 与 Bearer。"""

    server_options = {
        "auth_user": "alice",
        "auth_pass": "s3cret",
        "access_token": "tok-123",
        "limits": Limits.unlimited(),
    }

    def test_api_requires_auth(self):
        status, body, headers = self.get("/api/health")
        self.assertEqual(status, 401)
        self.assertIn("Basic realm=", headers.get("WWW-Authenticate", ""))
        self.assertFalse(json.loads(body)["ok"])

    def test_api_with_basic(self):
        status, body, _ = self.get("/api/health", {"Authorization": basic("alice", "s3cret")})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["auth"]["mode"], "basic+bearer")

    def test_api_with_bearer(self):
        status, _, _ = self.get("/api/health", {"Authorization": "Bearer tok-123"})
        self.assertEqual(status, 200)

    def test_api_with_wrong_bearer(self):
        status, _, _ = self.get("/api/health", {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_ping_is_public(self):
        # 容器 healthcheck 走这里，配了鉴权也必须 200，且不能带任何明细
        status, body, _ = self.get("/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_static_is_public(self):
        # 页面本身必须能加载，否则 Bearer 模式拿不到令牌
        for path in ("/", "/app.js", "/style.css"):
            status, _, _ = self.get(path)
            self.assertEqual(status, 200, path)

    def test_upload_requires_auth(self):
        blob = build_ncm(fake_audio(5_000), meta=META)
        status, data = self.upload("x.ncm", blob)
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_unauthorized_post_drains_body(self):
        # 401 时若不读掉 body，残留的分段字节会被当成下一条请求行（400 + 走私口子）。
        # 用同一条 keep-alive 连接复验：读干净了，第二个请求才能正常拿到 200。
        body = multipart([("x.ncm", build_ncm(fake_audio(5_000), meta=META))], {"target": "auto"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", "/api/convert", body=body,
                         headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 401)

            conn.request("GET", "/api/ping")
            second = conn.getresponse()
            payload = second.read()
            self.assertEqual(second.status, 200, payload)
        finally:
            conn.close()

    def test_upload_with_bearer(self):
        blob = build_ncm(fake_audio(5_000), meta=META)
        status, data = self.upload("x.ncm", blob, headers={"Authorization": "Bearer tok-123"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["results"][0]["ok"])

    def test_download_accepts_query_token(self):
        audio = fake_audio(5_000)
        status, data = self.upload(
            "q.ncm", build_ncm(audio, meta=META),
            headers={"Authorization": "Bearer tok-123"},
        )
        self.assertEqual(status, 200, data)
        url = data["results"][0]["url"]

        self.assertEqual(self.get(url)[0], 401)
        status, payload, _ = self.get(url + "?token=tok-123")
        self.assertEqual(status, 200)
        self.assertEqual(payload, audio)
        self.assertEqual(self.get(url + "?token=wrong")[0], 401)


class RateCase(HttpMixin, unittest.TestCase):
    """每分钟 3 次，其余不限制。"""

    server_options = {"limits": Limits(per_minute=3, files_per_day=0, mb_per_day=0, disk_mb=0)}

    def test_rate_limit(self):
        codes = [self.get("/api/health")[0] for _ in range(4)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)

    def test_retry_after_header(self):
        for _ in range(3):
            self.get("/api/health")
        status, body, headers = self.get("/api/health")
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertIn("频繁", json.loads(body)["error"])


class PingCase(HttpMixin, unittest.TestCase):
    """探活口：每分钟只给 1 次额度，探针也不该把它吃光。"""

    server_options = {"limits": Limits(per_minute=1, files_per_day=0, mb_per_day=0, disk_mb=0)}

    def test_ping_never_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.get("/api/ping")[0], 200)

    def test_ping_does_not_consume_budget(self):
        for _ in range(5):
            self.assertEqual(self.get("/api/ping")[0], 200)
        self.assertEqual(self.get("/api/health")[0], 200)


class QuotaCase(HttpMixin, unittest.TestCase):
    """每 IP 每天 1 个文件。"""

    server_options = {"limits": Limits(per_minute=0, files_per_day=1, mb_per_day=0, disk_mb=0)}

    def test_file_quota(self):
        first = self.upload("one.ncm", build_ncm(fake_audio(4_000), meta=META))
        self.assertEqual(first[0], 200)
        self.assertTrue(first[1]["results"][0]["ok"])

        second = self.upload("two.ncm", build_ncm(fake_audio(4_000), meta=META))
        self.assertEqual(second[0], 429)
        self.assertIn("配额", second[1]["error"])


class ByteQuotaCase(HttpMixin, unittest.TestCase):
    """每 IP 每天 1 MB 上传量。"""

    server_options = {"limits": Limits(per_minute=0, files_per_day=0, mb_per_day=1, disk_mb=0)}

    def test_byte_quota(self):
        big = build_ncm(fake_audio(2 << 20), meta=META)     # 2 MB，超出 1 MB 配额
        status, data = self.upload("big.ncm", big)
        self.assertEqual(status, 429)
        self.assertIn("配额", data["error"])


if __name__ == "__main__":
    unittest.main()
