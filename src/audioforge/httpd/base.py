"""HTTP 基类：路由分发、响应助手、鉴权/限速闸门、请求体处置。

拆分说明：``server.py`` 原本是 483 行单文件（路由 + 上传 + 转换 + 下载 + 静态资源 +
启动横幅），现在按职责拆到 ``audioforge.httpd`` 包；``audioforge.server`` 保留为
兼容门面，旧导入路径 ``from audioforge.server import ServerConfig, create_server``
仍然可用。
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import mimetypes
import re
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..guard import Denied
from . import convert, download, health

__all__ = ["BaseHandler", "mask_secrets"]

_SPOOL_BLOCK = 1 << 16
# 拒绝请求时最多读掉这么多 body 再回错误；超过就不读、直接关连接
_DRAIN_CAP = 32 << 20
# 这些路径不含数据，且 Bearer 模式下页面必须先加载出来才能取到令牌
_PUBLIC_PATHS = ("/", "/index.html", "/app.js", "/style.css", "/favicon.ico")
_PING_PATH = "/api/ping"
_READY_PATH = "/api/ready"

# 访问日志会进 stdout / journald / 反代日志：query 里的凭证必须打码
_SECRET_QUERY = re.compile(r"(?i)([?&](?:token|access_token)=)[^&\s]+")


def mask_secrets(text: str) -> str:
    """把请求行里的 ``?token=…`` 替换成 ``?token=***``。"""
    return _SECRET_QUERY.sub(r"\1***", text)


def _valid_ip(raw: str) -> str | None:
    try:
        return str(ipaddress.ip_address(raw.strip().strip("[]")))
    except ValueError:
        return None


def _is_trusted(peer: str, trusted: tuple[str, ...]) -> bool:
    candidate = _valid_ip(peer)
    if candidate is None:
        return False
    addr = ipaddress.ip_address(candidate)
    for entry in trusted:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


class BaseHandler(BaseHTTPRequestHandler):
    """带静态资源、JSON、鉴权闸门与连接体处置的请求处理器。

    运行期依赖 ``self.server`` 上的几个属性：``cfg`` / ``guard`` / ``slots`` /
    ``dl_slots`` / ``jobs`` / ``ffmpeg`` / ``ffmpeg_version``（见 ``httpd.app.AppServer``）。
    """

    server_version = f"audio-forge/{__version__}"
    protocol_version = "HTTP/1.1"

    # ---------- 生命周期 ---------- #

    def setup(self) -> None:
        # 实例属性优先于类属性；StreamRequestHandler.setup() 会据此 settimeout。
        # typeshed 把 timeout 标成 ClassVar，但标准库正是靠实例属性覆盖它 —— 有意为之。
        with contextlib.suppress(AttributeError):
            self.timeout = self.server.cfg.conn_timeout  # type: ignore[misc, attr-defined]
        self._responded = False
        super().setup()

    def handle_one_request(self) -> None:
        """把「客户端半路 RST」当成正常收尾。

        标准库只在 ``handle_one_request`` 里捕 ``socket.timeout``；客户端提前断开时抛的
        ``ConnectionResetError`` / ``ConnectionAbortedError``（Windows 10054 / 10053）会
        一路冒到 ``socketserver.handle_error``，在日志里刷一整段回溯 —— 实测一天能刷几千行，
        真正的异常反而被埋掉。
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        try:
            quiet = self.server.cfg.quiet  # type: ignore[attr-defined]
        except AttributeError:
            quiet = False
        if quiet:
            return
        print(f"[{self.log_date_time_string()}] {self.address_string()} {mask_secrets(fmt % args)}")

    # ---------- 基础工具 ---------- #

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str,
        extra: dict | None = None,
        *,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        if status != HTTPStatus.NOT_MODIFIED:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self._responded = True
        try:
            self.end_headers()
            if self.command != "HEAD" and status != HTTPStatus.NOT_MODIFIED and body:
                self.wfile.write(body)
        except OSError:
            # 客户端提前断开（RST / 关连接）：记一笔就走，别再往上抛一层回溯
            self.close_connection = True

    def _json(self, obj, status: int = 200, *, cache: str = "no-store") -> None:
        self._send(
            status,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=cache,
        )

    def _error(self, status: int, msg: str, headers: dict | None = None) -> None:
        """错误响应；``headers`` 用于 401 的挑战头、429 的 Retry-After、416 的 Content-Range。"""
        payload = json.dumps({"ok": False, "error": msg}, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", headers)

    def _internal(self, exc: BaseException, *, where: str = "") -> None:
        """500：细节只进日志，响应里给一个可对账的 request id。"""
        rid = uuid.uuid4().hex[:8]
        print(f"[error {rid}] {where or self.command} {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        if self._responded:
            self.close_connection = True
            return
        self._send(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            json.dumps(
                {"ok": False, "error": "服务器内部错误", "id": rid}, ensure_ascii=False
            ).encode("utf-8"),
            "application/json; charset=utf-8",
            {"X-Request-Id": rid},
        )

    def _client_ip(self) -> str:
        """客户端 IP；仅在显式信任反代时才读 X-Forwarded-For。

        不设开关就无条件信任 XFF 等于把配额白白送给伪造头的人；设了开关但不校验
        来源，同样能被绕过。所以这里要求「开关打开 **且** 直连方在 trusted_proxies
        里」两个条件同时成立。
        """
        peer = self.client_address[0]
        cfg = self.server.cfg  # type: ignore[attr-defined]
        if not cfg.trust_proxy or not _is_trusted(peer, cfg.trusted_proxies):
            return peer
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if not forwarded:
            forwarded = (self.headers.get("X-Real-IP") or "").strip()
        return _valid_ip(forwarded) or peer

    # ---------- 闸门与请求体 ---------- #

    def _gate(self, *, check_rate: bool = True) -> bool:
        """鉴权 + 限速。被拒时已写好响应，返回 False。

        配额不在这里 —— 它需要知道请求体多大、几个文件，而且要先把 body 读掉
        再回错误，单独在 ``convert.handle`` 里处理。
        """
        ip = self._client_ip()
        # 下载是 <a href> 直连，带不了自定义头，所以也接受 ?token=（只在没有头时兜底）
        query_token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        headers = {
            "Authorization": self.headers.get("Authorization", "") or "",
            "X-Auth-Token": self.headers.get("X-Auth-Token", "") or query_token,
        }
        try:
            self.server.guard.verify(headers)  # type: ignore[attr-defined]
            if check_rate:
                self.server.guard.check_rate(ip)  # type: ignore[attr-defined]
        except Denied as exc:
            # 先读掉 body 再回错误：不读的话残余字节会被当成下一条请求行
            self._reject_body(self._body_length())
            self._error(exc.status, exc.message, exc.headers)
            return False
        return True

    def _drain(self, total: int) -> None:
        """把请求体读掉丢弃（客户端还在 sendall 时直接关连接会吃 RST）。"""
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
          ``BaseHTTPRequestHandler`` 当成下一条请求行（``400 Bad request syntax``），
          这也是请求走私的口子，必须关连接。
        """
        if total <= 0:
            return
        if total > _DRAIN_CAP:
            self.close_connection = True
            return
        self._drain(total)

    # ---------- 静态资源 ---------- #

    def _static(self, name: str) -> None:
        web_dir = self.server.web_dir  # type: ignore[attr-defined]
        path = (web_dir / name).resolve()
        if web_dir.resolve() not in path.parents or not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        stat = path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self._send(
                HTTPStatus.NOT_MODIFIED, b"", "", {"ETag": etag}, cache="public, max-age=300"
            )
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "text/javascript"):
            ctype += "; charset=utf-8"
        self._send(
            HTTPStatus.OK, path.read_bytes(), ctype, {"ETag": etag}, cache="public, max-age=300"
        )

    # ---------- 路由 ---------- #

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            # 放在 _gate 之前：探活不该需要凭证，也不该占用限速额度
            if route == _PING_PATH:
                health.ping(self)
                return
            if route == _READY_PATH:
                health.ready(self)
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
                health.health(self)
            elif route.startswith("/api/job/"):
                health.job_status(self, route)
            elif route.startswith("/api/file/"):
                download.handle(self, route)
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知路径 {route}")
        except ConnectionError:
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._internal(exc, where="GET " + route)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            if route != "/api/convert":
                self._reject_body(self._body_length())
                self._error(HTTPStatus.NOT_FOUND, f"未知路径 {route}")
                return
            if not self._gate():
                return
            convert.handle(self)
        except ConnectionError:
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._internal(exc, where="POST " + route)
