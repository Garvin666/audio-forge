"""命令行自测：参数解析、退出码、目录展开、probe/ncm/qmc 三条离线通道。

不真的起服务；除显式 skip 的 `--to` 用例外不依赖 ffmpeg —— 覆盖的是
「用户敲错参数时给不给得出人话」。
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ncm import META, build_ncm, fake_audio  # noqa: E402
from test_qmc import (  # noqa: E402
    encrypt_payload,
    make_ekey,
    make_key,
    make_plain,
    pcv1_footer,
    qtag_footer,
    stag_footer,
)

from audioforge import __version__, qmc, transcode  # noqa: E402
from audioforge.cli import _expand, _fmt_size, build_parser, main  # noqa: E402


def run(argv: list[str]) -> tuple[int, str, str]:
    """跑一次 ``main``，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:  # --version / 参数错误走这里
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
        ns = build_parser().parse_args(
            [
                "serve",
                "--trust-proxy",
                "--trusted-proxy",
                "10.0.0.0/8",
                "--conn-timeout",
                "15",
                "--concurrency",
                "2",
                "--download-slots",
                "4",
                "--max-out-mb",
                "512",
            ]
        )
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

    def test_probe_bad_content_exits_nonzero(self):
        # 文件在、但不是 NCM：以前打印 [FAIL] 却回 0，脚本里 `if probe x` 会静默放行
        bad = self.root / "bad.ncm"
        bad.write_bytes(b"not a ncm at all")
        code, out, _ = run(["probe", str(bad)])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("不是 NCM 文件", out)

    def test_probe_json_bad_content_exits_nonzero(self):
        bad = self.root / "bad.ncm"
        bad.write_bytes(b"not a ncm at all")
        code, out, _ = run(["probe", str(bad), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIn("error", payload[0])

    def test_probe_batch_keeps_good_rows(self):
        # batch 语义不变：坏文件不连累好文件被探测，只是整体退出码非零
        bad = self.root / "bad.ncm"
        bad.write_bytes(b"not a ncm at all")
        code, out, _ = run(["probe", str(self.src), str(bad)])
        self.assertEqual(code, 1)
        self.assertIn("测试曲目", out)
        self.assertIn("[FAIL]", out)

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
        self.assertEqual(code, 0)  # 只告警，不当失败
        self.assertIn("[WARN]", err)
        self.assertIn("截断", err)


class TestQmcCommand(unittest.TestCase):
    """`audioforge qmc` 与「probe 能吃 QMC」这两条命令行契约。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plain = make_plain(6000)
        self.master = make_key(512)  # 512 字节 -> RC4 通道
        self.ekey = make_ekey(self.master, v2=True)

    def _write(self, name: str, master: bytes | None = None, footer: bytes = b"") -> Path:
        master = self.master if master is None else master
        p = self.root / name
        p.write_bytes(encrypt_payload(self.plain, master) + footer)
        return p

    def test_qmc_decrypts_to_outdir(self):
        src = self._write("song.mflac", footer=qtag_footer(self.ekey))
        out_dir = self.root / "out"
        code, out, _ = run(["qmc", str(src), "-o", str(out_dir)])
        self.assertEqual(code, 0, out)
        produced = out_dir / "song.flac"  # 后缀按嗅探结果定，不看 .mflac
        self.assertTrue(produced.is_file())
        self.assertEqual(produced.read_bytes(), self.plain)
        self.assertIn("[OK]", out)
        self.assertIn("rc4", out)

    def test_qmc_static_key_when_no_footer(self):
        # 没有尾部 = QMCv1，128 字节静态密钥，不需要任何 ekey
        src = self.root / "old.qmcflac"
        src.write_bytes(qmc.qmc1_transform(self.plain, 0))
        out_dir = self.root / "out"
        code, out, _ = run(["qmc", str(src), "-o", str(out_dir)])
        self.assertEqual(code, 0, out)
        self.assertEqual((out_dir / "old.flac").read_bytes(), self.plain)
        self.assertIn("静态密钥", out)

    def test_qmc_stag_without_ekey_fails_loudly(self):
        src = self._write("stag.mflac", footer=stag_footer())
        code, _, err = run(["qmc", str(src), "-o", str(self.root / "out")])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", err)
        self.assertIn("没有内嵌 EKey", err)

    def test_qmc_stag_with_manual_ekey(self):
        src = self._write("stag.mflac", footer=stag_footer())
        out_dir = self.root / "out"
        code, out, _ = run(["qmc", str(src), "--ekey", self.ekey, "-o", str(out_dir)])
        self.assertEqual(code, 0, out)
        self.assertEqual((out_dir / "stag.flac").read_bytes(), self.plain)

    def test_qmc_recursive_directory(self):
        self._write("a.mgg", footer=pcv1_footer(self.ekey))
        sub = self.root / "sub"
        sub.mkdir()
        # 无尾部 = QMCv1 静态密钥，载荷必须用同一个变换加，才能自洽
        (sub / "b.qmc0").write_bytes(qmc.qmc1_transform(self.plain, 0))
        out_dir = self.root / "out"
        code, out, _ = run(["qmc", str(self.root), "-r", "-o", str(out_dir)])
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["a.flac", "b.flac"])

    def test_qmc_probe_reports_footer(self):
        src = self._write("song.mflac", footer=qtag_footer(self.ekey))
        code, out, _ = run(["probe", str(src)])
        self.assertEqual(code, 0)
        self.assertIn("QMC", out)
        self.assertIn("QTag", out)
        self.assertIn(str(len(self.ekey)), out)

    def test_probe_mixed_batch_ncm_and_qmc(self):
        # probe 按后缀分流，两种容器混着传不该互相干扰
        ncm_src = self.root / "song.ncm"
        ncm_src.write_bytes(build_ncm(fake_audio(20_000), meta=META))
        qmc_src = self._write("song.mflac", footer=pcv1_footer(self.ekey))
        code, out, _ = run(["probe", str(ncm_src), str(qmc_src), "--json"])
        self.assertEqual(code, 0, out)
        rows = json.loads(out)
        self.assertEqual([r["kind"] for r in rows], ["ncm", "qmc"])

    def test_probe_cannot_validate_qmc_without_decrypting(self):
        # 刻意的分工：QMC 没有魔数，probe 只看尾部，看不出「这不是 QMC」。
        # 所以坏文件在 probe 眼里就是「无尾部的 v1」，真正判伪必须解密。
        # 这个用例钉住该语义，免得哪天有人把 probe 改成半解密还以为没改坏。
        bad = self.root / "bad.mflac"
        bad.write_bytes(b"not a qmc at all" * 8)
        code, out, _ = run(["probe", str(bad)])
        self.assertEqual(code, 0)
        self.assertIn("none(v1)", out)

        code2, _, err = run(["qmc", str(bad), "-o", str(self.root / "out")])
        self.assertEqual(code2, 1)
        self.assertIn("[FAIL]", err)
        self.assertIn("不像任何一种音频", err)

    def test_qmc_missing_files_reports(self):
        code, _, err = run(["qmc", str(self.root / "nope.mflac")])
        self.assertEqual(code, 2)
        self.assertIn("没有找到 QMC 文件", err)

    def test_qmc_ignores_non_qmc_names_in_dir(self):
        # .ncm 与普通音频不在 QMC 后缀集合里，不该被当成 QMC 硬解
        (self.root / "note.txt").write_bytes(b"hello")
        code, _, err = run(["qmc", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("没有找到 QMC 文件", err)

    @unittest.skipUnless(transcode.find_ffmpeg(), "本机没有 ffmpeg")
    def test_qmc_to_transcodes(self):
        # 得用真音频：纯随机字节能让嗅探认成 flac，但 ffmpeg 解不动
        wav = self.root / "tone.wav"
        subprocess.run(
            [
                transcode.find_ffmpeg(),
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.3",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],
            check=True,
        )
        real = wav.read_bytes()
        src = self.root / "tone.mflac"
        src.write_bytes(encrypt_payload(real, self.master) + pcv1_footer(self.ekey))

        out_dir = self.root / "out"
        code, out, _ = run(["qmc", str(src), "-o", str(out_dir), "--to", "mp3"])
        self.assertEqual(code, 0, out)
        self.assertTrue((out_dir / "tone.mp3").is_file())
        self.assertIn("→ tone.mp3", out)


if __name__ == "__main__":
    unittest.main()
