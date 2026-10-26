"""通用音频转码（走外部 ffmpeg）。

设计取舍：不把 ffmpeg 塞进 Python 依赖，也不自己实现编解码。这里只做三件事 ——
找到 ffmpeg、把「目标格式」翻译成正确的 codec 参数、把子进程的失败信息翻成人话。
找不到 ffmpeg 时整个模块降级为不可用，但 NCM 通道（纯 Python）不受影响。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TranscodeError",
    "TargetFormat",
    "FORMATS",
    "find_ffmpeg",
    "ffmpeg_version",
    "convert",
    "quality_label",
]


class TranscodeError(RuntimeError):
    """ffmpeg 缺失或转码失败。"""


@dataclass(frozen=True)
class TargetFormat:
    """一种目标格式的编码参数。"""

    name: str
    ext: str
    codec: str
    lossy: bool
    # 三档码率（kbps）：low / medium / high；无损格式为 None
    tiers: tuple[int, int, int] | None = None
    extra: tuple[str, ...] = ()
    note: str = ""

    def bitrate(self, quality: str) -> str | None:
        if not self.lossy or self.tiers is None:
            return None
        idx = {"low": 0, "medium": 1, "high": 2}.get(quality, 2)
        return f"{self.tiers[idx]}k"


FORMATS: dict[str, TargetFormat] = {
    "mp3": TargetFormat("mp3", ".mp3", "libmp3lame", True, (128, 192, 320),
                        note="兼容性最好，有损"),
    "flac": TargetFormat("flac", ".flac", "flac", False,
                         extra=("-compression_level", "5"), note="无损压缩"),
    "wav": TargetFormat("wav", ".wav", "pcm_s16le", False, note="无损未压缩，体积大"),
    "m4a": TargetFormat("m4a", ".m4a", "aac", True, (96, 160, 256),
                        extra=("-movflags", "+faststart"), note="AAC 封装，苹果生态友好"),
    "aac": TargetFormat("aac", ".aac", "aac", True, (96, 160, 256), note="裸 AAC（ADTS）"),
    "ogg": TargetFormat("ogg", ".ogg", "libvorbis", True, (112, 160, 224), note="Ogg Vorbis"),
    "opus": TargetFormat("opus", ".opus", "libopus", True, (64, 96, 160),
                         note="低码率高音质，语音/流媒体首选"),
    "alac": TargetFormat("alac", ".m4a", "alac", False, note="苹果无损"),
    "aiff": TargetFormat("aiff", ".aiff", "pcm_s16be", False, note="未压缩，专业音频交换"),
}

QUALITY_NAMES = ("low", "medium", "high")

# Windows 上常见的非 PATH 安装位置
_WIN_HINTS = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
)


def find_ffmpeg() -> str | None:
    """按优先级定位 ffmpeg：显式环境变量 → PATH → 常见安装路径 → 随包二进制。"""
    for var in ("AUDIOFORGE_FFMPEG", "FFMPEG_BIN", "FFMPEG_PATH"):
        val = os.environ.get(var)
        if val and Path(val).is_file():
            return val

    found = shutil.which("ffmpeg")
    if found:
        return found

    for hint in _WIN_HINTS:
        if sys.platform.startswith("win") and Path(hint).is_file():
            return hint

    # imageio-ffmpeg 会在 site-packages 里塞一份静态二进制，装了就直接用
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # noqa: BLE001
        pass
    return None


def ffmpeg_version(ffmpeg: str | None = None) -> str | None:
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.stdout or "").splitlines()
    return line[0].strip() if line else None


def quality_label(fmt: TargetFormat, quality: str, bitrate: str | None = None) -> str:
    if bitrate:
        return bitrate
    br = fmt.bitrate(quality)
    return br or "无损"


def convert(
    src: str | Path,
    out_dir: str | Path,
    target: str,
    *,
    quality: str = "high",
    bitrate: str | None = None,
    overwrite: bool = True,
    ffmpeg: str | None = None,
    timeout: int = 900,
) -> Path:
    """把任意 ffmpeg 能读的音频转成 ``target`` 格式，返回输出路径。"""
    target = target.lower().lstrip(".")
    fmt = FORMATS.get(target)
    if fmt is None:
        raise TranscodeError(
            f"不支持的目标格式 {target!r}；可选：{', '.join(sorted(FORMATS))}"
        )

    exe = ffmpeg or find_ffmpeg()
    if not exe:
        raise TranscodeError(
            "找不到 ffmpeg。请安装后加入 PATH，或设置环境变量 AUDIOFORGE_FFMPEG 指向可执行文件。"
        )

    src = Path(src)
    if not src.is_file():
        raise TranscodeError(f"文件不存在：{src}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{src.stem}{fmt.ext}"
    if dst.resolve() == src.resolve():
        dst = out_dir / f"{src.stem}.converted{fmt.ext}"
    if dst.exists() and not overwrite:
        raise TranscodeError(f"输出已存在：{dst}")

    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src), "-vn"]
    cmd += ["-map_metadata", "0", "-c:a", fmt.codec]
    br = bitrate or fmt.bitrate(quality)
    if br:
        cmd += ["-b:a", br]
    cmd += list(fmt.extra)
    cmd.append(str(dst))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(f"转码超时（>{timeout}s）：{src.name}") from exc
    except OSError as exc:
        raise TranscodeError(f"无法启动 ffmpeg：{exc}") from exc

    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " | ".join(detail[-3:]) if detail else f"退出码 {proc.returncode}"
        raise TranscodeError(f"ffmpeg 转码失败：{tail}")
    return dst
