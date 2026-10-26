"""命令行入口：``audioforge`` / ``python -m audioforge``。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, ncm, transcode
from .server import ServerConfig, serve

__all__ = ["main"]

_FFMPEG_OPT = dict(
    dest="ffmpeg", default=argparse.SUPPRESS, metavar="路径",
    help="显式指定 ffmpeg 可执行文件（也可用顶层 --ffmpeg 或环境变量 AUDIOFORGE_FFMPEG）",
)

_AUDIO_EXT = {
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".ape",
    ".wma", ".ncm", ".aif", ".alac",
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
                    path, out_dir, args.to, quality=args.quality,
                    bitrate=args.bitrate, ffmpeg=args.ffmpeg,
                )
                line += f"  → {path.name}"
            print(line)
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
                src, out_dir, args.format, quality=args.quality,
                bitrate=args.bitrate, ffmpeg=args.ffmpeg,
            )
            layer = transcode.quality_label(transcode.FORMATS[args.format], args.quality, args.bitrate)
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
        print(pad(name, 8) + pad(f.ext, 9) + pad(f.codec, 13) + pad(kind, 6)
              + pad(tiers, 21) + f.note)
    print()
    ff = transcode.find_ffmpeg()
    print(f"ffmpeg: {transcode.ffmpeg_version(ff) if ff else '未找到（通用转码不可用，NCM 通道仍可用）'}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = ServerConfig(
        host=args.host,
        port=args.port,
        max_bytes=int(args.max_mb * (1 << 20)),
        ttl=args.ttl,
        work_dir=Path(args.workdir) if args.workdir else None,
        ffmpeg=args.ffmpeg,
        quiet=args.quiet,
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
                print(f"{r['file']}  {r['format'].upper()}  {dur}  "
                      f"{r['bitrate'] or '?'}bps  {r['title'] or '?'} — {who}")
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

    common_out = dict(metavar="目录", help="输出目录，默认与源文件同目录")

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
