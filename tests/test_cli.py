"""命令行自测：参数解析、退出码、目录展开、probe/ncm 两条纯离线通道。

不真的起服务、不依赖 ffmpeg —— 覆盖的是「用户敲错参数时给不给得出人话」。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ncm import META, build_ncm, fake_audio  # noqa: E402

from audioforge import __version__  # noqa: E402
from audioforge.cli import _expand, _fmt_size, build_parser, main  # noqa: E402


def run(argv: list[str]) -> tuple[int, str, str]:
    """跑一次 ``main``，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:            # --version / 参数错误走这里
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class TestParser(unittest.TestCase):
    def test_no_subcommand_prints_help(self):
        code, out, _ = run([])
        self.assertEqual(code, 2)
        self.assertIn("audioforge", out)
        self.assertIn("ncm", out)

    def test_version(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_unknown_subcommand(self):
        code, _, err = run(["nope"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_convert_requires_format(self):
        code, _, err = run(["convert", "x.flac"])
        self.assertEqual(code, 2)
        self.assertIn("--format", err)

    def test_new_serve_flags_are_accepted(self):
        # 只解析，不执行：这里拿 parser 直接看 namespace，避免起服务
        ns = build_parser().parse_args([
            "serve", "--trust-proxy", "--trusted-proxy", "10.0.0.0/8",
            "--conn-timeout", "15", "--concurrency", "2",
            "--download-slots", "4", "--max-out-mb", "512",
        ])
        self.assertTrue(ns.trust_proxy)
        self.assertEqual(ns.trusted_proxy, "10.0.0.0/8")
        self.assertEqual(ns.conn_timeout, 15)
        self.assertEqual(ns.concurrency, 2)
        self.assertEqual(ns.download_slots, 4)
        self.assertEqual(ns.max_out_mb, 512.0)

    def test_serve_rejects_bad_credentials(self):
        code, _, err = run(["serve", "--auth", "没有冒号的口令"])
        self.assertEqual(code, 2)
        self.assertIn("用户:口令", err)


class TestExpand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "a.ncm").write_bytes(b"x")
        (self.root / "b.mp3").write_bytes(b"x")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "c.ncm").write_bytes(b"x")

    def test_files_and_dirs(self):
        got = [p.name for p in _expand([str(self.root)], {".ncm"}, recursive=False)]
        self.assertEqual(got, ["a.ncm"])

    def test_recursive(self):
        got = [p.name for p in _expand([str(self.root)], {".ncm"}, recursive=True)]
        self.assertEqual(sorted(got), ["a.ncm", "c.ncm"])

    def test_missing_path_is_reported_not_fatal(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            got = _expand([str(self.root / "nope.ncm"), str(self.root / "a.ncm")], {".ncm"}, False)
        self.assertEqual([p.name for p in got], ["a.ncm"])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("不存在", err.getvalue())

    def test_size_formatting(self):
        self.assertEqual(_fmt_size(1024), "1 KB")
        self.assertEqual(_fmt_size(3 << 20), "3.0 MB")


class TestOfflineCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "song.ncm"
        self.src.write_bytes(build_ncm(fake_audio(50_000), meta=META))

    def test_formats_lists_every_target(self):
        code, out, _ = run(["formats"])
        self.assertEqual(code, 0)
        for name in ("mp3", "flac", "wav", "opus"):
            self.assertIn(name, out)

    def test_probe_json(self):
        code, out, _ = run(["probe", str(self.src), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["title"], "测试曲目")
        self.assertEqual(payload[0]["artists"], ["张三", "李四"])

    def test_probe_missing_file(self):
        code, _, err = run(["probe", str(self.root / "nope.ncm")])
        self.assertEqual(code, 2)
        self.assertIn("没有找到", err)

    def test_ncm_decrypts_to_outdir(self):
        out_dir = self.root / "out"
        code, out, _ = run(["ncm", str(self.src), "-o", str(out_dir)])
        self.assertEqual(code, 0, out)
        produced = out_dir / "song.mp3"
        self.assertTrue(produced.is_file())
        self.assertIn("[OK]", out)

    def test_ncm_reports_truncation_warning(self):
        blob = build_ncm(fake_audio(500_000), meta=META)
        cut = self.root / "cut.ncm"
        cut.write_bytes(blob[: len(blob) - 380_000])
        code, _, err = run(["ncm", str(cut), "-o", str(self.root / "o2")])
        self.assertEqual(code, 0)              # 只告警，不当失败
        self.assertIn("[WARN]", err)
        self.assertIn("截断", err)


if __name__ == "__main__":
    unittest.main()
