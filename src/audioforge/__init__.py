"""audio-forge：NCM 容器解密 + 通用音频格式转换。

两个通道互不依赖：

* ``audioforge.ncm``      纯 Python，零第三方依赖，把 .ncm 还原成 mp3/flac 原始码流
* ``audioforge.transcode`` 调外部 ffmpeg，做 mp3/wav/flac/m4a/ogg/opus/alac/aiff 互转

``audioforge.server`` 把两者包成一个本地 Web 平台（标准库 HTTP 服务）。
"""

__version__ = "1.0.0"
__all__ = ["__version__", "ncm", "transcode"]
