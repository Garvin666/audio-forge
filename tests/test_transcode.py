"""通用转码自测。检测不到 ffmpeg 时整组跳过（NCM 通道不受影响）。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
