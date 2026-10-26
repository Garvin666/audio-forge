"""ID3v2.3 标签写入的用例。

刻意不引第三方解析库：这里用一个二十行的最小解析器回读，只认本项目会写的那几种帧
（TIT2 / TPE1 / TALB / APIC）。这样测的是「字节按规范排」，而不是「某个库恰好能读」——
后者在换库时会悄悄失去意义。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge.id3 import MAX_COVER_BYTES, build_tag, looks_tagged  # noqa: E402


def parse_tag(data: bytes) -> tuple[tuple[int, int], dict[str, bytes]]:
    """回读 ID3v2 标签：返回 ((主版本, 次版本), {帧ID: 载荷})。"""
    if data[:3] != b"ID3":
        raise AssertionError("没有 ID3 头")
    ver = (data[3], data[4])
    raw = data[6:10]
    size = (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]
    body = data[10 : 10 + size]
    if len(body) != size:
        raise AssertionError(f"声明长度 {size}，实得 {len(body)}")

    frames: dict[str, bytes] = {}
    i = 0
    while i + 10 <= len(body):
        fid = body[i : i + 4].decode("ascii")
        n = int.from_bytes(body[i + 4 : i + 8], "big")
        frames[fid] = body[i + 10 : i + 10 + n]
        i += 10 + n
    if i != len(body):
        raise AssertionError(f"帧长度对不上：走过 {i}，标签体 {len(body)}")
    return ver, frames


def decode_text(payload: bytes) -> str:
    """解一个 UTF-16 文本帧（编码字节 + BOM + 正文）。"""
    assert payload[0] == 0x01, f"编码字节应为 0x01，实得 {payload[0]:#x}"
    assert payload[1:3] == b"\xff\xfe", "缺 UTF-16LE BOM"
    return payload[3:].decode("utf-16-le")


class TestHeader(unittest.TestCase):
    def test_nothing_to_write(self):
        self.assertEqual(build_tag(), b"")

    def test_version_and_size(self):
        tag = build_tag(title="x")
        ver, frames = parse_tag(tag)  # parse_tag 内部会核对 synchsafe 长度
        self.assertEqual(ver, (3, 0), "应写 ID3v2.3")
        self.assertEqual(tag[5], 0, "flags 应为 0")
        self.assertIn("TIT2", frames)

    def test_synchsafe_high_bit_clear(self):
        # 载荷 200 字节时，长度字节必须都不带最高位（否则玩家会把长度读成负数）
        big = "a" * 200
        tag = build_tag(title=big)
        for b in tag[6:10]:
            self.assertLess(b, 0x80)

    def test_looks_tagged(self):
        self.assertTrue(looks_tagged(b"ID3\x03\x00\x00"))
        self.assertFalse(looks_tagged(b"\xff\xfb\x90\x00"))

    def test_single_frame_size_is_plain_big_endian(self):
        # v2.3 的帧长度是普通大端，不是 synchsafe —— 这里用 >127 的载荷把它钉住
        text = "长" * 100  # UTF-16 下 200 字节
        tag = build_tag(title=text)
        self.assertEqual(tag[14:18], (200 + 3).to_bytes(4, "big"))


class TestFrames(unittest.TestCase):
    def test_title_utf16(self):
        _, frames = parse_tag(build_tag(title="晴天"))
        self.assertEqual(decode_text(frames["TIT2"]), "晴天")

    def test_artists_joined_by_slash(self):
        _, frames = parse_tag(build_tag(artists=["张三", "李四"]))
        self.assertEqual(decode_text(frames["TPE1"]), "张三/李四")

    def test_blank_artists_dropped(self):
        _, frames = parse_tag(build_tag(artists=["", "李四", ""]))
        self.assertEqual(decode_text(frames["TPE1"]), "李四")

    def test_album(self):
        _, frames = parse_tag(build_tag(album="测试专辑"))
        self.assertEqual(decode_text(frames["TALB"]), "测试专辑")

    def test_cover_apic_layout(self):
        cover = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        _, frames = parse_tag(build_tag(title="t", cover=cover, cover_ext="png"))
        apic = frames["APIC"]
        self.assertEqual(apic[0], 0x00, "APIC 用 ISO-8859-1 编码")
        self.assertTrue(apic.startswith(b"\x00image/png\x00"), apic[:20])
        self.assertEqual(apic[len(b"\x00image/png\x00")], 0x03, "图片类型应为正面封面")
        self.assertEqual(apic[len(b"\x00image/png\x00") + 1], 0x00, "描述应为空串")
        self.assertTrue(apic.endswith(cover))

    def test_unknown_cover_ext_defaults_to_jpeg(self):
        _, frames = parse_tag(build_tag(title="t", cover=b"\xff\xd8\xff", cover_ext="qoi"))
        self.assertTrue(frames["APIC"].startswith(b"\x00image/jpeg\x00"))

    def test_oversized_cover_skipped(self):
        huge = b"\x00" * (MAX_COVER_BYTES + 1)
        self.assertEqual(build_tag(cover=huge), b"", "只有封面且超大时应整体不写")
        _, frames = parse_tag(build_tag(title="t", cover=huge))
        self.assertNotIn("APIC", frames, "超大封面应被丢掉，但文本帧还在")


if __name__ == "__main__":
    unittest.main()
