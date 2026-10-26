# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

版本号的**唯一来源**是 `src/audioforge/__init__.py` 里的 `__version__`，
`pyproject.toml` 用 `dynamic = ["version"]` 从那里读，CI 会核对 wheel 文件名。

发版流程：改 `__version__` → 把 `[Unreleased]` 改成 `[x.y.z] — 日期` → 打 tag。

## [Unreleased]

一轮以「审计 → 修复」为形式的加固，重点是安全边界、内存与可运维性。
**没有破坏性变更**：旧的导入路径（`audioforge.server`）、CLI 参数、HTTP 契约都兼容。

### 安全

- **访问日志不再记录明文令牌**：`GET /api/file/…?token=…` 里的凭证在入日志前统一替换成
  `?token=***`（`mask_secrets`）。此前访问日志、journald、反代日志里都是明文。
- **日文件配额按真实文件数记账**：`POST /api/convert` 解析出文件列表后重新核一次配额。
  此前 `check_quota` 的 `n_files` 恒为默认值 1，`files_per_day=1` 时一次传 3 个文件全部放行。
- **支持反向代理后的真实 IP（需显式开启）**：新增 `--trust-proxy` / `--trusted-proxy`。
  两个条件同时成立才读 `X-Forwarded-For`：开关打开 **且** 直连方在白名单 CIDR 内。
  默认关闭 —— 不设开关就无条件信任 XFF 等于把配额白送给伪造请求头的人。

### 新增

- `GET /api/job/<id>`：转换进度轮询（阶段 / 总数 / 已完成 / 失败数 / 逐项结果 / 耗时）。
- `GET /api/ready`：就绪探针，只回 `{"ok": true}`；工作目录不可用时回 503。
  与 `/api/ping`（活着的证明，恒 200）分工不同，负载均衡摘流用这个。
- 下载支持 **单段 `Range`**（断点续传）：`206 Partial Content` + `Content-Range`；
  越界回 `416` 并带 `bytes */<size>`。同时下发 `Accept-Ranges: bytes`。
- CLI：`--concurrency`、`--download-slots`、`--conn-timeout`、`--max-out-mb`、
  `--trust-proxy`、`--trusted-proxy`；对应环境变量 `AUDIOFORGE_TRUST_PROXY` /
  `AUDIOFORGE_TRUSTED_PROXY` / `AUDIOFORGE_MAX_OUT_MB`。
- 前端：转换中可**取消**（`xhr.abort()`）；进度改为轮询 job 接口 + 「已用时」计时；
  提交前按 `health.limits.max_upload_mb` 做**客户端大小校验**（含批量总和），
  不再等 3 分钟才收到 413；状态区加 `role="status" aria-live="polite"`。
- 工程化护栏：`ruff`（`E,F,W,I,UP,B,SIM`）、`mypy`（`check_untyped_defs`）、
  `coverage`（门限 80%）、`.pre-commit-config.yaml`、Dependabot、
  `SECURITY.md`、本文件、issue 模板；CI 的 action 全部固定到 commit SHA。

### 变更

- 下载**改为流式**（64 KiB 分块），`HEAD` 不再读盘。实测 96 MB 文件的内存增量
  从 **+96.2 MB（1.00×）** 降到 **+2.3 MB（0.02×）**。
- 磁盘用量改为**增量计数**（30 s 兜底重扫），`/api/health` 不再每次 `rglob` 全目录；
  ffmpeg 探测在建服务时做一次并缓存。601 个 job 时单次 health 从 **679 ms** 降到 **2.5–4.5 ms**。
- `Guard._buckets` 增加**过期淘汰 + 硬上限**（10 万条），此前每个新 IP 留一条、只增不减。
- `ensure_disk()` 加锁并改成单趟扫描（原来的「删一个再扫一次」是 O(n²)）。
- 静态资源改为 `Cache-Control: public, max-age=300` + `ETag`（`If-None-Match` → 304），
  不再对所有响应硬写 `no-store`；API 响应仍是 `no-store`。
- `500` 响应不再回显异常文本（可能带绝对路径与用户名），改为
  `{"ok": false, "error": "服务器内部错误", "id": "<request-id>"}` + `X-Request-Id`，
  细节只进服务端日志。
- **单产物有体积上限**：`ffmpeg -fs` 先兜住，落盘后再核一次大小，超限即丢弃产物并报错。
- **Docker 镜像 832 MB → 279 MB**：基础镜像换成 `python:3.12-alpine`，ffmpeg 改由 apk 提供
  （debian 下 `apt install ffmpeg` 会连带拉进 449 MB 的 libav* 依赖树）。构建参数新增
  `APK_MIRROR` / `PIP_INDEX_URL`（替代原来的 `APT_MIRROR`），国内网络不加会慢到像卡死。
  加固跑法不变，已在 `--read-only --tmpfs /tmp --cap-drop ALL --no-new-privileges` 下复验
  解密 / 转码 / Range / 416 全通。⚠️ musl 下 `imageio-ffmpeg` 自带的 glibc 二进制不可用
  （本项目不装它，且 `find_ffmpeg()` 先查 PATH）。

### 修复

- **截断的 `.ncm` 不再静默产出半截文件**：解密先写 `<name>.<fmt>.part` 再原子改名；
  中途失败（含磁盘满、Ctrl+C）不留半成品。同时用「元数据时长 × 码率」推算应得体积做对照，
  偏差超过 0.5–2.0 倍区间时打 `[WARN]`（**只告警不阻断** —— 这是启发式，不是校验和）。
- 通用转码同样改成 `.<stem>.part<ext>` + 原子改名；临时文件保留目标扩展名，
  否则 ffmpeg 会选错封装器。
- **慢连接不再无限占线程**：`conn_timeout`（默认 60 s）到时即断开。
- **客户端 RST 不再刷回溯**：捕获 `ConnectionError`（含 `ConnectionResetError` /
  `ConnectionAbortedError`），此前 Windows 上一天能刷几千行。
- 码率参数先校验再拼进命令行（只接受 `^\d{2,4}[kK]?$`），避免任意串进 ffmpeg 参数位。

### 重构

- `server.py`（483 行、五合一）拆成 `audioforge.httpd` 包：
  `config` / `base`（路由与闸门）/ `convert` / `download` / `health` / `app`（装配）；
  `audioforge.server` 保留为兼容门面，旧导入路径不变。
- 新增 `audioforge.jobs.JobRegistry`：进程内进度表，TTL 淘汰 + 容量上限。
- `ServerConfig.__post_init__` 不再建目录（配置对象无副作用），改由 `ensure_dirs()` 显式调用。
- 处理器不再用 `type("Handler", ...)` 动态造类，改为 `AppServer(ThreadingHTTPServer)`
  持有 `cfg` / `guard` / `slots` / `dl_slots` / `jobs` / `ffmpeg`。
- `_ok_item()` 的 job token 改为显式传参（此前用 `path.parent.parent.name` 反推，目录一改就静默出错）。

### 测试

- 用例数 **110 → 215**；覆盖率 **83%**（`--fail-under=80`）。
- 新增回归：日志脱敏、批量上传配额、`Range`/`206`/`416`/`HEAD`、`ETag`/`304`、
  job 轮询、`/api/ready` 的 503、XFF 信任模型、`.part` 原子写、体积对照告警、
  `-fs` 参数位置与超限清理、`JobRegistry` 淘汰策略、磁盘计数缓存、CLI 退出码。

## [1.0.0] — 2026-10-26（初始版本）

- NCM 容器解密（纯 Python，手写 AES-128-ECB，零第三方依赖）
- 通用音频转码（调用外部 ffmpeg）：mp3 / flac / wav / m4a / aac / ogg / opus / alac / aiff
- 本地 Web 上传平台（标准库 `ThreadingHTTPServer`）：拖拽上传、批量、进度、结果下载
- 可选鉴权（Basic / Bearer）、按 IP 的限速与日配额、磁盘上限自动清理
- 部署件：Dockerfile / docker-compose / systemd 单元（含 RHEL7 变体）/ nginx 示例
