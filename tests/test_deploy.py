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
LEGACY_UNIT = ROOT / "deploy" / "audio-forge.service.rhel7"


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
        line = next(ln.strip() for ln in self.lines if ln.strip().startswith("ExecStart="))
        return shlex.split(line[len("ExecStart=") :])

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
        """加固项是这套部署的一半价值，删掉要有人发现。

        这 18 条在 systemd 257 上实测过：`systemd-analyze verify` **零输出**，
        且用同一份加固跑 `audioforge serve` 能正常启动、上传解密、Range 下载。
        （判据必须是「零输出」——`verify` 对语法错误也返回 0，只看退出码会假绿。）
        """
        for key in (
            # 基础防护
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "ReadWritePaths=/var/lib/audio-forge",
            "RestrictSUIDSGID=yes",
            "MemoryMax=2G",
            # 2026-10 新增：内核面与 capability 面
            "ProtectKernelLogs=yes",
            "ProtectClock=yes",
            "ProtectHostname=yes",
            "ProtectProc=invisible",
            "RestrictRealtime=yes",
            "UMask=0077",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
            "SystemCallFilter=@system-service",
        ):
            self.assertIn(key, self.text, f"缺少加固项 {key}")

    def test_syscall_filter_has_error_number(self) -> None:
        # 没有它，被拦的系统调用会直接 SIGSYS 杀进程，日志里只剩
        # "Main process exited, code=killed, status=31/SYS" —— 完全看不出原因
        self.assertIn("SystemCallErrorNumber=EPERM", self.text)

    def test_umask_tightens_created_files(self) -> None:
        # 实测：加了 UMask=0077 后，服务落盘的上传件与产物都是 600
        self.assertIn("UMask=0077", self.text)

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


# systemd 219（CentOS 7 / RHEL 7）上不存在的指令与取值。
# 版本来源：上游 man/systemd.exec.xml 的 version-info 指针 + NEWS 双源核对
# （ProtectKernelTunables/Modules/ControlGroups=232、ProtectSystem=strict=232、
#  RestrictNamespaces=233、LockPersonality=235、RestrictSUIDSGID=242）。
# 「不存在」是在 219 的 systemd 二进制指令表里逐条扫出来的（见 README §3.4）。
MISSING_ON_219 = (
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectControlGroups",
    "RestrictNamespaces",
    "RestrictSUIDSGID",
    "LockPersonality",
    "ReadWritePaths",  # 219 只认旧名 ReadWriteDirectories
    "MemoryMax",  # 219 只认旧名 MemoryLimit
    "ProtectSystem=strict",  # strict 取值 232 才有，219 只认 yes/no/full
    # 2026-10 主单元新增的几条，在 219 上同样不存在：
    "ProtectKernelLogs",  # 244
    "ProtectClock",  # 245
    "ProtectHostname",  # 242
    "ProtectProc",  # 247
    "RestrictRealtime",  # 231
    "AmbientCapabilities",  # 229
    "SystemCallErrorNumber",  # 232
)


class TestLegacyUnit(unittest.TestCase):
    """给老 systemd 用的降级单元：不许混进 219 不认识的指令。"""

    def setUp(self) -> None:
        self.assertTrue(LEGACY_UNIT.is_file(), f"缺少降级单元: {LEGACY_UNIT}")
        self.text = LEGACY_UNIT.read_text(encoding="utf-8")
        self.lines = join_continuations(self.text)
        # 头部注释里刻意写明了「哪些指令被换掉了」，所以断言只看真正的配置行。
        self.body = "\n".join(ln for ln in self.lines if not ln.lstrip().startswith("#"))

    def _exec_start(self, lines: list[str]) -> str:
        return next(ln.strip() for ln in lines if ln.strip().startswith("ExecStart="))

    def test_no_directive_missing_on_219(self) -> None:
        for key in MISSING_ON_219:
            self.assertNotIn(key, self.body, f"降级单元里出现了 219 不支持的 {key}")

    def test_keeps_baseline_hardening(self) -> None:
        """219 认识的那些必须留住，不然降级就等于没加固。"""
        for key in (
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "ProtectHome=yes",
            "ProtectSystem=full",
            "MemoryLimit=2G",
            "TasksMax=128",
            "ReadWriteDirectories=/var/lib/audio-forge",
            "Environment=PYTHONUNBUFFERED=1",
            "EnvironmentFile=",
        ):
            self.assertIn(key, self.body, f"降级单元缺少 {key}")

    def test_exec_start_identical_to_main_unit(self) -> None:
        """两份单元的 ExecStart 必须完全一致，否则 CLI 改了只改一边又会漂移。"""
        main = join_continuations(UNIT.read_text(encoding="utf-8"))
        self.assertEqual(
            self._exec_start(main),
            self._exec_start(self.lines),
            "降级单元与完整单元的 ExecStart 不一致",
        )


if __name__ == "__main__":
    unittest.main()
