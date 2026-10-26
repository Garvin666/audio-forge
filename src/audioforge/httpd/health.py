"""`/api/ping`（探活）、`/api/ready`（就绪）、`/api/health`（能力探测）、`/api/job/<id>`（进度轮询）。

health 曾经每次请求都做三件重活：``find_ffmpeg()`` 扫 PATH、起一次 ``ffmpeg -version``
子进程、``rglob`` 整个 jobs 目录。实测在 601 个 job 时单次 health 要 679 ms，
而 compose 的 healthcheck 超时是 5 s —— 探针先于业务被打死。
现在 ffmpeg 的探测结果在 ``AppServer`` 建服务时算一次，磁盘用量走 ``Guard`` 的
增量计数（30 s 兜底重扫一次）。

三个探针口径不同，别混用：

* ``/api/ping`` —— 活着的证明。不碰磁盘、不看配置，恒定 ``{"ok": true}``；
  容器 healthcheck / 进程守护走这里。
* ``/api/ready`` —— 能不能干活。只做一次 ``is_dir()``（<0.1 ms），
  挂载卷掉了 / 目录被删了就回 503，负载均衡摘流走这里。
* ``/api/health`` —— 给人看的明细（版本、ffmpeg、限额、用量），代价比前两者高一个量级。
"""

from __future__ import annotations

import platform
import re
import time
from http import HTTPStatus

from .. import __version__, qmc, transcode

__all__ = ["ping", "ready", "health", "job_status", "stats"]

_JOB_ID = re.compile(r"^[0-9a-fA-F]{8,64}$")


def ping(h) -> None:
    """不校验凭证、不计限速、不带任何明细：容器探针与监控走这里。"""
    h._json({"ok": True})


def ready(h) -> None:
    """就绪：只回 ok，但要求工作目录还在。

    容器里最常见的「活着但干不了活」是卷没挂上或目录被清掉 ——
    ``ensure_dirs()`` 只在启动时跑过一次，之后没人复查。这里补这一次 ``is_dir()``。
    """
    jobs_dir = h.server.cfg.jobs_dir
    if jobs_dir.is_dir():
        h._json({"ok": True, "ready": True})
    else:
        h._json(
            {"ok": False, "ready": False, "error": f"工作目录不可用：{jobs_dir}"},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )


def health(h) -> None:
    srv = h.server
    cfg = srv.cfg
    guard = srv.guard
    h._json(
        {
            "ok": True,
            "version": __version__,
            "ffmpeg": srv.ffmpeg_version,
            "capabilities": {
                "ncm": True,
                "qmc": True,
                "transcode": bool(srv.ffmpeg),
            },
            # 加密容器后缀由后端下发，前端不重抄这 17 个（.ncm + QMC 家族）
            "encrypted_ext": [".ncm", *(f".{ext}" for ext in sorted(qmc.EXTENSIONS))],
            "formats": sorted(transcode.FORMATS),
            "limits": {
                "max_upload_mb": round(cfg.max_bytes / (1 << 20)),
                "max_output_mb": cfg.max_out_mb,
                "job_ttl_minutes": round(cfg.ttl / 60),
                "quota": guard.limits.as_dict(),
            },
            "server": {
                "conn_timeout_s": cfg.conn_timeout,
                "concurrency": cfg.concurrency,
                "download_slots": cfg.slots_for_download,
                "trust_proxy": cfg.trust_proxy,
            },
            "auth": {
                "mode": guard.mode,
                "usage": guard.usage(h._client_ip()),
                "disk_mb": round(guard.disk_usage_mb(), 1),
            },
        }
    )


def stats(h) -> None:
    """运行指标（``GET /api/stats``）：给运维看，不是给探针看的。

    全部取自内存里已有的计数（jobs 表、Guard 的桶与磁盘增量），不遍历目录、
    不起子进程 —— 否则就成了 P1-1 修掉的那个毛病换了个入口。
    """
    srv = h.server
    cfg = srv.cfg
    guard = srv.guard
    up = time.time() - getattr(srv, "started_at", time.time())
    h._json(
        {
            "ok": True,
            "version": __version__,
            "uptime_s": round(max(0.0, up), 1),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "jobs": srv.jobs.counts(),
            "ips": guard.bucket_count(),
            "disk": {
                "jobs_mb": round(guard.disk_usage_mb(), 1),
                "limit_mb": cfg.limits.disk_mb,
            },
            "limits": {
                "max_upload_mb": round(cfg.max_bytes / (1 << 20)),
                "max_output_mb": cfg.max_out_mb,
                "concurrency": cfg.concurrency,
                "conn_timeout_s": cfg.conn_timeout,
            },
            "ffmpeg": {"found": bool(srv.ffmpeg), "version": srv.ffmpeg_version},
            "auth": {"mode": guard.mode},
        }
    )


def job_status(h, route: str) -> None:
    token = route.split("/", 3)[3] if route.count("/") >= 3 else ""
    if not _JOB_ID.match(token):
        h._error(HTTPStatus.BAD_REQUEST, "job 标识非法")
        return
    snap = h.server.jobs.get(token)
    if snap is None:
        h._error(HTTPStatus.NOT_FOUND, "没有这个 job（可能还没开始转换，或已过期）")
        return
    h._json({"ok": True, **snap})
