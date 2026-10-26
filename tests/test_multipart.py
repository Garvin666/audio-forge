"""multipart 解析器自测：字段、文件、中文名、恶意文件名、异常输入。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge.multipart import (  # noqa: E402
    MultipartError,
    get_boundary,
    parse_multipart,
    sanitize_filename,
)

BOUNDARY = b"----audioforgeTestBoundary"


def build_body(parts: list[dict], closing: bool = True) -> bytes:
    out = bytearray()
    for p in parts:
        out += b"--" + BOUNDARY + b"\r\n"
        disp = f'Content-Disposition: form-data; name="{p["name"]}"'
        if "filename" in p:
            disp += f'; filename="{p["filename"]}"'
        out += disp.encode("utf-8") + b"\r\n"
        if "ctype" in p:
            out += f'Content-Type: {p["ctype"]}\r\n'.encode()
        out += b"\r\n"
        out += p["data"]
        out += b"\r\n"
    out += b"--" + BOUNDARY + (b"--" if closing else b"") + b"\r\n"
    return bytes(out)


class TestBoundary(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(get_boundary("multipart/form-data; boundary=abc"), b"abc")

    def test_quoted(self):
        self.assertEqual(get_boundary('multipart/form-data; boundary="a b c"'), b"a b c")

    def test_missing(self):
        with self.assertRaises(MultipartError):
            get_boundary("application/json")

    def test_no_boundary_value(self):
        with self.assertRaises(MultipartError):
            get_boundary("multipart/form-data")


class TestSanitize(unittest.TestCase):
    def test_strips_directories(self):
        self.assertEqual(sanitize_filename("../../etc/passwd.mp3"), "passwd.mp3")
        self.assertEqual(sanitize_filename(r"C:\Windows\x.mp3"), "x.mp3")

    def test_removes_control_and_wildcard(self):
        self.assertEqual(sanitize_filename('a<>:"|?*.flac'), "a_______.flac")

    def test_keeps_unicode(self):
        self.assertEqual(sanitize_filename("夜曲 - 周杰伦.mp3"), "夜曲 - 周杰伦.mp3")

    def test_empty_falls_back(self):
        self.assertEqual(sanitize_filename("", fallback="up"), "up")
        self.assertEqual(sanitize_filename("...", fallback="up"), "up")

    def test_long_stem_truncated(self):
        name = "a" * 300 + ".mp3"
        out = sanitize_filename(name)
        self.assertTrue(out.endswith(".mp3"))
        self.assertLessEqual(len(out), 120)


class TestParse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.spool = self.dir / "body.bin"
        self.dest = self.dir / "files"

    def _parse(self, body: bytes):
        self.spool.write_bytes(body)
        return parse_multipart(self.spool, BOUNDARY, self.dest)

    def test_field_and_file(self):
        body = build_body([
            {"name": "target", "data": b"mp3"},
            {"name": "files", "filename": "song.ncm", "ctype": "application/octet-stream",
             "data": b"\x00\x01\x02" * 100},
        ])
        parts = self._parse(body)
        self.assertEqual(len(parts), 2)

        field = next(p for p in parts if p.name == "target")
        self.assertFalse(field.is_file)
        self.assertEqual(field.text(), "mp3")

        f = next(p for p in parts if p.is_file)
        self.assertEqual(f.filename, "song.ncm")
        self.assertEqual(f.size, 300)
        self.assertEqual(f.path.read_bytes(), b"\x00\x01\x02" * 100)

    def test_multiple_files_keep_names(self):
        body = build_body([
            {"name": "files", "filename": "a.mp3", "data": b"A" * 10},
            {"name": "files", "filename": "b.flac", "data": b"B" * 20},
        ])
        parts = self._parse(body)
        self.assertEqual([p.filename for p in parts], ["a.mp3", "b.flac"])
        self.assertEqual(parts[0].path.name, "a.mp3")
        self.assertEqual(parts[1].path.read_bytes(), b"B" * 20)

    def test_duplicate_names_get_suffix(self):
        body = build_body([
            {"name": "files", "filename": "same.mp3", "data": b"A"},
            {"name": "files", "filename": "same.mp3", "data": b"B"},
        ])
        parts = self._parse(body)
        self.assertNotEqual(parts[0].path, parts[1].path)
        self.assertEqual(parts[1].path.name, "same-1.mp3")

    def test_unicode_filename(self):
        body = build_body([{"name": "files", "filename": "夜曲.ncm", "data": b"x" * 5}])
        parts = self._parse(body)
        self.assertEqual(parts[0].filename, "夜曲.ncm")
        self.assertTrue(parts[0].path.is_file())

    def test_traversal_filename_is_neutralised(self):
        body = build_body([{"name": "files", "filename": "../../evil.mp3", "data": b"z"}])
        part = self._parse(body)[0]
        self.assertEqual(part.path.parent, self.dest)
        self.assertEqual(part.filename, "evil.mp3")

    def test_binary_payload_preserved(self):
        payload = bytes(range(256)) * 512        # 128 KiB，跨内部拷贝块
        body = build_body([{"name": "files", "filename": "big.bin", "data": payload}])
        part = self._parse(body)[0]
        self.assertEqual(part.path.read_bytes(), payload)

    def test_wrong_boundary(self):
        self.spool.write_bytes(build_body([{"name": "f", "filename": "a", "data": b"x"}]))
        with self.assertRaises(MultipartError):
            parse_multipart(self.spool, b"not-the-boundary", self.dest)

    def test_empty_body(self):
        self.spool.write_bytes(b"")
        with self.assertRaises(MultipartError):
            parse_multipart(self.spool, BOUNDARY, self.dest)

    def test_oversized_field_rejected(self):
        body = build_body([{"name": "note", "data": b"x" * 500}])
        self.spool.write_bytes(body)
        with self.assertRaises(MultipartError):
            parse_multipart(self.spool, BOUNDARY, self.dest, max_field_bytes=100)


if __name__ == "__main__":
    unittest.main()
