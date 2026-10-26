"""兼容门面：``audioforge.server`` 仍然导出配置与启动函数。

真正的实现在 :mod:`audioforge.httpd`（2026-10-26 从 483 行单文件拆开）：

* :mod:`audioforge.httpd.config`  —— ``ServerConfig``
* :mod:`audioforge.httpd.base`    —— 请求处理器基类、路由分发、鉴权闸门
* :mod:`audioforge.httpd.convert` —— ``POST /api/convert``
* :mod:`audioforge.httpd.download`—— ``GET /api/file/…``（流式 + Range）
* :mod:`audioforge.httpd.health`  —— ``/api/ping``、``/api/health``、``/api/job/…``
* :mod:`audioforge.httpd.app`     —— ``AppServer`` / ``create_server`` / ``serve``

保留这个模块是为了不破坏旧导入路径（``from audioforge.server import ServerConfig``）。
"""

from __future__ import annotations

from .httpd import AppServer, BaseHandler, ServerConfig, create_server, mask_secrets, serve

__all__ = [
    "AppServer",
    "BaseHandler",
    "ServerConfig",
    "create_server",
    "mask_secrets",
    "serve",
]
