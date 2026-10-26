"""HTTP 层：配置、处理器基类、路由、装配。

``audioforge.server`` 是这里的兼容门面 —— 历史代码与测试都用那个路径导入。
"""

from __future__ import annotations

from .app import AppServer, create_server, serve
from .base import BaseHandler, mask_secrets
from .config import DEFAULT_TRUSTED_PROXIES, ServerConfig

__all__ = [
    "AppServer",
    "BaseHandler",
    "DEFAULT_TRUSTED_PROXIES",
    "ServerConfig",
    "create_server",
    "mask_secrets",
    "serve",
]
