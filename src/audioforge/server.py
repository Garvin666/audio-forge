"""本地 Web 平台：浏览器上传 → 服务器转换 → 下载。

只用标准库（``http.server`` + ``mmap`` + ``subprocess``），因此 ``pip install`` 之后
一条命令就能跑起来，也方便塞进任何能跑 Python 的容器。

路由::

    GET  /                     上传页
    GET  /app.js /style.css    前端静态资源
    GET  /api/ping             探活（恒 200，无凭证、不计限速）
    GET  /api/health           能力探测（有没有 ffmpeg、支持哪些格式、当前用量）
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
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from . import ncm, transcode
from .guard import Denied, Guard, Limits
from .multipart import MultipartError, get_boundary, parse_multipart

__all__ = ["ServerConfig", "create_server", "serve"]

WEB_DIR = Path(__file__).parent / "web"
_SPOOL_BLOCK = 1 << 16
# 拒绝请求时最多读掉这么多 body 再回错误；超过就不读、直接关连接
_DRAIN_CAP = 32 << 20
# 这些路径不含数据，且 Bearer 模式下页面必须先加载出来才能取到令牌
_PUBLIC_PATHS = ("/", "/index.html", "/app.js", "/style.css", "/favicon.ico")
# 探活口：不校验凭证、不计限速，只回 {"ok": true}。
# 容器 healthcheck 与监控探针走这里，就不必把带用量明细的 /api/health 公开，
# 也不会每 30 秒吃掉一次每 IP 额度。
_PING_PATH = "/api/ping"


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
    auth_user: str | None = None
    auth_pass: str | None = None
    access_token: str | None = None
    limits: Limits = field(default_factory=Limits)
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
    guard: Guard
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

    def _error(self, status: int, msg: str, headers: dict | None = None) -> None:
        self._send(status, json.dumps({"ok": False, "error": msg}, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    def _gate(self, *, check_rate: bool = True) -> bool:
        """鉴权 + 限速。被拒时已写好响应，返回 False。

        配额不在这里 —— 它需要知道请求体多大，而且要先把 body 读掉再回错误，
        单独放在 ``do_POST`` 里处理。
        """
        ip = self.client_address[0]
        # 下载是 <a href> 直连，带不了自定义头，所以也接受 ?token=（只在没有头时兜底）
        query_token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        headers = {
            "Authorization": self.headers.get("Authorization", "") or "",
            "X-Auth-Token": self.headers.get("X-Auth-Token", "") or query_token,
        }
        try:
            self.guard.verify(headers)
            if check_rate:
                self.guard.check_rate(ip)
        except Denied as exc:
            # 先读掉 body 再回错误：不读的话残余字节会被当成下一条请求行
            self._reject_body(self._body_length())
            self._error(exc.status, exc.message, exc.headers)
            return False
        return True

    def _drain(self, total: int) -> None:
        """把请求体读掉丢弃。

        拒绝时如果不读 body，客户端可能还在 ``sendall`` 就被 RST，拿不到我们写好的
        错误响应（Windows 上表现为 ConnectionResetError）。读之前先估大小，见
        ``_reject_body``。
        """
        left = total
        try:
            while left > 0:
                chunk = self.rfile.read(min(_SPOOL_BLOCK, left))
                if not chunk:
                    return
                left -= len(chunk)
        except OSError:
            self.close_connection = True

    def _body_length(self) -> int:
        """请求头里声明的 body 大小，解析不了就当 0。"""
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _reject_body(self, total: int) -> None:
        """拒绝请求前处理还没读的请求体。

        两条路，走错哪条都出事：

        * **读干净再回错误** —— 客户端还在发送时直接关连接，服务端会发 RST，
          客户端拿不到写好的 401/429。
        * **太大就不读、置 close_connection** —— 残留的分段字节会被
          ``BaseHTTPRequestHandler`` 当成下一条请求行，日志里冒出
          ``400 Bad request syntax``；这也是请求走私的口子，必须关连接。
        """
        if total <= 0:
            return
        if total > _DRAIN_CAP:
            self.close_connection = True
            return
        self._drain(total)

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
                "quota": self.guard.limits.as_dict(),
            },
            "auth": {
                "mode": self.guard.mode,
                "usage": self.guard.usage(self.client_address[0]),
                "disk_mb": round(self.guard.disk_usage_mb(), 1),
            },
        })

    # ---------- 路由 ---------- #

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            # 放在 _gate 之前：探活不该需要凭证，也不该占用限速额度
            if route == _PING_PATH:
                self._json({"ok": True})
                return
            if route not in _PUBLIC_PATHS and not self._gate():
                return
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
            self._reject_body(self._body_length())
            self._error(HTTPStatus.NOT_FOUND, f"未知路径 {route}")
            return
        if not self._gate():
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
            self._reject_body(total)
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"上传超过上限 {self.cfg.max_bytes / (1 << 20):.0f} MB")
            return

        # 提交前先按声明大小核配额、腾磁盘，避免收完 200 MB 才发现不让传
        ip = self.client_address[0]
        try:
            self.guard.check_quota(ip, n_bytes=total)
        except Denied as exc:
            self._reject_body(total)
            self._error(exc.status, exc.message, exc.headers)
            return
        self.guard.ensure_disk()

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

            done = sum(1 for r in results if r["ok"])
            self.guard.record(ip, done, total)
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
    guard = Guard(
        user=cfg.auth_user,
        password=cfg.auth_pass,
        token=cfg.access_token,
        limits=cfg.limits,
        jobs_dir=cfg.jobs_dir,
    )
    handler = type("Handler", (_Limited,), {
        "cfg": cfg,
        "guard": guard,
        "slots": threading.Semaphore(cfg.concurrency),
    })
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    httpd.daemon_threads = True
    if start_cleaner:
        stop = threading.Event()
        threading.Thread(target=_cleaner, args=(cfg, stop), daemon=True).start()
        httpd._cleaner_stop = stop  # type: ignore[attr-defined]
    return httpd


def _describe_limits(limits: Limits) -> str:
    bits = []
    if limits.per_minute > 0:
        bits.append(f"{limits.per_minute} 次/分")
    if limits.files_per_day > 0:
        bits.append(f"{limits.files_per_day} 文件/日")
    if limits.mb_per_day > 0:
        bits.append(f"{limits.mb_per_day / 1024:.0f} GB/日")
    if limits.disk_mb > 0:
        bits.append(f"磁盘 {limits.disk_mb / 1024:.0f} GB")
    return " · ".join(bits) if bits else "不限制"


def serve(cfg: ServerConfig) -> int:
    httpd = create_server(cfg)
    guard: Guard = httpd.RequestHandlerClass.guard  # type: ignore[attr-defined]
    ff = cfg.ffmpeg or transcode.find_ffmpeg()
    shown = cfg.host if cfg.host not in ("0.0.0.0", "") else "127.0.0.1"
    print(f"audio-forge {__version__}  http://{shown}:{cfg.port}")
    print(f"  临时目录 : {cfg.root}")
    print(f"  上传上限 : {cfg.max_bytes / (1 << 20):.0f} MB")
    print(f"  通用转码 : {transcode.ffmpeg_version(ff) if ff else '不可用（未找到 ffmpeg，仅 NCM 通道可用）'}")
    if guard.mode == "none":
        print("  鉴权     : 未启用 —— 任何能访问该端口的人都能上传，只应在本机使用")
    else:
        who = f"用户 {cfg.auth_user}" if guard.has_basic else ""
        tok = "Bearer 令牌" if cfg.access_token else ""
        print(f"  鉴权     : {guard.mode}（{' + '.join(x for x in (who, tok) if x)}）")
    print(f"  配额     : {_describe_limits(cfg.limits)}")
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
