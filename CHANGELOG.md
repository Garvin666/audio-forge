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
- **ID3v2.3 标签回写**（`ncm --tags` / `POST /api/convert` 的 `tags=1` / 网页勾选框）：
  纯 Python 构造 `TIT2`/`TPE1`/`TALB`/`APIC`（封面），**前置**到 mp3 码流前面。
  **默认关闭** —— 主卖点是「码流逐字节还原」，写标签是显式选择；flac 不写（该用
  Vorbis comment），文件自带 ID3 头时不重复写，封面超 8 MiB 跳过。
- `GET /api/stats`：运维视角的运行指标（`uptime_s` / `jobs` 各状态条数 / 见过的 IP 数 /
  磁盘用量 / 限额 / ffmpeg / 鉴权模式）。全部取自内存计数，不遍历目录、不起子进程。
  与 `/api/health`（「这个实例能干什么」）分工不同，这个回答「现在怎么样」。
- CI 矩阵加 **Python 3.14**，`pyproject.toml` 的 classifiers 同步。

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
- **Docker 镜像 832 MB → 202 MB**（两段式）：基础镜像从 debian-slim 换成
  `python:3.12-alpine`（debian 下 `apt install ffmpeg` 会连带拉进 449 MB 的 libav* 依赖树），
  ffmpeg 不再走 `apk add`（`ffmpeg-libs` 是 189 MB 的单体，`libx265`/`libaom`/`libvpx`
  全是 `libavcodec` 的 `DT_NEEDED`，删不掉），改为**多阶段构建 + COPY 静态 ffmpeg 单文件**
  （79.8 MB，连 libc 都不需要，musl 下照跑）。amd64 / arm64 两个包按 `TARGETARCH` 自动选，
  **SHA256 钉在 Dockerfile 里**，上游换包会直接构建失败而不是悄悄换二进制。
  构建参数：`APK_MIRROR` / `PIP_INDEX_URL` / `FFMPEG_URL` / `FFMPEG_SHA256`。
  加固跑法不变，已在 `--read-only --tmpfs /tmp --cap-drop ALL --security-opt
  no-new-privileges` 下复验解密（含 ID3）/ 转码 / Range / 416 / `/api/stats` 全通。
  ⚠️ 代价两条：镜像内 ffmpeg 是 7.0.2（apk 给的是 8.x），且**没有 `ffprobe`**（本项目不调它）。
- **代码全量过一遍 `ruff format`**（23 个文件，纯格式、零逻辑改动），并在 CI 与
  pre-commit 里卡住格式，避免「格式化提交」和「功能提交」混在一起。

### 修复

- **上传被截断不再静默成功**：`multipart` 解析要求请求体以 `--boundary--` 收尾，
  缺了就按「被截断」回 400。此前 `find()` 找不到下一个分隔符时会把剩下的字节当成
  最后一个 part 落盘 —— 产出一个「能过、内容却是半截」的文件，比直接报错难查得多。
- **恶意文件名多挡两类**：Windows 保留设备名（`CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`，
  带扩展名同样算）加 `_` 前缀；文件名里的尾点/尾空格（NTFS 会静默吃掉，导致与本批
  同名文件相撞而 `exists()` 看不出来）在清洗阶段直接去掉。
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

- 用例数 **110 → 250**；覆盖率 **84%**（`--fail-under=80`）。
- 新增回归：日志脱敏、批量上传配额、`Range`/`206`/`416`/`HEAD`、`ETag`/`304`、
  job 轮询、`/api/ready` 的 503、XFF 信任模型、`.part` 原子写、体积对照告警、
  `-fs` 参数位置与超限清理、`JobRegistry` 淘汰策略、磁盘计数缓存、CLI 退出码、
  ID3 标签逐字节布局（自带最小解析器回读）、multipart 截断请求体、
  `/api/stats` 字段与鉴权、`tags=1` 端到端。

## [1.0.0] — 2026-10-26（初始版本）

- NCM 容器解密（纯 Python，手写 AES-128-ECB，零第三方依赖）
- 通用音频转码（调用外部 ffmpeg）：mp3 / flac / wav / m4a / aac / ogg / opus / alac / aiff
- 本地 Web 上传平台（标准库 `ThreadingHTTPServer`）：拖拽上传、批量、进度、结果下载
- 可选鉴权（Basic / Bearer）、按 IP 的限速与日配额、磁盘上限自动清理
- 部署件：Dockerfile / docker-compose / systemd 单元（含 RHEL7 变体）/ nginx 示例
