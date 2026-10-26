"""通用转码自测。检测不到 ffmpeg 时整组跳过（NCM 通道不受影响）。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge.transcode import (  # noqa: E402
    FORMATS,
    TranscodeError,
    convert,
    find_ffmpeg,
    quality_label,
)

FFMPEG = find_ffmpeg()


@unittest.skipUnless(FFMPEG, "未安装 ffmpeg")
class TestConvert(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.wav = cls.dir / "tone.wav"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.6:sample_rate=44100",
             "-ac", "2", str(cls.wav)],
            check=True, capture_output=True, timeout=60,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_wav_to_flac(self):
        out = convert(self.wav, self.dir, "flac", ffmpeg=FFMPEG)
        self.assertTrue(out.is_file())
        self.assertEqual(out.suffix, ".flac")
        self.assertEqual(out.read_bytes()[:4], b"fLaC")

    def test_wav_to_mp3(self):
        out = convert(self.wav, self.dir, "mp3", quality="medium", ffmpeg=FFMPEG)
        head = out.read_bytes()[:3]
        self.assertTrue(head == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"))

    def test_bitrate_override(self):
        out = convert(self.wav, self.dir, "mp3", bitrate="128k", ffmpeg=FFMPEG)
        self.assertGreater(out.stat().st_size, 1000)

    def test_opus_and_m4a(self):
        for target in ("opus", "m4a"):
            out = convert(self.wav, self.dir, target, quality="low", ffmpeg=FFMPEG)
            self.assertTrue(out.is_file(), target)
            self.assertGreater(out.stat().st_size, 200, target)

    def test_unknown_target(self):
        with self.assertRaises(TranscodeError):
            convert(self.wav, self.dir, "xyz", ffmpeg=FFMPEG)

    def test_missing_source(self):
        with self.assertRaises(TranscodeError):
            convert(self.dir / "nope.wav", self.dir, "mp3", ffmpeg=FFMPEG)

    def test_broken_input(self):
        junk = self.dir / "junk.wav"
        junk.write_bytes(b"not audio at all" * 50)
        with self.assertRaises(TranscodeError):
            convert(junk, self.dir, "mp3", ffmpeg=FFMPEG)

    def test_ext_is_dotted_input_accepted(self):
        out = convert(self.wav, self.dir, ".ogg", ffmpeg=FFMPEG)
        self.assertEqual(out.suffix, ".ogg")


class TestInvocation(unittest.TestCase):
    """不需要真 ffmpeg：换掉 ``subprocess.run``，只看参数拼装与产物收尾。

    这里锁的是三件「改坏了不会报错、只会悄悄出事」的事：临时文件保留扩展名、
    ``-fs`` 出现在输出文件之前、超限时把半成品删干净。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "in.wav"
        self.src.write_bytes(b"RIFF" + b"\x00" * 100)

    def _fake(self, size: int = 4096, code: int = 0):
        def run(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"\x00" * size)
            return subprocess.CompletedProcess(cmd, code, "", "")
        return mock.patch("audioforge.transcode.subprocess.run", side_effect=run)

    def test_fs_flag_goes_before_output(self):
        with self._fake() as run:
            convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake", max_bytes=1 << 20)
        cmd = run.call_args.args[0]
        self.assertIn("-fs", cmd)
        self.assertEqual(cmd[cmd.index("-fs") + 1], str(1 << 20))
        self.assertLess(cmd.index("-fs"), len(cmd) - 1)      # 必须作为输出选项

    def test_no_cap_means_no_fs_flag(self):
        with self._fake() as run:
            convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake")
        self.assertNotIn("-fs", run.call_args.args[0])

    def test_zero_cap_means_unlimited(self):
        with self._fake() as run:
            convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake", max_bytes=0)
        self.assertNotIn("-fs", run.call_args.args[0])

    def test_bad_bitrate_rejected_before_spawning(self):
        with self._fake() as run, self.assertRaises(TranscodeError):
            convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake", bitrate="-1:a")
        run.assert_not_called()

    def test_over_cap_raises_and_leaves_nothing(self):
        with self._fake(size=2 << 20), self.assertRaises(TranscodeError) as ctx:
            convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake", max_bytes=1 << 20)
        self.assertIn("体积上限", str(ctx.exception))
        self.assertEqual(list(self.dir.glob("*.mp3")), [])
        self.assertEqual(list(self.dir.glob(".*part*")), [])

    def test_under_cap_lands_at_final_name(self):
        with self._fake(size=1024):
            out = convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake", max_bytes=1 << 20)
        self.assertTrue(out.is_file())
        self.assertEqual(out.name, "in.mp3")

    def test_temp_keeps_target_extension(self):
        # ffmpeg 靠扩展名选封装器；.part 丢了扩展名就会转出错误的容器
        with self._fake() as run:
            convert(self.src, self.dir, "flac", ffmpeg="ffmpeg-fake")
        self.assertTrue(run.call_args.args[0][-1].endswith(".part.flac"))

    def test_failure_cleans_temp(self):
        with self._fake(size=1024, code=1), self.assertRaises(TranscodeError):
            convert(self.src, self.dir, "mp3", ffmpeg="ffmpeg-fake")
        self.assertEqual(list(self.dir.glob(".*part*")), [])
        self.assertEqual(list(self.dir.glob("*.mp3")), [])


class TestMetadata(unittest.TestCase):
    def test_every_format_has_unique_ext_and_quality(self):
        exts = [(f.ext, f.name) for f in FORMATS.values()]
        self.assertEqual(len(exts), len(set(exts)))

    def test_quality_labels(self):
        for fmt in FORMATS.values():
            for q in ("low", "medium", "high"):
                label = quality_label(fmt, q)
                self.assertTrue(label)
                if fmt.lossy:
                    self.assertTrue(label.endswith("k"), f"{fmt.name}/{q} -> {label}")

    def test_lossless_has_no_bitrate(self):
        for fmt in FORMATS.values():
            if not fmt.lossy:
                self.assertIsNone(fmt.bitrate("high"))


if __name__ == "__main__":
    unittest.main()
