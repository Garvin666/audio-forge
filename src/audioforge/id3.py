"""ID3v2.3 标签写入（纯 Python，只服务 NCM 解密出的 mp3）。

为什么自己拼字节、而不是拉一个标签库：本项目运行时依赖刻意保持为零，为写四个文本帧
引第三方库不划算；而且这里只需要「在文件最前面拼一个完整的 ID3 头」，不需要读或改
已有标签。

版本选 **ID3v2.3** 而不是 v2.4：v2.3 的兼容面最广（Windows 资源管理器、老车机、各类
手机播放器都认），代价是文本必须用 UTF-16 + BOM —— 中文歌名在 v2.3 下只能这么写。

几条刻意的限制：

* **默认不写**。`.ncm` 解密的主卖点是「逐字节还原码流」，往里加标签就改变了输出字节，
  所以只有显式要（CLI ``--tags`` / HTTP ``tags=1``）才写。
* 码流本身已经带 ``ID3`` 头时**不重复写**：双标签会让不同播放器各读一个，出现
  「同一个文件在两台设备上显示不同歌名」。
* 只写 mp3。flac 该用 Vorbis comment，wav 没有标签位——写到这些容器里等于制造垃圾。
"""

from __future__ import annotations

__all__ = ["MAX_COVER_BYTES", "build_tag", "looks_tagged"]

# 封面超过这个大小就放弃：标签是前置在音频前面的，塞一张 10 MB 的图会让「解密」变成
# 「解压」，而且大多数播放器读封面都会先整段读进内存。
MAX_COVER_BYTES = 8 << 20

_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
}


def _synchsafe(value: int) -> bytes:
    """ID3 头里的长度是「7 位一字节」的防同步整数（每字节最高位恒 0）。"""
    if not 0 <= value < 1 << 28:
        raise ValueError(f"标签长度超出 ID3v2 上限：{value}")
    return bytes(
        (
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        )
    )


def _frame(frame_id: str, payload: bytes) -> bytes:
    """一个 ID3v2.3 帧：4 字节 ID + 4 字节大端长度 + 2 字节标志 + 载荷。"""
    if len(payload) > 0x7FFFFFFF:
        raise ValueError(f"{frame_id} 帧过大：{len(payload)} 字节")
    return frame_id.encode("ascii") + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _text_frame(frame_id: str, value: str) -> bytes:
    """文本帧：编码字节 0x01（UTF-16 带 BOM）+ BOM + 正文。

    v2.3 的文本帧不做 nul 结尾，正文长度就是帧长度；多值用 ``\\x00`` 分隔，这里不用。
    """
    return _frame(frame_id, b"\x01" + b"\xff\xfe" + value.encode("utf-16-le"))


def _apic_frame(cover: bytes, mime: str) -> bytes:
    """封面帧：编码(ISO-8859-1) + MIME\\0 + 图片类型(3=正面封面) + 描述\\0 + 图片数据。"""
    head = b"\x00" + mime.encode("ascii", "replace") + b"\x00" + b"\x03" + b"\x00"
    return _frame("APIC", head + cover)


def looks_tagged(head: bytes) -> bool:
    """码流开头是不是已经有一个 ID3v2 头。"""
    return head[:3] == b"ID3"


def build_tag(
    *,
    title: str | None = None,
    artists: list[str] | tuple[str, ...] = (),
    album: str | None = None,
    cover: bytes | None = None,
    cover_ext: str | None = None,
) -> bytes:
    """拼一个完整的 ID3v2.3 标签；没有任何可写字段时返回 ``b""``。

    返回的字节要**前置**到 mp3 码流最前面（ID3v2 规定标签在文件头）。
    """
    frames: list[bytes] = []
    if title:
        frames.append(_text_frame("TIT2", title))
    names = [a for a in artists if a]
    if names:
        # 多个艺人按 ID3 惯例用 "/" 连起来塞进一个 TPE1，避免多值分隔符各家实现不一
        frames.append(_text_frame("TPE1", "/".join(names)))
    if album:
        frames.append(_text_frame("TALB", album))
    if cover and len(cover) <= MAX_COVER_BYTES:
        mime = _MIME_BY_EXT.get((cover_ext or "jpg").lower(), "image/jpeg")
        frames.append(_apic_frame(cover, mime))

    if not frames:
        return b""
    body = b"".join(frames)
    return b"ID3\x03\x00\x00" + _synchsafe(len(body)) + body
