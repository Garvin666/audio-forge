"""HTTP 层端到端自测：静态资源、能力探测、上传转换、下载、错误码。

跑在随机端口上（``port=0`` 由系统分配），不占用固定端口、不依赖 ffmpeg。
通用转码相关用例在检测不到 ffmpeg 时自动跳过。
"""

from __future__ import annotations

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

from audioforge.server import ServerConfig, create_server  # noqa: E402
from audioforge.transcode import find_ffmpeg  # noqa: E402
from test_ncm import META, build_ncm, fake_audio  # noqa: E402

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
        ).encode("utf-8")
        out += data + b"\r\n"
    out += b"--" + BOUNDARY.encode() + b"--\r\n"
    return bytes(out)


class ServerCase(unittest.TestCase):
    max_bytes = 4 << 20

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = ServerConfig(
            port=0, work_dir=Path(cls.tmp.name), quiet=True,
            max_bytes=cls.max_bytes, ttl=600,
        )
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

    def get(self, path: str) -> tuple[int, bytes, dict]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, path: str, body: bytes) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw.decode("utf-8", "replace")}

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

    def test_unknown_route_404(self):
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)

    # ---------- 上传转换 ---------- #

    def test_ncm_upload_and_download(self):
        audio = fake_audio(60_000)
        blob = build_ncm(audio, meta=META, cover=b"")
        status, data = self.post("/api/convert", multipart([("test.ncm", blob)], {"target": "auto"}))
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
        status, data = self.post("/api/convert", multipart([("x.wav", wav)], {"target": "auto"}))
        self.assertEqual(status, 200)
        self.assertFalse(data["results"][0]["ok"])
        self.assertIn("目标格式", data["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
