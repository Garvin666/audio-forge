"""部署产物自测：systemd 单元与 CLI 参数表不许漂移。

背景：`deploy/audio-forge.service` 的 `ExecStart` 手写了 10 个参数。
改 CLI 时如果只动 argparse 忘了改单元，服务上线后会在 systemd 启动阶段直接
`unrecognized arguments` 退出——本地测试、CI、Web 单测全绿，只有真正部署才炸。
这里把「参数名必须都被 argparse 接受」变成可回归的断言。
"""

from __future__ import annotations

import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audioforge.cli import build_parser  # noqa: E402

UNIT = ROOT / "deploy" / "audio-forge.service"


def join_continuations(text: str) -> list[str]:
    """按 systemd 的规则把反斜杠续行合并（行尾 \\ 表示下一行接续）。"""
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1]
        else:
            out.append(buf + line)
            buf = ""
    if buf:
        out.append(buf)
    return out


def serve_subparser():
    parser = build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "serve" in choices:
            return choices["serve"]
    raise AssertionError("build_parser() 里找不到 serve 子命令")


class TestSystemdUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(UNIT.is_file(), f"缺少单元文件: {UNIT}")
        self.text = UNIT.read_text(encoding="utf-8")
        self.lines = join_continuations(self.text)

    def test_exec_start_single_line(self) -> None:
        starts = [ln for ln in self.lines if ln.strip().startswith("ExecStart=")]
        self.assertEqual(len(starts), 1, f"ExecStart 应恰好一条，实际 {len(starts)}")

    def test_exec_start_targets_serve(self) -> None:
        argv = self.argv()
        self.assertTrue(argv[0].endswith("/audioforge"), f"可执行体异常: {argv[0]}")
        self.assertEqual(argv[1], "serve", f"子命令异常: {argv[1]}")

    def argv(self) -> list[str]:
        line = next(
            ln.strip() for ln in self.lines if ln.strip().startswith("ExecStart=")
        )
        return shlex.split(line[len("ExecStart="):])

    def test_every_flag_accepted_by_cli(self) -> None:
        """ExecStart 里每个 flag 都必须被 serve 子命令认识。"""
        sub = serve_subparser()
        known: set[str] = set()
        for action in sub._actions:
            known.update(action.option_strings)
            if action.dest != "help":
                known.add("--" + action.dest.replace("_", "-"))

        rest = self.argv()[2:]
        seen: list[str] = []
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                name = tok.split("=", 1)[0]
                seen.append(name)
                self.assertIn(name, known, f"CLI 不认识 {name}（单元文件已漂移）")
                if "=" not in tok and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
                    i += 2
                    continue
            i += 1
        self.assertGreaterEqual(len(seen), 5, f"只解析到 {len(seen)} 个 flag，疑似解析失效")

    def test_flags_parse_without_error(self) -> None:
        """真的丢给 argparse 跑一遍，确认取值类型也没问题。"""
        sub = serve_subparser()
        rest = self.argv()[2:]
        probe: list[str] = []
        i = 0
        while i < len(rest):
            tok = rest[i]
            probe.append(tok)
            if "=" not in tok and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
                probe.append(rest[i + 1])
                i += 2
                continue
            i += 1
        ns = sub.parse_args(probe)  # 解析失败会抛 SystemExit
        self.assertEqual(ns.port, 8000)
        self.assertEqual(ns.host, "127.0.0.1", "单元文件应只监听回环，由反代对外")
        self.assertTrue(ns.quiet)

    def test_hardening_directives_present(self) -> None:
        """加固项是这套部署的一半价值，删掉要有人发现。"""
        for key in (
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "RestrictSUIDSGID=yes",
            "ReadWritePaths=/var/lib/audio-forge",
        ):
            self.assertIn(key, self.text, f"缺少加固项 {key}")

    def test_no_credential_on_command_line(self) -> None:
        """口令不许出现在 ExecStart（ps 可见）。"""
        cmdline = " ".join(self.argv())
        for bad in ("--auth", "--token"):
            self.assertNotIn(bad, cmdline, f"命令行里不应出现 {bad}，应走 EnvironmentFile")
        self.assertIn("EnvironmentFile=", self.text)

    def test_stdout_unbuffered(self) -> None:
        """必须关掉 stdout 缓冲，否则 journalctl 里一个字都看不到。

        systemd 下 stdout 是管道，Python 默认块缓冲 ⇒ 启动横幅与访问日志全卡在缓冲区，
        直到进程退出才刷（被 SIGKILL 则永久丢失）。实测：不加这行 `journalctl -u` 只有
        systemd 自己的 Started/Stopped 四行，运维会以为服务没起来。
        Docker 路径看不出来是因为 Dockerfile 里设了同一个变量。
        """
        self.assertIn(
            "Environment=PYTHONUNBUFFERED=1",
            self.text,
            "缺 PYTHONUNBUFFERED=1：journalctl 里将看不到任何应用输出",
        )


if __name__ == "__main__":
    unittest.main()
