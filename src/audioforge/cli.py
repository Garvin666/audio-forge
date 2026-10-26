"""命令行入口：``audioforge`` / ``python -m audioforge``。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__, ncm, transcode
from .guard import Limits
from .httpd.config import DEFAULT_TRUSTED_PROXIES
from .server import ServerConfig, serve

__all__ = ["main"]

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


_FFMPEG_OPT: dict[str, Any] = dict(
    dest="ffmpeg",
    default=argparse.SUPPRESS,
    metavar="路径",
    help="显式指定 ffmpeg 可执行文件（也可用顶层 --ffmpeg 或环境变量 AUDIOFORGE_FFMPEG）",
)

_AUDIO_EXT = {
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".aiff",
    ".ape",
    ".wma",
    ".ncm",
    ".aif",
    ".alac",
}


def _expand(paths: list[str], exts: set[str], recursive: bool) -> list[Path]:
    """把「文件或目录」展开成文件列表，保持命令行给出的顺序。"""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            out.extend(sorted(x for x in it if x.is_file() and x.suffix.lower() in exts))
        elif p.is_file():
            out.append(p)
        else:
            print(f"[跳过] 不存在：{p}", file=sys.stderr)
    return out


def _fmt_size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1 << 20 else f"{n / 1024:.0f} KB"


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def cmd_ncm(args: argparse.Namespace) -> int:
    targets = _expand(args.input, {".ncm"}, args.recursive)
    if not targets:
        print("没有找到 .ncm 文件", file=sys.stderr)
        return 2

    ok = fail = 0
    for src in targets:
        out_dir = Path(args.outdir) if args.outdir else src.parent
        try:
            path, info = ncm.decrypt(src, out_dir, extract_cover=args.cover)
            line = f"[OK] {path.name}  {_fmt_size(path.stat().st_size)}  {info.fmt.upper()}"
            if info.music_name:
                who = " / ".join(info.artists) or "未知艺人"
                line += f"  {info.music_name} — {who}"
            if args.to:
                path = transcode.convert(
                    path,
                    out_dir,
                    args.to,
                    quality=args.quality,
                    bitrate=args.bitrate,
                    ffmpeg=args.ffmpeg,
                )
                line += f"  → {path.name}"
            print(line)
            check = info.size_check or {}
            if check and not check.get("consistent", True):
                print(
                    f"[WARN] {src.name}: 解出 {check['actual_bytes']} 字节，按元数据推算约 "
                    f"{check['expected_bytes']} 字节 —— 文件可能被截断，建议核对原文件",
                    file=sys.stderr,
                )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {src.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            fail += 1

    print(f"\n完成 {ok} 个，失败 {fail} 个")
    return 0 if fail == 0 else 1


def cmd_convert(args: argparse.Namespace) -> int:
    targets = _expand(args.input, _AUDIO_EXT, args.recursive)
    if not targets:
        print("没有找到可转换的音频文件", file=sys.stderr)
        return 2

    ok = fail = 0
    for src in targets:
        out_dir = Path(args.outdir) if args.outdir else src.parent
        try:
            path = transcode.convert(
                src,
                out_dir,
                args.format,
                quality=args.quality,
                bitrate=args.bitrate,
                ffmpeg=args.ffmpeg,
            )
            layer = transcode.quality_label(
                transcode.FORMATS[args.format], args.quality, args.bitrate
            )
            print(f"[OK] {src.name} → {path.name}  {_fmt_size(path.stat().st_size)}  {layer}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {src.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            fail += 1

    print(f"\n完成 {ok} 个，失败 {fail} 个")
    return 0 if fail == 0 else 1


def cmd_formats(_: argparse.Namespace) -> int:
    import unicodedata

    def width(s: str) -> int:
        # 中文/全角字符占两列，直接按 len 对齐会歪
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

    def pad(s: str, w: int) -> str:
        return s + " " * max(0, w - width(s))

    header = [("格式", 8), ("扩展名", 9), ("编码器", 13), ("类型", 6), ("档位", 21), ("说明", 0)]
    print("".join(pad(t, w) for t, w in header))
    print("-" * 76)
    for name, f in transcode.FORMATS.items():
        tiers = " / ".join(f"{t}k" for t in f.tiers) if f.tiers else "—"
        kind = "有损" if f.lossy else "无损"
        print(
            pad(name, 8) + pad(f.ext, 9) + pad(f.codec, 13) + pad(kind, 6) + pad(tiers, 21) + f.note
        )
    print()
    ff = transcode.find_ffmpeg()
    print(
        f"ffmpeg: {transcode.ffmpeg_version(ff) if ff else '未找到（通用转码不可用，NCM 通道仍可用）'}"
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if args.no_limits:
        limits = Limits.unlimited()
    else:
        limits = Limits(
            per_minute=args.rate,
            files_per_day=args.daily_files,
            mb_per_day=int(args.daily_gb * 1024),
            disk_mb=int(args.disk_gb * 1024),
        )

    raw_auth = args.auth or os.environ.get("AUDIOFORGE_AUTH") or ""
    user = password = None
    if raw_auth:
        user, sep, password = raw_auth.partition(":")
        if not sep or not user:
            print("--auth 需要「用户:口令」形式（口令里可以含冒号）", file=sys.stderr)
            return 2
    token = args.token or os.environ.get("AUDIOFORGE_TOKEN") or None

    trust_proxy = args.trust_proxy or _env_flag("AUDIOFORGE_TRUST_PROXY")
    raw_proxies = args.trusted_proxy or os.environ.get("AUDIOFORGE_TRUSTED_PROXY") or ""
    trusted = tuple(x.strip() for x in raw_proxies.split(",") if x.strip())
    if trust_proxy and not trusted:
        trusted = DEFAULT_TRUSTED_PROXIES

    if args.host not in ("127.0.0.1", "localhost", "::1") and not (user or token):
        print(
            "警告：正在对非本机地址开放，但没有启用任何鉴权。"
            "任何能访问该端口的人都能上传并读回结果。",
            file=sys.stderr,
        )

    max_out_mb = args.max_out_mb
    if max_out_mb is None:
        max_out_mb = float(os.environ.get("AUDIOFORGE_MAX_OUT_MB") or 2048)

    cfg = ServerConfig(
        host=args.host,
        port=args.port,
        max_bytes=int(args.max_mb * (1 << 20)),
        ttl=args.ttl,
        work_dir=Path(args.workdir) if args.workdir else None,
        concurrency=args.concurrency,
        download_slots=args.download_slots,
        conn_timeout=args.conn_timeout,
        max_out_mb=int(max_out_mb),
        ffmpeg=args.ffmpeg,
        quiet=args.quiet,
        auth_user=user,
        auth_pass=password,
        access_token=token,
        trust_proxy=trust_proxy,
        trusted_proxies=trusted,
        limits=limits,
    )
    return serve(cfg)


def cmd_probe(args: argparse.Namespace) -> int:
    import json

    targets = _expand(args.input, {".ncm"}, args.recursive)
    if not targets:
        print("没有找到 .ncm 文件", file=sys.stderr)
        return 2
    rows = []
    for src in targets:
        try:
            rows.append(ncm.probe(src).summary())
        except Exception as exc:  # noqa: BLE001
            rows.append({"file": src.name, "error": f"{type(exc).__name__}: {exc}"})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            if "error" in r:
                print(f"[FAIL] {r['file']}: {r['error']}")
            else:
                who = " / ".join(r["artists"]) or "未知"
                dur = f"{r['duration_s']}s" if r["duration_s"] else "?"
                print(
                    f"{r['file']}  {r['format'].upper()}  {dur}  "
                    f"{r['bitrate'] or '?'}bps  {r['title'] or '?'} — {who}"
                )
    return 0


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="audioforge",
        description="audio-forge：NCM 解密 + 通用音频格式转换（本地 Web 平台见 serve）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  audioforge ncm song.ncm\n"
            "  audioforge ncm 专辑目录/ -r --cover\n"
            "  audioforge ncm song.ncm --to wav\n"
            "  audioforge convert *.flac -f mp3 --quality high\n"
            "  audioforge serve --port 8000\n"
        ),
    )
    ap.add_argument("-V", "--version", action="version", version=f"audio-forge {__version__}")
    ap.add_argument("--ffmpeg", default=None, help="显式指定 ffmpeg 可执行文件路径")
    sub = ap.add_subparsers(dest="cmd", metavar="<命令>")

    common_out: dict[str, Any] = dict(metavar="目录", help="输出目录，默认与源文件同目录")

    p = sub.add_parser("ncm", help="把 .ncm 还原成 mp3/flac")
    p.add_argument("input", nargs="+", help=".ncm 文件或目录")
    p.add_argument("-o", "--outdir", default=None, **common_out)
    p.add_argument("-r", "--recursive", action="store_true", help="递归子目录")
    p.add_argument("--cover", action="store_true", help="同时导出内嵌封面")
    p.add_argument("--to", default=None, help="解出后再转成该格式（如 wav/mp3/flac）")
    p.add_argument("--quality", default="high", choices=transcode.QUALITY_NAMES)
    p.add_argument("--bitrate", default=None, help="覆盖码率，如 320k")
    p.add_argument("--ffmpeg", **_FFMPEG_OPT)
    p.set_defaults(func=cmd_ncm)

    p = sub.add_parser("convert", help="通用音频格式互转（需 ffmpeg）")
    p.add_argument("input", nargs="+", help="音频文件或目录")
    p.add_argument("-f", "--format", required=True, choices=sorted(transcode.FORMATS))
    p.add_argument("-o", "--outdir", default=None, **common_out)
    p.add_argument("-r", "--recursive", action="store_true", help="递归子目录")
    p.add_argument("--quality", default="high", choices=transcode.QUALITY_NAMES)
    p.add_argument("--bitrate", default=None, help="覆盖码率，如 192k")
    p.add_argument("--ffmpeg", **_FFMPEG_OPT)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("probe", help="只读 .ncm 头部信息，不解音频")
    p.add_argument("input", nargs="+")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("formats", help="列出支持的格式与编码参数")
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("serve", help="启动本地 Web 上传平台")
    p.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--max-mb", type=float, default=200, help="单次上传上限（MB）")
    p.add_argument("--ttl", type=int, default=3600, help="结果保留秒数，默认 3600")
    p.add_argument("--workdir", default=None, help="临时工作目录")
    p.add_argument("-q", "--quiet", action="store_true", help="不打印访问日志")
    p.add_argument(
        "--auth",
        metavar="用户:口令",
        help="启用 HTTP Basic 认证（浏览器会弹框）；也可用环境变量 AUDIOFORGE_AUTH",
    )
    p.add_argument(
        "--token",
        metavar="令牌",
        help="启用 Bearer 令牌；也可用环境变量 AUDIOFORGE_TOKEN。页面用 ?token= 传入",
    )
    p.add_argument("--rate", type=int, default=120, help="每 IP 每分钟请求上限，0 表示不限")
    p.add_argument("--daily-files", type=int, default=2000, help="每 IP 每日文件数上限，0 表示不限")
    p.add_argument(
        "--daily-gb", type=float, default=20.0, help="每 IP 每日上传总量上限（GB），0 表示不限"
    )
    p.add_argument("--disk-gb", type=float, default=20.0, help="结果目录总量上限（GB），0 表示不限")
    p.add_argument(
        "--max-out-mb",
        type=float,
        default=None,
        help="单个产物体积上限（MB），默认 2048，0 表示不限；也可用环境变量 AUDIOFORGE_MAX_OUT_MB",
    )
    p.add_argument("--no-limits", action="store_true", help="关闭全部速率与配额限制")
    p.add_argument("--concurrency", type=int, default=4, help="同时转换的请求数，默认 4")
    p.add_argument(
        "--download-slots",
        type=int,
        default=0,
        help="同时下载的文件数，0 表示按并发数自动（至少 8）",
    )
    p.add_argument(
        "--conn-timeout",
        type=int,
        default=60,
        help="单次 socket 读超时秒数，默认 60（防慢连接占满线程）",
    )
    p.add_argument(
        "--trust-proxy",
        action="store_true",
        help="信任反代传来的 X-Forwarded-For（仅当直连方在 --trusted-proxy 内）；"
        "也可用环境变量 AUDIOFORGE_TRUST_PROXY=1",
    )
    p.add_argument(
        "--trusted-proxy",
        metavar="CIDR[,CIDR]",
        help=f"允许传入 XFF 的反代地址/网段，默认 {', '.join(DEFAULT_TRUSTED_PROXIES)}；"
        "也可用环境变量 AUDIOFORGE_TRUSTED_PROXY",
    )
    p.add_argument("--ffmpeg", **_FFMPEG_OPT)
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
