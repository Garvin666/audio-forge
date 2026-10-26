"""本地 Web 平台：浏览器上传 → 服务器转换 → 下载。

只用标准库（``http.server`` + ``mmap`` + ``subprocess``），因此 ``pip install`` 之后
一条命令就能跑起来，也方便塞进任何能跑 Python 的容器。

路由::

    GET  /                     上传页
    GET  /app.js /style.css    前端静态资源
    GET  /api/health           能力探测（有没有 ffmpeg、支持哪些格式）
    POST /api/convert          multipart 上传并转换
    GET  /api/file/<job>/<名>  下载转换结果

每次转换生成一个 job 目录（``token`` 为随机串），过期后由后台线程整目录清掉。
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import __version__
from . import ncm, transcode
from .multipart import MultipartError, get_boundary, parse_multipart

__all__ = ["ServerConfig", "create_server", "serve"]

WEB_DIR = Path(__file__).parent / "web"
_SPOOL_BLOCK = 1 << 16


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    max_bytes: int = 200 << 20        # 单次请求上限 200 MiB
    ttl: int = 3600                   # job 目录存活秒数
    work_dir: Path | None = None      # 默认系统临时目录
    concurrency: int = 4
    ffmpeg: str | None = None
    quiet: bool = False
    _root: Path = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        base = Path(self.work_dir) if self.work_dir else Path(tempfile.gettempdir())
        self._root = base / "audioforge"
        (self._root / "jobs").mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def jobs_dir(self) -> Path:
        return self._root / "jobs"


class _Limited(BaseHTTPRequestHandler):
    """带静态资源与 JSON 辅助方法的基类。"""

    server_version = f"audio-forge/{__version__}"
    protocol_version = "HTTP/1.1"
    cfg: ServerConfig
    slots: threading.Semaphore

    # ---------- 基础工具 ---------- #

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        if self.cfg.quiet:
            return
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, status)

    def _static(self, name: str) -> None:
        path = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in path.parents or not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, path.read_bytes(), ctype)

    # ---------- 能力 ---------- #

    def _health(self) -> None:
        ff = transcode.find_ffmpeg() if not self.cfg.ffmpeg else self.cfg.ffmpeg
        self._json({
            "ok": True,
            "version": __version__,
            "ffmpeg": transcode.ffmpeg_version(ff) if ff else None,
            "capabilities": {
                "ncm": True,
                "transcode": bool(ff),
            },
            "formats": sorted(transcode.FORMATS),
            "limits": {
                "max_upload_mb": round(self.cfg.max_bytes / (1 << 20)),
                "job_ttl_minutes": round(self.cfg.ttl / 60),
            },
        })

    # ---------- 路由 ---------- #

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                self._static("index.html")
            elif route in ("/app.js", "/style.css"):
                self._static(route.lstrip("/"))
            elif route == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            elif route == "/api/health":
                self._health()
            elif route.startswith("/api/file/"):
                self._download(route)
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知路径 {route}")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route != "/api/convert":
            self._error(HTTPStatus.NOT_FOUND, f"未知路径 {route}")
            return

        length = self.headers.get("Content-Length")
        if not length:
            self._error(HTTPStatus.LENGTH_REQUIRED, "缺少 Content-Length")
            return
        try:
            total = int(length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Content-Length 非法")
            return
        if total <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "请求体为空")
            return
        if total > self.cfg.max_bytes:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"上传超过上限 {self.cfg.max_bytes / (1 << 20):.0f} MB")
            return

        token = uuid.uuid4().hex
        job = self.cfg.jobs_dir / token
        upload_dir = job / "upload"
        out_dir = job / "out"
        spool = job / "body.part"
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            self._spool(spool, total)

            boundary = get_boundary(self.headers.get("Content-Type", ""))
            parts = parse_multipart(spool, boundary, upload_dir)
            fields = {p.name: p.text() for p in parts if p.name and not p.is_file}
            files = [p for p in parts if p.is_file]

            if not files:
                self._error(HTTPStatus.BAD_REQUEST, "没有收到文件")
                return

            target = (fields.get("target") or "auto").strip().lower()
            quality = (fields.get("quality") or "high").strip().lower()
            bitrate = (fields.get("bitrate") or "").strip() or None

            with self.slots:
                results = [
                    self._convert_one(f, out_dir, target, quality, bitrate) for f in files
                ]

            self._json({"ok": True, "job": token, "results": results})
        except MultipartError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            spool.unlink(missing_ok=True)

    # ---------- 转换 ---------- #

    def _spool(self, path: Path, total: int) -> None:
        """把请求体分块写盘，避免整包进内存。"""
        left = total
        with path.open("wb") as fp:
            while left > 0:
                chunk = self.rfile.read(min(_SPOOL_BLOCK, left))
                if not chunk:
                    raise MultipartError("请求体提前结束")
                fp.write(chunk)
                left -= len(chunk)

    def _convert_one(self, part, out_dir: Path, target: str,
                     quality: str, bitrate: str | None) -> dict:
        src: Path = part.path
        name = part.filename or src.name
        ext = src.suffix.lower()
        try:
            if ext == ".ncm":
                decoded, info = ncm.decrypt(src, out_dir, extract_cover=False)
                produced = decoded
                note = f"NCM 解密（{info.fmt}，码率 {info.bitrate or '未知'}）"
                want = target if target not in ("auto", "", info.fmt) else ""
                if want:
                    produced = transcode.convert(
                        decoded, out_dir, want, quality=quality, bitrate=bitrate,
                        ffmpeg=self.cfg.ffmpeg,
                    )
                    decoded.unlink(missing_ok=True)
                    note += f" → {want}"
                return self._ok_item(name, produced, note, info.summary())

            if target in ("auto", ""):
                raise ValueError("非 NCM 文件必须指定目标格式")
            produced = transcode.convert(
                src, out_dir, target, quality=quality, bitrate=bitrate,
                ffmpeg=self.cfg.ffmpeg,
            )
            return self._ok_item(name, produced, f"转码 → {target}", {})
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "source": name,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _ok_item(self, source: str, path: Path, note: str, info: dict) -> dict:
        token = path.parent.parent.name
        return {
            "ok": True,
            "source": source,
            "name": path.name,
            "size": path.stat().st_size,
            "note": note,
            "url": f"/api/file/{token}/{quote(path.name)}",
            "meta": info,
        }

    def _download(self, route: str) -> None:
        pieces = route.split("/", 4)          # ['', 'api', 'file', token, name]
        if len(pieces) != 5:
            self._error(HTTPStatus.BAD_REQUEST, "下载路径非法")
            return
        token, raw_name = pieces[3], unquote(pieces[4])
        if not token.isalnum():
            self._error(HTTPStatus.BAD_REQUEST, "job 标识非法")
            return
        out_dir = (self.cfg.jobs_dir / token / "out").resolve()
        path = (out_dir / raw_name).resolve()
        if out_dir not in path.parents or not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "文件不存在或已过期")
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        disp = f"attachment; filename*=UTF-8''{quote(path.name)}"
        self._send(HTTPStatus.OK, data, ctype, {"Content-Disposition": disp})


def _cleaner(cfg: ServerConfig, stop: threading.Event) -> None:
    """后台清过期 job 目录。"""
    interval = 300
    while not stop.wait(interval):
        cutoff = time.time() - cfg.ttl
        for d in cfg.jobs_dir.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def create_server(cfg: ServerConfig, *, start_cleaner: bool = True) -> ThreadingHTTPServer:
    handler = type("Handler", (_Limited,), {"cfg": cfg, "slots": threading.Semaphore(cfg.concurrency)})
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    httpd.daemon_threads = True
    if start_cleaner:
        stop = threading.Event()
        threading.Thread(target=_cleaner, args=(cfg, stop), daemon=True).start()
        httpd._cleaner_stop = stop  # type: ignore[attr-defined]
    return httpd


def serve(cfg: ServerConfig) -> int:
    httpd = create_server(cfg)
    ff = cfg.ffmpeg or transcode.find_ffmpeg()
    shown = cfg.host if cfg.host not in ("0.0.0.0", "") else "127.0.0.1"
    print(f"audio-forge {__version__}  http://{shown}:{cfg.port}")
    print(f"  临时目录 : {cfg.root}")
    print(f"  上传上限 : {cfg.max_bytes / (1 << 20):.0f} MB")
    print(f"  通用转码 : {transcode.ffmpeg_version(ff) if ff else '不可用（未找到 ffmpeg，仅 NCM 通道可用）'}")
    print("  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.shutdown()
        httpd.server_close()
        stop = getattr(httpd, "_cleaner_stop", None)
        if stop is not None:
            stop.set()
    return 0
