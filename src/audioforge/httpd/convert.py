"""``POST /api/convert``：spool 落盘 → mmap 解析 → 配额复核 → 转换 → 结果。

几条不变量：

* 请求体永远先落盘再解析（内存占用与上传大小无关）；
* **配额按真实文件数复核**（旧实现里 ``check_quota`` 的 ``n_files`` 取默认 1，
  一次塞 N 个文件只扣 1 个额度，实测 ``files_per_day=1`` 时批量传 3 个全过）；
* 单个文件失败不影响同批其它文件，错误按文件回传。
"""

from __future__ import annotations

import re
import uuid
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote

from .. import ncm, transcode
from ..guard import Denied
from ..multipart import MultipartError, get_boundary, parse_multipart

__all__ = ["handle"]

_SPOOL_BLOCK = 1 << 16
_JOB_ID = re.compile(r"^[0-9a-fA-F]{8,64}$")


def handle(h) -> None:
    cfg = h.server.cfg
    guard = h.server.guard
    jobs = h.server.jobs

    length = h.headers.get("Content-Length")
    if not length:
        h._error(HTTPStatus.LENGTH_REQUIRED, "缺少 Content-Length")
        return
    try:
        total = int(length)
    except ValueError:
        h._error(HTTPStatus.BAD_REQUEST, "Content-Length 非法")
        return
    if total <= 0:
        h._error(HTTPStatus.BAD_REQUEST, "请求体为空")
        return
    if total > cfg.max_bytes:
        h._reject_body(total)
        h._error(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"上传超过上限 {cfg.max_bytes / (1 << 20):.0f} MB"
        )
        return

    ip = h._client_ip()
    # 提交前先按声明大小核字节配额、腾磁盘，避免收完 200 MB 才发现不让传
    try:
        guard.check_quota(ip, n_bytes=total)
    except Denied as exc:
        h._reject_body(total)
        h._error(exc.status, exc.message, exc.headers)
        return
    guard.ensure_disk()

    token = uuid.uuid4().hex
    job_dir = cfg.jobs_dir / token
    upload_dir = job_dir / "upload"
    out_dir = job_dir / "out"
    spool = job_dir / "body.part"
    job_key: str | None = None
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        _spool(h, spool, total)

        boundary = get_boundary(h.headers.get("Content-Type", ""))
        parts = parse_multipart(spool, boundary, upload_dir)
        fields = {p.name: p.text() for p in parts if p.name and not p.is_file}
        files = [p for p in parts if p.is_file]
        if not files:
            h._error(HTTPStatus.BAD_REQUEST, "没有收到文件")
            return

        # 以真实文件数复核配额（按请求计数会让批量上传白拿额度）
        try:
            guard.check_quota(ip, n_bytes=0, n_files=len(files))
        except Denied as exc:
            h._error(exc.status, exc.message, exc.headers)
            return

        requested = (fields.get("job") or "").strip()
        job_key = requested if _JOB_ID.match(requested) else token
        jobs.start(job_key, total=len(files))

        target = (fields.get("target") or "auto").strip().lower()
        quality = (fields.get("quality") or "high").strip().lower()
        bitrate = (fields.get("bitrate") or "").strip() or None
        # 表单勾选框：浏览器传 "on"，也接受 1/true/yes
        want_tags = (fields.get("tags") or "").strip().lower() in ("1", "true", "on", "yes")

        jobs.phase(job_key, "converting")
        with h.server.slots:
            results = []
            for part in files:
                result = _convert_one(
                    h, part, out_dir, target, quality, bitrate, token, tags=want_tags
                )
                results.append(result)
                jobs.add(job_key, result)

        done = sum(1 for r in results if r["ok"])
        guard.record(ip, done, total)
        guard.note_disk(total)
        jobs.finish(job_key)
        h._json({"ok": True, "job": token, "results": results, "status": f"/api/job/{job_key}"})
    except MultipartError as exc:
        if job_key:
            jobs.finish(job_key, error=str(exc))
        h._error(HTTPStatus.BAD_REQUEST, str(exc))
    except ConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        if job_key:
            jobs.finish(job_key, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        spool.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 单文件
# --------------------------------------------------------------------------- #


def _spool(h, path: Path, total: int) -> None:
    """把请求体分块写盘，避免整包进内存。"""
    left = total
    with path.open("wb") as fp:
        while left > 0:
            chunk = h.rfile.read(min(_SPOOL_BLOCK, left))
            if not chunk:
                raise MultipartError("请求体提前结束")
            fp.write(chunk)
            left -= len(chunk)


def _convert_one(
    h,
    part,
    out_dir: Path,
    target: str,
    quality: str,
    bitrate: str | None,
    token: str,
    *,
    tags: bool = False,
) -> dict:
    src: Path = part.path
    name = part.filename or src.name
    ext = src.suffix.lower()
    ffmpeg = h.server.ffmpeg
    cap = h.server.cfg.max_out_bytes
    try:
        if ext == ".ncm":
            decoded, info = ncm.decrypt(src, out_dir, extract_cover=False, tags=tags)
            produced = decoded
            note = f"NCM 解密（{info.fmt}，码率 {info.bitrate or '未知'}）"
            if info.tagged:
                note += "，含 ID3 标签"
            warn = _size_warning(info)
            want = target if target not in ("auto", "", info.fmt) else ""
            if want:
                produced = transcode.convert(
                    decoded,
                    out_dir,
                    want,
                    quality=quality,
                    bitrate=bitrate,
                    ffmpeg=ffmpeg,
                    max_bytes=cap,
                )
                decoded.unlink(missing_ok=True)
                note += f" → {want}"
            return _ok_item(h, name, produced, note, info.summary(), token, warning=warn)

        if target in ("auto", ""):
            raise ValueError("非 NCM 文件必须指定目标格式")
        produced = transcode.convert(
            src,
            out_dir,
            target,
            quality=quality,
            bitrate=bitrate,
            ffmpeg=ffmpeg,
            max_bytes=cap,
        )
        return _ok_item(h, name, produced, f"转码 → {target}", {}, token)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "source": name,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _size_warning(info: ncm.NcmInfo) -> str | None:
    """元数据推算体积与实得体积相差过大时给一句人话提示（不阻断）。"""
    check = getattr(info, "size_check", None) or {}
    if not check or check.get("consistent", True):
        return None
    return (
        f"解出 {check['actual_bytes']} 字节，按元数据（时长×码率）推算约 "
        f"{check['expected_bytes']} 字节，文件可能被截断"
    )


def _ok_item(
    h, source: str, path: Path, note: str, info: dict, token: str, *, warning: str | None = None
) -> dict:
    # token 由调用方显式传入（旧实现用 path.parent.parent.name 反推，目录一改就静默出错）
    item = {
        "ok": True,
        "source": source,
        "name": path.name,
        "size": path.stat().st_size,
        "note": note,
        "url": f"/api/file/{token}/{quote(path.name)}",
        "meta": info,
    }
    if warning:
        item["warning"] = warning
    return item
