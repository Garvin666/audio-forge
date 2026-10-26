"""NCM 容器自测。

fixture 不是从网上下载的样本，而是按 ``docs/FORMAT.md`` 里的布局现造出来的：
用 ``AES128ECB.encrypt`` 造 key/meta 块，用同一张 keystream 表把音频异或回去。
这样测试既能验证解密逻辑，也能顺带证明「格式描述本身是自洽的」。
"""

from __future__ import annotations

import base64
import json
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge import ncm as ncm_mod  # noqa: E402
from audioforge.aes import AES128ECB  # noqa: E402
from audioforge.ncm import (  # noqa: E402
    CORE_KEY,
    GAP,
    KEY_PREFIX,
    MAGIC,
    META_B64_PREFIX,
    META_KEY,
    NcmError,
    NcmInfo,
    decrypt,
    guess_format,
    keystream_table,
    probe,
)

KEY_STR = "0123456789abcdef0123456789abcdef"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"


def _xor(data: bytes, table: bytes) -> bytes:
    n = len(data)
    stream = (table * (n // len(table) + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(n, "little")


def build_ncm(
    audio: bytes,
    *,
    key_str: str = KEY_STR,
    meta: dict | None = None,
    cover: bytes = b"",
    legacy_key: bool = False,
    dup_cover_len: bool = False,
) -> bytes:
    core = AES128ECB(CORE_KEY)
    if legacy_key:
        key_raw = KEY_PREFIX + core.encrypt_padded(key_str.encode("ascii"))
    else:
        key_raw = core.encrypt_padded(KEY_PREFIX + key_str.encode("ascii"))
    key_block = bytes(b ^ 0x64 for b in key_raw)

    if meta is None:
        meta_block = b""
    else:
        plain = b"music:" + json.dumps(meta, ensure_ascii=False).encode("utf-8")
        enc = AES128ECB(META_KEY).encrypt_padded(plain)
        meta_block = bytes(b ^ 0x63 for b in META_B64_PREFIX + base64.b64encode(enc))

    out = bytearray()
    out += MAGIC + GAP
    out += struct.pack("<I", len(key_block)) + key_block
    out += struct.pack("<I", len(meta_block)) + meta_block
    out += struct.pack("<I", 0)  # crc32 占位
    out += b"\x00"  # gap
    if cover:
        out += struct.pack("<I", len(cover))
        if dup_cover_len:
            out += struct.pack("<I", len(cover))
        out += cover
    else:
        out += struct.pack("<I", 0)
    out += _xor(audio, keystream_table(key_str))
    return bytes(out)


def fake_audio(n: int, seed: int = 7) -> bytes:
    """造一段以 mp3 帧同步头开头的伪码流。"""
    rnd = random.Random(seed)
    return b"\xff\xfb\x90\x00" + bytes(rnd.getrandbits(8) for _ in range(max(0, n - 4)))


META = {
    "musicName": "测试曲目",
    "artist": [["张三", "1001"], ["李四", "1002"]],
    "album": "测试专辑",
    "bitrate": 320000,
    "duration": 12345,
    "format": "mp3",
}


class TestKeystream(unittest.TestCase):
    def test_table_is_periodic_and_stable(self):
        t1 = keystream_table(KEY_STR)
        self.assertEqual(len(t1), 256)
        self.assertEqual(t1, keystream_table(KEY_STR))

    def test_key_changes_stream(self):
        self.assertNotEqual(keystream_table("a" * 32), keystream_table("b" * 32))

    def test_empty_key_rejected(self):
        with self.assertRaises(NcmError):
            keystream_table("")


class TestProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_metadata(self):
        p = self._write("song.ncm", build_ncm(fake_audio(5000), meta=META, cover=JPEG))
        info = probe(p)
        self.assertEqual(info.music_name, "测试曲目")
        self.assertEqual(info.artists, ["张三", "李四"])
        self.assertEqual(info.album, "测试专辑")
        self.assertEqual(info.bitrate, 320000)
        self.assertEqual(info.duration_ms, 12345)
        self.assertEqual(info.fmt, "mp3")
        self.assertTrue(info.cover_size > 0)

    def test_without_meta_or_cover(self):
        p = self._write("bare.ncm", build_ncm(fake_audio(3000)))
        info = probe(p)
        self.assertIsNone(info.music_name)
        self.assertEqual(info.artists, [])
        self.assertEqual(info.cover_size, 0)
        self.assertEqual(info.fmt, "mp3")  # 由码流头推断

    def test_rejects_non_ncm(self):
        p = self._write("bad.ncm", b"ID3\x03\x00" + b"\x00" * 100)
        with self.assertRaises(NcmError):
            probe(p)

    def test_truncated_file(self):
        data = build_ncm(fake_audio(20000), meta=META)
        p = self._write("cut.ncm", data[:40])
        with self.assertRaises(NcmError):
            probe(p)


class TestDecrypt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.out.mkdir()

    def _roundtrip(self, audio: bytes, **kw) -> bytes:
        src = self.dir / "x.ncm"
        src.write_bytes(build_ncm(audio, **kw))
        dst, _ = decrypt(src, self.out)
        return dst.read_bytes()

    def test_bytes_identical(self):
        audio = fake_audio(100_000)
        self.assertEqual(self._roundtrip(audio, meta=META), audio)

    def test_crosses_chunk_boundary(self):
        # 4 MiB 是一个分块边界，必须保证第二块起点仍落在 256 字节周期上
        audio = fake_audio((4 << 20) + 777)
        self.assertEqual(self._roundtrip(audio), audio)

    def test_tail_not_period_aligned(self):
        audio = fake_audio(256 * 40 + 13)
        self.assertEqual(self._roundtrip(audio), audio)

    def test_legacy_key_layout(self):
        # 旧版布局：key 块以 17 字节明文前缀开头，长度 % 16 == 1
        audio = fake_audio(50_000)
        self.assertEqual(self._roundtrip(audio, key_str="legacy-key-ascii", legacy_key=True), audio)

    def test_cover_duplicated_length(self):
        audio = fake_audio(20_000)
        self.assertEqual(self._roundtrip(audio, cover=JPEG, dup_cover_len=True), audio)

    def test_cover_single_length(self):
        audio = fake_audio(20_000)
        self.assertEqual(self._roundtrip(audio, cover=JPEG), audio)

    def test_cover_extracted(self):
        src = self.dir / "c.ncm"
        src.write_bytes(build_ncm(fake_audio(20_000), cover=JPEG))
        dst, info = decrypt(src, self.out, extract_cover=True)
        cover = self.out / f"{src.stem}.cover.jpg"
        self.assertTrue(cover.is_file())
        self.assertEqual(cover.read_bytes(), JPEG)
        self.assertEqual(info.cover_ext, "jpg")

    def test_output_extension_follows_stream(self):
        src = self.dir / "v.ncm"
        src.write_bytes(build_ncm(b"fLaC" + b"\x00" * 5000))
        dst, info = decrypt(src, self.out)
        self.assertEqual(dst.suffix, ".flac")
        self.assertEqual(info.fmt, "flac")

    def test_refuses_overwrite_when_asked(self):
        src = self.dir / "o.ncm"
        src.write_bytes(build_ncm(fake_audio(1000)))
        decrypt(src, self.out)
        with self.assertRaises(NcmError):
            decrypt(src, self.out, overwrite=False)

    def test_missing_file(self):
        with self.assertRaises(NcmError):
            decrypt(self.dir / "nope.ncm", self.out)


class TestAtomicWrite(unittest.TestCase):
    """``.part`` + 原子改名：失败不留半截文件（P0-5）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_no_part_file_left_after_success(self):
        src = self.dir / "ok.ncm"
        src.write_bytes(build_ncm(fake_audio(20_000), meta=META))
        dst, _ = decrypt(src, self.dir)
        self.assertTrue(dst.is_file())
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.part")), [])

    def test_failure_removes_part_and_target(self):
        src = self.dir / "boom.ncm"
        src.write_bytes(build_ncm(fake_audio(20_000), meta=META))

        def explode(fp, dst, table, size):
            dst.write(b"\x00" * 512)  # 已经写了一半
            raise OSError("disk full")

        patched = mock.patch.object(ncm_mod, "_xor_keystream", explode)
        with patched, self.assertRaises(OSError):
            decrypt(src, self.dir)

        self.assertFalse((self.dir / "boom.mp3").exists())  # 不能留「看着像成品」的
        self.assertEqual(list(self.dir.glob("*.part")), [])  # 也不能留临时件


class TestSizeCheck(unittest.TestCase):
    """体积对照是启发式提示，不阻断产出（P0-5）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _decrypt(self, audio: bytes) -> NcmInfo:
        src = self.dir / "s.ncm"
        src.write_bytes(build_ncm(audio, meta=META))
        _, info = decrypt(src, self.dir)
        return info

    def test_matching_size_is_consistent(self):
        # META 声明 320 kbps / 12.345 s → 推算约 493 800 字节，给 500 000 落在 0.5–2.0 内
        info = self._decrypt(fake_audio(500_000))
        self.assertTrue(info.size_check["consistent"])
        self.assertTrue(info.summary()["size_consistent"])

    def test_truncated_file_is_flagged_but_still_produced(self):
        blob = build_ncm(fake_audio(500_000), meta=META)
        src = self.dir / "cut.ncm"
        src.write_bytes(blob[: len(blob) - 380_000])  # 尾部被砍掉

        out, info = decrypt(src, self.dir)  # 不抛错：仍产出
        self.assertTrue(out.is_file())
        self.assertFalse(info.size_check["consistent"])
        self.assertLess(info.size_check["ratio"], 0.5)
        self.assertFalse(info.summary()["size_consistent"])

    def test_no_metadata_means_no_verdict(self):
        src = self.dir / "plain.ncm"
        src.write_bytes(build_ncm(fake_audio(5_000)))
        _, info = decrypt(src, self.dir)
        self.assertEqual(info.size_check, {})
        self.assertIsNone(info.summary()["size_consistent"])

    def test_probe_never_sets_size_check(self):
        src = self.dir / "p.ncm"
        src.write_bytes(build_ncm(fake_audio(500_000), meta=META))
        self.assertEqual(probe(src).size_check, {})


class TestGuessFormat(unittest.TestCase):
    def test_known_magic(self):
        self.assertEqual(guess_format(b"fLaC\x00\x00\x00\x22"), "flac")
        self.assertEqual(guess_format(b"ID3\x03\x00\x00"), "mp3")
        self.assertEqual(guess_format(b"\xff\xfb\x90\x00"), "mp3")
        self.assertEqual(guess_format(b"OggS\x00"), "ogg")
        self.assertEqual(guess_format(b"RIFF\x00\x00\x00\x00"), "wav")

    def test_unknown(self):
        self.assertIsNone(guess_format(b"\x00\x01\x02\x03"))


if __name__ == "__main__":
    unittest.main()
