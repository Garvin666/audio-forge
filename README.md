# audio-forge

**NCM / QMC 容器解密 + 通用音频格式转换，附一个零依赖的本地 Web 上传平台。**

纯 Python 实现，`pip install` 之后一条命令就能在浏览器里传文件、换格式、下载结果。
AES-128、RC4 变体 keystream、multipart 解析、HTTP 服务全部自己写，**没有任何第三方
Python 依赖**（`pip` 不会下载任何东西）。唯一的可选外部依赖是 ffmpeg，只服务于通用格式互转。

<p>
<img alt="ci" src="https://github.com/Garvin666/audio-forge/actions/workflows/ci.yml/badge.svg">
<img alt="python" src="https://img.shields.io/badge/python-3.9%2B-3776ab">
<img alt="deps" src="https://img.shields.io/badge/dependencies-0-brightgreen">
<img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
<img alt="tests" src="https://img.shields.io/badge/tests-324%20passed-brightgreen">
<img alt="coverage" src="https://img.shields.io/badge/coverage-86%25-green">
</p>

![上传页](docs/screenshot.png)

---

## 三个通道

| 通道 | 输入 | 输出 | 依赖 | 说明 |
| --- | --- | --- | --- | --- |
| **NCM 解密** | `.ncm` | 原始 `mp3` / `flac` | 无 | 剥离容器，逐字节还原码流；可顺带导出封面 |
| **QMC 解密** | `.mflac` `.mgg` `.qmc0` `.qmc3` `.qmcflac`… | 原始 `flac` / `ogg` / `mp3` / `m4a` | 无 | 流加密，无头无长度；解完嗅探格式（见下） |
| **通用转码** | 任意 ffmpeg 可读音频 | `mp3` `flac` `wav` `m4a` `aac` `ogg` `opus` `alac` `aiff` | ffmpeg | 有损格式支持 low/medium/high 三档或自定义码率 |

通道可以串联：`audioforge ncm song.ncm --to wav`、`audioforge qmc song.mflac --to mp3`
一步完成「解密 + 转码」。

**QMC 与 NCM 的结构差别**：NCM 是容器（有魔数、能按块切开），QMC 是**整文件流加密**，
没有头也没有长度字段 —— 所以它没有「读头就拒绝」这条路，只能先解 4 KiB 再嗅探。
`.mflac/.mgg` 的密钥多半内嵌在文件尾部；尾部是 `STag` / `musicex` 的那批**离线解不了**
（密钥在服务器上），得用 `--ekey` 手工给。详见 [docs/FORMAT.md](docs/FORMAT.md#qmc-qq-音乐)。

---

## 快速开始

### 方式一：直接用源码（不需要安装）

```bash
git clone https://github.com/Garvin666/audio-forge.git
cd audio-forge
python -m audioforge serve          # 需要先设置 PYTHONPATH=src，或用下面的方式二
```

### 方式二：装成本地包（推荐）

```bash
git clone https://github.com/Garvin666/audio-forge.git
cd audio-forge
pip install .                       # 零第三方依赖，只装本项目
audioforge serve
```

打开 <http://127.0.0.1:8000> 即可看到上传页。

### 方式三：Docker（镜像里已带 ffmpeg）

```bash
docker build -t audio-forge .
docker run --rm -p 127.0.0.1:8000:8000 -v "$PWD/data:/data" audio-forge
```

或 `docker compose up -d`（`docker-compose.yml` 默认只绑回环地址）。

镜像基于 `python:3.12-alpine` + **静态 ffmpeg 单文件**，`docker images` 报 **202 MB**
（debian 方案要 832 MB：`apt install ffmpeg` 会连带拉进 449 MB 的 libav* 依赖树；
alpine 的 `apk add ffmpeg` 也要 279 MB —— `x265`/`aom`/`vpx` 全是 `libavcodec`
的硬依赖，删不掉）。构建时可按需换源：

```bash
docker build --build-arg APK_MIRROR=mirrors.aliyun.com \
             --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
             -t audio-forge .
```

镜像里的 ffmpeg 是 **7.0.2-static**（johnvansickle 的 amd64 静态构建，SHA256 钉在
`Dockerfile` 里）。代价是没有 `ffprobe`、版本比 apk 的 8.x 旧一代；要新的就在自己
的 Dockerfile 里 `FROM audio-forge` 再 `apk add --no-cache ffmpeg`（`/usr/local/bin`
优先于 `/usr/bin`，加完即生效，体积 +189 MB）。

镜像已实测跑通：`.ncm` 解密（含 `--tags` 写 ID3）与 mp3→flac 转码都在容器里验过，
输出与真机逐字节一致；`/data` 卷重启不丢，`--read-only --cap-drop ALL
--security-opt no-new-privileges` 下上传/解密/下载/Range 全通。
构建若卡在下载，见 [docs/DEPLOY.md §2.2](docs/DEPLOY.md)。

---

## Web 平台怎么用

1. **拖文件进去**，或点击选择。支持一次多个文件，`.ncm` 与常见音频可以混着传。
2. **选目标格式**：
   - `自动` —— `.ncm` 解出它本来就有的格式（mp3 还是 mp3、flac 还是 flac），其余文件必须指定格式；
   - 选具体格式 —— 所有文件统一转成该格式，`.ncm` 会自动走「先解密再转码」。
3. **选质量**：高 / 中 / 低。有损格式对应 320k / 192k / 128k（opus 更低），无损格式忽略此项。
4. 点 **开始转换**。上传有进度条，转换期间按钮变灰。
5. 结果列表出现后点 **下载**。链接在服务器保留 60 分钟（`--ttl` 可调），过后自动清理。

页面右上角的徽章会显示后端能力：拿到 ffmpeg 就是「就绪 · 9 种目标格式」，
拿不到就变成「仅 NCM 通道」，此时通用格式下拉被禁用，但 `.ncm` 照样能解。

---

## 鉴权与配额

对外提供服务时必须打开鉴权。**鉴权在应用里做，不依赖反代** —— 因为只有应用知道
「一次请求几个文件、多少字节」，配额才有意义。

### 三种模式

| 模式 | 启动参数 | 适合 | 浏览器体验 |
| --- | --- | --- | --- |
| 关（默认） | 无 | 只在本机 | 直接可用 |
| Basic | `--auth 用户:口令` | 人用 | 原生弹框，输一次之后自动带凭据 |
| Bearer | `--token 令牌` | 脚本 / CI | 页面地址加 `?token=xxx` 打开一次，令牌存进 sessionStorage |

两者都配时「任一通过即可」。口令和令牌也可以走环境变量 `AUDIOFORGE_AUTH` /
`AUDIOFORGE_TOKEN`，避免出现在 `ps` 里。

```bash
audioforge serve --auth 'alice:强口令'
audioforge serve --token "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

边界：`/api/*` 全部需要凭证（含 `/api/health`、`/api/file/...`）；三个例外 ——
静态资源 `/`、`/app.js`、`/style.css` 公开（不含数据，且 Bearer 模式要靠页面先加载
才能取令牌），`/api/ping` 与 `/api/ready` 公开（前者只回 `{"ok": true}`，后者多一个
「工作目录还在不在」的判断；给容器探针、监控和负载均衡用）。
下载走 `<a href>` 带不了自定义头，所以额外接受 `?token=`，日志里会被打码成 `token=***`。

### 配额默认值

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--max-mb` | 200 | 单次请求体积，超出 413 |
| `--rate` | 120 | 每 IP 每分钟请求数，超出 429 + `Retry-After` |
| `--daily-files` | 2000 | 每 IP 每天成功转换的文件数 |
| `--daily-gb` | 20 | 每 IP 每天上传总量 |
| `--disk-gb` | 20 | 结果目录上限，超出按 mtime 从旧到新清到 80% |
| `--max-out-mb` | 2048 | **单个产物**体积上限（`ffmpeg -fs` + 落盘后复核），`0` 不限 |
| `--ttl` | 3600 | 结果保留秒数 |
| `--no-limits` | — | 一键关掉以上全部（`--max-mb` 除外） |

配额检查在读完 `Content-Length` 后立刻做，不会先收完 200 MB 再告诉你超了。
所有拒绝路径（401 / 403 / 413 / 429）都先处理请求体再回错误：body 不超过 32 MB 就
读干净丢弃，否则关闭连接 —— 不这样做，客户端还在发送时会被 RST 拿不到错误响应；
而甩着不读，残留的分段字节会被当成下一条请求行（日志里冒出 `400 Bad request syntax`，
也是请求走私的口子）。计数在进程内，重启清零，多实例不共享。

### 并发、超时与反向代理

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--concurrency` | 4 | 同时进行的**转换**数（信号量，排队而不是拒绝） |
| `--download-slots` | 0（自动） | 同时进行的**下载**数，0 = `max(8, concurrency×2)` |
| `--conn-timeout` | 60 | 单次 socket 读超时秒数；慢连接（slowloris）到点即断 |
| `--trust-proxy` | 关 | 是否采信反代传来的 `X-Forwarded-For` / `X-Real-IP` |
| `--trusted-proxy` | `127.0.0.1,::1` | 允许传 XFF 的反代地址/CIDR，逗号分隔 |

**`--trust-proxy` 的两个条件缺一不可**：开关打开 **且** 直连方在 `--trusted-proxy`
白名单里。只在反代后面开，裸奔时开着等于让人自造 IP 绕过配额。

不开会怎样：经过 Nginx 后所有请求的来源都是反代地址，`--rate` / `--daily-*` 会退化成
「所有人共用一个桶」，一个人就能把别人挤掉；`/api/health` 里的「本人用量」也失去意义。

对应环境变量：`AUDIOFORGE_TRUST_PROXY=1`、`AUDIOFORGE_TRUSTED_PROXY=10.0.0.0/8`。

`/api/health` 会回显当前模式与本人用量：

```json
"auth": { "mode": "basic+bearer", "usage": { "files_today": 3, "mb_today": 12.5 }, "disk_mb": 48.2 }
```

---

## 命令行

```bash
# 解 .ncm（输出到源文件同目录）
audioforge ncm song.ncm

# 批量解整个目录（含子目录），同时导出内嵌封面
audioforge ncm ~/Music/ncm/ -r --cover -o ~/Music/out

# 解完直接转成 wav
audioforge ncm song.ncm --to wav

# 解出来顺便把歌名/歌手/专辑/封面写成 ID3v2.3 标签（mp3 专有，码流不动）
audioforge ncm song.ncm --tags

# 解 QQ 音乐（.mflac / .mgg / .qmc0 / .qmcflac …），批量同理
audioforge qmc song.mflac
audioforge qmc ~/Music/QQmusic/ -r -o ~/Music/out

# 尾部没有内嵌密钥的文件（STag / musicex）要手工给 ekey
audioforge qmc song.mflac --ekey 'UVFNdXNpYyBFbmNWMixLZXk6...'

# 通用互转：flac -> mp3 320k
audioforge convert album/*.flac -f mp3 --quality high -o out/

# 自定义码率
audioforge convert a.wav -f opus --bitrate 96k

# 只看头部信息，不解音频（NCM 看容器，QMC 看尾部）
audioforge probe song.ncm --json
audioforge probe song.mflac --json

# 列出支持的格式与编码参数
audioforge formats

# 起 Web 平台（改端口 / 改上限 / 只写日志外）
audioforge serve --port 9000 --max-mb 500 --ttl 7200

# 公网部署：开鉴权 + 只信指定反代的 X-Forwarded-For + 给产物设体积上限
audioforge serve --host 0.0.0.0 --auth 'alice:强口令' \
                 --trust-proxy --trusted-proxy 172.18.0.0/16 --max-out-mb 512
```

`serve` 的其余常用项：`--concurrency 4`（转码并发）、`--download-slots 8`（下载并发，
默认按并发数自动、至少 8）、`--conn-timeout 60`（慢连接多久断开）、`--max-out-mb 2048`
（单产物上限，0 表示不限）、`--trust-proxy` / `--trusted-proxy`（反代后取真实 IP，
两个同时成立才生效）。全部参数见 `audioforge serve -h`。

`--ffmpeg /path/to/ffmpeg` 可显式指定 ffmpeg；也支持环境变量
`AUDIOFORGE_FFMPEG`、`FFMPEG_BIN`、`FFMPEG_PATH`。

**退出码**：`0` 全部成功，`1` 有文件失败，`2` 没有找到输入文件。

---

## HTTP API

Web 页面就是这套接口的客户端，可以单独调用。

### `GET /api/ping`

探活口，恒回 `{"ok": true}`。不校验凭证、不计限速 —— 容器 healthcheck 和监控
探针走这里，既不用把带用量的 `/api/health` 公开，也不会每 30 秒吃掉一次额度。

### `GET /api/ready`

就绪口，同样不校验凭证。比 `/api/ping` 多做一件极便宜的事：确认工作目录还在
（一次 `is_dir()`，<0.1 ms）。卷没挂上、目录被清掉时回 `503`：

```json
{"ok": false, "ready": false, "error": "工作目录不可用：/data/audioforge/jobs"}
```

负载均衡的摘流探针用这个；容器 `healthcheck` 保持用 `/api/ping`（它是「进程还活着」，
不该因为一支卷抖一下就重启容器）。

### `GET /api/health`

```json
{
  "ok": true,
  "version": "1.1.0",
  "ffmpeg": "ffmpeg version 7.1-essentials_build ...",
  "capabilities": { "ncm": true, "qmc": true, "transcode": true },
  "encrypted_ext": [".bkcape", ".bkcflac", "…", ".ncm", ".qmc0", ".qmc3", ".qmcflac"],
  "formats": ["aac", "aiff", "alac", "flac", "m4a", "mp3", "ogg", "opus", "wav"],
  "limits": {
    "max_upload_mb": 200,
    "max_output_mb": 2048,
    "job_ttl_minutes": 60,
    "quota": { "per_minute": 120, "files_per_day": 2000, "mb_per_day": 20480, "disk_mb": 20480 }
  },
  "server": {
    "conn_timeout_s": 60,
    "concurrency": 4,
    "download_slots": 8,
    "trust_proxy": false
  },
  "auth": {
    "mode": "none",
    "usage": { "files_today": 0, "mb_today": 0.0, "requests_this_minute": 1 },
    "disk_mb": 0.0
  }
}
```

`ffmpeg` 为 `null` 表示只支持解密通道（NCM + QMC）。启用鉴权后这个接口本身也要凭证。

`encrypted_ext` 把「哪些后缀算加密容器」交给后端下发：前端不重抄这份名单，
以后加格式只改一处（`qmc.EXTENSIONS`）。

这个接口**故意比前两个重**（要序列化限额与用量），别拿它当探针 —— 它曾经每次
请求都扫一遍 PATH、起一次 `ffmpeg -version` 子进程、`rglob` 整个结果目录，
601 个 job 时单次要 679 ms；现在 ffmpeg 探测与服务启动时做一次、磁盘用量走增量计数。

### `GET /api/stats`

给运维看的一组**纯内存计数**：进程活了多久、接了几个 IP、job 表里各状态多少条、
磁盘用了多少。不遍历目录、不起子进程 —— 否则就成了 `/api/health` 那个毛病换了个入口。

```json
{
  "ok": true,
  "version": "1.1.0",
  "uptime_s": 3612.4,
  "python": "3.12.7",
  "platform": "Linux-6.6.32-x86_64-with",
  "jobs": { "tracked": 12, "active": 1, "done": 10, "error": 1 },
  "ips": 3,
  "disk": { "jobs_mb": 41.2, "limit_mb": 20480 },
  "limits": { "max_upload_mb": 200, "max_output_mb": 2048, "concurrency": 4, "conn_timeout_s": 60 },
  "ffmpeg": { "found": true, "version": "ffmpeg version 7.1 ..." },
  "auth": { "mode": "bearer" }
}
```

它和 `/api/health` 的区别是「视角」：`health` 回答**这个实例能干什么**（能力、限额、
本人用量，客户端要先读它再决定能不能传），`stats` 回答**这个实例现在怎么样**
（负载、积压、异常数，给监控和 `docker stats` 旁边那一眼用）。
和其余 `/api/*` 一样需要凭证。

### `POST /api/convert`

`multipart/form-data`，字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `files` | 是 | 文件，可重复多次；`.ncm` 与普通音频可混传 |
| `target` | 是 | 目标格式，或 `auto`（仅对 `.ncm` 有效） |
| `quality` | 否 | `low` / `medium` / `high`，默认 `high` |
| `bitrate` | 否 | 覆盖码率，如 `320k`（语法校验：`^\d{2,4}[kK]?$`） |
| `job` | 否 | 客户端自带的 job 标识（8–64 位十六进制），用于转换**开始前**就能轮询进度 |
| `tags` | 否 | 写 ID3 标签：`1` / `on` / `true` / `yes` 为真，缺省不写（见下） |

`tags` 只影响 **NCM 解密出 mp3** 这一条路径：把歌名/歌手/专辑（有封面就一起）拼成
一个 ID3v2.3 头**前置**到码流前面，码流本身逐字节不变 —— 所以「默认不写」是刻意的，
`--tags` 是一个显式选择，而不是默认行为。flac 不写（该用 Vorbis comment），
文件自带 ID3 头时也不重复写。

```bash
curl -F "files=@song.ncm" -F "target=wav" -F "quality=medium" \
     http://127.0.0.1:8000/api/convert
```

响应（单文件失败不会让整个请求失败，逐条给状态）：

```json
{
  "ok": true,
  "job": "60bcaaffd5804b22980272910cc7542c",
  "status": "/api/job/60bcaaffd5804b22980272910cc7542c",
  "results": [
    {
      "ok": true,
      "source": "song.ncm",
      "name": "song.wav",
      "size": 47222916,
      "note": "NCM 解密（mp3，码率 320000） → wav",
      "url": "/api/file/60bcaaffd5804b22980272910cc7542c/song.wav",
      "meta": {
        "title": "我知道", "artists": ["By2"], "album": "Twins",
        "bitrate": 320000, "duration_s": 245.95, "has_cover": true,
        "size_consistent": true
      }
    }
  ]
}
```

两个可选字段值得注意：

- `status`：轮询地址。转换期间的进度从这里拿（见下一节）。
- `warning`：结果行里偶尔出现，例如体积对不上时
  `"解出 123456 字节，按元数据（时长×码率）推算约 987654 字节，文件可能被截断"`。
  **只提示不阻断** —— 这是启发式推断（VBR、元数据不准的文件本来就会偏），
  产物照常给你下载。

### `GET /api/job/<job>`

转换进度。前端在上传阶段就 `job=<32位hex>`，所以「上传完 → 转换中 → 完成」全程
都能查到，不必等响应回来。

```json
{
  "ok": true,
  "job": "60bcaaffd5804b22980272910cc7542c",
  "phase": "converting",
  "total": 3, "done": 1, "failed": 0,
  "items": [{"source": "a.ncm", "ok": true, "name": "a.mp3", "error": null}],
  "finished": false,
  "error": null,
  "elapsed": 12.34
}
```

`phase` 取值：`parsing` / `converting` / `done` / `error`。未知或已过期的 job 回 `404`，
格式不对（不是 8–64 位十六进制）回 `400`。只存在内存里 —— 服务重启即失，条目按
TTL 与数量上限回收。

### `GET /api/file/<job>/<文件名>`

下载结果，带 `Content-Disposition: attachment`（中文名用 RFC 5987 编码）。

**流式发送**（64 KiB 分块），支持单段 `Range`：

```bash
curl -r 0-1023 -o head.bin "http://127.0.0.1:8000/api/file/<job>/song.wav?token=..."
```

| 情况 | 响应 |
| --- | --- |
| 无 `Range` | `200` + `Content-Length` + `Accept-Ranges: bytes` |
| `bytes=0-1023` | `206` + `Content-Range: bytes 0-1023/<size>` + `Content-Length: 1024` |
| `bytes=1024-` | `206`，从 1024 到结尾 |
| `bytes=-1024` | `206`，最后 1024 字节 |
| 起点 ≥ 文件大小 | `416` + `Content-Range: bytes */<size>` |
| 多段 `bytes=0-1,5-6` | `200` 整包（RFC 允许忽略） |
| `HEAD` | `200` + 正确的 `Content-Length`，**不读盘、不发 body** |
文件名做了白名单校验，越界路径直接 404。

### 错误码

| 状态 | 场景 |
| --- | --- |
| 400 | 缺 `Content-Length`、boundary 不合法、没有文件、字段超长、job 标识格式不对 |
| 401 | 缺凭证或凭证不对（启用 `--auth` / `--token` 后，`/api/*` 全需要；响应带 `WWW-Authenticate`） |
| 404 | 未知路由、job 不存在或已过期、路径越界 |
| 413 | 请求体超过 `--max-mb`（不读 body，直接断开） |
| 416 | `Range` 起点超出文件大小，响应带 `Content-Range: bytes */<size>` |
| 429 | 触发限流或日配额，带 `Retry-After`；`error` 字段写明是哪一项 |
| 500 | 服务端异常：**只回 `{"ok": false, "error": "服务器内部错误", "id": "<8位>"}`** + `X-Request-Id`，异常类型/路径/堆栈只进日志（避免把绝对路径和用户名回给客户端） |
| 503 | `/api/ready` 发现工作目录不可用（卷没挂上 / 目录被删） |

---

## 项目结构

```
audio-forge/
├── src/audioforge/
│   ├── __init__.py          版本号（唯一来源）与包说明
│   ├── __main__.py          python -m audioforge 入口
│   ├── py.typed             PEP 561 标记：本包自带类型注解
│   ├── aes.py               纯 Python AES-128-ECB 加解密 + PKCS#7
│   ├── ncm.py               NCM 容器解析、keystream 生成、流式解密（.part 原子写）
│   ├── qmc.py               QMC 解密：静态表 / Map / RC4 + EKey 派生 + 四种尾部（.part 原子写）
│   ├── id3.py               ID3v2.3 标签构造（`--tags`，只前置、不动码流）
│   ├── transcode.py         ffmpeg 探测与转码参数翻译（含 -fs 体积封顶）
│   ├── multipart.py         零依赖 multipart/form-data 解析（mmap 切片）
│   ├── guard.py             鉴权（Basic / Bearer）+ 限速 + 日配额 + 磁盘闸门
│   ├── jobs.py              进程内 job 进度表（TTL + 容量上限）
│   ├── httpd/               HTTP 层（原来的 server.py 拆成了这一包）
│   │   ├── config.py        ServerConfig（不含副作用，目录由 ensure_dirs 建）
│   │   ├── base.py          BaseHandler：路由、响应助手、闸门、日志脱敏
│   │   ├── convert.py       POST /api/convert
│   │   ├── download.py      GET/HEAD /api/file/…（流式 + Range）
│   │   ├── health.py        /api/ping、/api/ready、/api/health、/api/stats、/api/job
│   │   └── app.py           AppServer 装配、启动横幅、过期清理线程
│   ├── server.py            兼容门面：旧导入路径仍然可用
│   ├── cli.py               子命令：ncm / convert / probe / formats / serve
│   └── web/                 前端（原生 HTML/CSS/JS，无构建、无 CDN）
│       ├── index.html
│       ├── app.js
│       └── style.css
├── tests/
│   ├── test_aes.py          FIPS-197 官方向量、补位边界
│   ├── test_ncm.py          现造 NCM fixture、跨分块边界、两种 key 布局、.part 原子写
│   ├── test_qmc.py          官方黄金向量（static/map/rc4/TC-TEA）、尾部四形态、边界 0x7FFF
│   ├── test_multipart.py    字段/文件/中文名/路径穿越/超大字段
│   ├── test_guard.py        Basic/Bearer/限速/配额/磁盘清理（注入假时钟）+ 桶回收
│   ├── test_jobs.py         进度表生命周期与淘汰策略
│   ├── test_transcode.py    wav→flac/mp3/opus/m4a（无 ffmpeg 时跳过）+ 参数拼装
│   ├── test_server.py       端到端 HTTP：上传、下载、鉴权、限流、404/413/429
│   ├── test_httpd.py        Range/HEAD、ETag、日志脱敏、批量配额、job 轮询、就绪口
│   ├── test_cli.py          退出码、目录展开、probe/ncm 离线通道
│   └── test_deploy.py       systemd 单元与 CLI 参数表不许漂移
├── docs/
│   ├── FORMAT.md            NCM 容器格式逐字节说明 + QMC 三种密码与尾部布局 + 参考实现
│   └── DEPLOY.md            Docker / 反代 / 公网部署清单
├── deploy/
│   ├── README.md            对外部署：鉴权模式 / 配额 / systemd / Nginx / 自检清单
│   ├── nginx.conf           TLS 反代 + 连接级限流 + 超时 + 安全头
│   ├── audio-forge.service  systemd 单元（加固项全生效需 ≥ 242；verify 零输出需 ≥ 247）
│   ├── audio-forge.service.rhel7  老 systemd 降级版（< 242，如 CentOS 7 的 219）
│   └── env.example          口令与令牌模板（`/etc/audio-forge/env` 或 compose 的 `.env`）
├── .github/workflows/ci.yml Python 3.9–3.14 矩阵测试 + lint 作业（action 固定 SHA）
├── .github/dependabot.yml   pip / actions / docker 三条升级通道
├── .pre-commit-config.yaml  ruff + ruff-format + 本地 unittest
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml           元数据 + ruff / mypy / coverage 配置（版本动态读取）
├── requirements.txt         故意留空：运行时零依赖，文件里写了原因
├── SECURITY.md              漏洞怎么报 + 部署时必须知道的三件事
├── CHANGELOG.md
├── LICENSE
└── README.md
```

运行时会额外产生两类目录：`src/audioforge/web/` 是随包发布的静态资源；
转换结果放在系统临时目录的 `audioforge/jobs/<job>/`（Docker 里是 `/data`）。

---

## 依赖说明

### Python 侧

**零第三方依赖。** `pyproject.toml` 的 `dependencies = []`，`requirements.txt` 只有注释。

| 功能 | 实现方式 | 为什么不用第三方库 |
| --- | --- | --- |
| AES-128-ECB | `src/audioforge/aes.py`，S-box 由 GF(2⁸) 逆元 + 仿射变换生成 | 只解几百字节的小块，引入 pycryptodome 反而增加安装门槛 |
| PKCS#7 | 同上，另加宽松模式兼容非标准补位 | |
| keystream | `ncm.py`，RC4 KSA + 预展开周期表 | 这就是格式本身，没有现成库 |
| HTTP 服务 | 标准库 `http.server.ThreadingHTTPServer` | 一个本地工具不需要 Flask/FastAPI |
| multipart | `multipart.py`，`mmap` + `find` 切片 | Python 3.13 已移除 `cgi`，不能依赖它 |
| 前端 | 原生 HTML/CSS/JS | 不引 CDN、不需要 node 构建 |

最低 Python 版本 **3.9**（CI 覆盖 3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14）。

### 外部程序

| 程序 | 是否必需 | 用途 | 装法 |
| --- | --- | --- | --- |
| `ffmpeg` | 仅通用转码需要 | 解码/编码各类音频 | Windows `winget install Gyan.FFmpeg`；macOS `brew install ffmpeg`；Debian `apt install ffmpeg`；Docker 镜像已内置 |

探测顺序：环境变量（`AUDIOFORGE_FFMPEG` → `FFMPEG_BIN` → `FFMPEG_PATH`）→ `PATH`
→ Windows 常见安装目录 → `imageio_ffmpeg` 自带的静态二进制。

**没有 ffmpeg 会怎样**：NCM 通道完全正常，`/api/health` 返回 `transcode: false`，
前端把格式下拉禁用，CLI 的 `convert` 子命令与 `--to` 参数报错提示装 ffmpeg。
不会出现「静默降级成半成品文件」。

---

## 支持的格式

| 目标 | 扩展名 | 编码器 | 类型 | low / medium / high |
| --- | --- | --- | --- | --- |
| `mp3` | `.mp3` | libmp3lame | 有损 | 128k / 192k / 320k |
| `m4a` | `.m4a` | aac | 有损 | 96k / 160k / 256k |
| `aac` | `.aac` | aac（ADTS） | 有损 | 96k / 160k / 256k |
| `ogg` | `.ogg` | libvorbis | 有损 | 112k / 160k / 224k |
| `opus` | `.opus` | libopus | 有损 | 64k / 96k / 160k |
| `flac` | `.flac` | flac | 无损 | — |
| `alac` | `.m4a` | alac | 无损 | — |
| `wav` | `.wav` | pcm_s16le | 无压缩 | — |
| `aiff` | `.aiff` | pcm_s16be | 无压缩 | — |

输入侧不限：任何 ffmpeg 能解的都行（`.ncm` / QMC 家族额外走解密通道）。转码时保留原文件的
元数据（`-map_metadata 0`），并丢弃视频流（`-vn`）。

解密通道认这些后缀：`.ncm`、`.mflac` `.mflac0` `.mgg` `.mgg0` `.mgg1` `.mggl` `.mmp4`、
`.qmc0` `.qmc2` `.qmc3` `.qmc4` `.qmc6` `.qmc8` `.qmcflac` `.qmcogg`、
`.bkcmp3` `.bkcm4a` `.bkcflac` `.bkcwav` `.bkcape` `.bkcogg` `.bkcwma`、`.tkm`。
（QMC 解出来的真实格式由码流嗅探决定，后缀只作为兜底提示。）

---

## 实现要点

### AES 是自己写的，而且对过官方测试向量

`aes.py` 里没有硬编码 S-box —— 用 GF(2⁸) 的 exp/log 表求逆元，再做仿射变换现场生成。
正确性由 FIPS-197 附录 B 与 C.1 的加解密向量兜住（`tests/test_aes.py`）。

这里有个真实的坑：`META_KEY` 的末尾是 `` `<` ``、`'`、`(`（hex `3C 27 28`），
很多现成脚本的注释把它写成 `` `<` ``、`(`、`'` —— 照抄注释，解出来的 JSON 就是一堆乱码。
**一切以 hex 为准。**

### 音频解密快了 53 倍

NCM 的音频段用 256 字节周期的 keystream 异或。逐字节 Python 循环解 9.4 MB 要 1.75 秒
（5.4 MB/s）；改成「预展开周期表 + 4 MiB 分块大整数异或」后是 0.033 秒（**287 MB/s**）。

分块大小必须是 256 的整数倍，否则第二块起相位错位、后半段全是噪音。代码里一旦出现
短读就直接抛错，不允许带着错的相位继续写文件。

### QMC 有三套密码，而且每套都有一个「只在特定密钥长度下才正确」的坑

QMC 不是容器，是整文件流加密，按密钥长度分流：

| 形态 | 触发条件 | 密码 | 结构 |
| --- | --- | --- | --- |
| v1 静态 | 尾部没有密钥 | 128 字节固定表异或 | `mask = box[(o*o+27) & 0xFF]`，`o` 超过 `0x7FFF` 才对 `0x7FFF` 取模 |
| v2 Map | master key ≤ 300 字节 | 由 key 派生的掩码表 | `idx = (o*o+71214) % len(key)`，`mask = rot(key[idx], idx&7)` |
| v2 RC4 | master key > 300 字节 | 变长 S 盒 RC4 | 首段 0x80 直接查 key；之后每 0x1400 一段，从 KSA 盒副本重启 PRGA 先丢 `skip` 个 |

三个真踩过的坑，都属于「拿真样本也测不出来」那一类：

1. **`0x7FFF` 这一个字节**：偏移 `0x7FFF` 用的是 `o = 0x7FFF` 本身，不是 `o = 0`。
   错这一字节的症状是整个 ogg 只有一页 CRC 不对，其余 2700+ 页全对，听感上根本发现不了。
2. **Map 不能先把 key 压成 128 字节**：压成 128 字节只在 `len(key)` 能整除 256 时等价
   （128/256 恰好能整除，96/200/300 不能）。真实样本多为 128/256，所以这个错会一路绿灯，
   直到遇见别的长度 —— 从偏移 128 起整首都是噪音。
3. **RC4 的 skip 是 `idx % len(key)`，不是 `idx & 0x1FF`**：两者在 `len(key)` 为 2 的幂
   （真实样本的 512）时完全等价，换 301/384/768 就全错。

所以 `tests/test_qmc.py` 里的黄金向量**故意挑不能整除 / 不是 2 的幂的密钥长度**
（map 用 96，rc4 用 384 与 301），再加上官方的 TC-TEA 测试向量。三条变异
（把 `% n` 换回 `& 0x1FF`、把 map 索引换成 `%128`、把边界换成 `0x8000`）都能让用例变红 ——
否则这些断言只是摆设。

### 上传不占内存，下载也是

请求体先按 64 KiB 分块落到临时文件，再用 `mmap` 定位 boundary，文件 part 按区间流式
拷贝到目标路径。200 MB 的上传，进程内存不会跟着涨到 200 MB。

下载同样走流式（64 KiB 分块写 `wfile`），`HEAD` 只发头不读盘。实测 96 MB 的文件：

| | 整包进内存（旧） | 流式（现在） |
| --- | --- | --- |
| 下载 RSS 增量 | +96.2 MB（1.00× 文件大小） | **+2.2 MB（0.02×）** |
| `HEAD` RSS 增量 | +99.6 MB | 同量级微小（只 `stat`，不读盘） |
| 200 MB 外推 | ~200 MB | ~4.6 MB |

顺带给了解析式进度以外的东西：单段 `Range` 支持断点续传，浏览器拖进度条也能用。

### 格式判定不看元数据、看码流

`meta` 里的 `format` 字段会缺、会错。真正决定输出扩展名的是**解密后**音频段的前 4 KB ——
`fLaC` 是 flac、`ID3`/`0xFFFB` 是 mp3。注意判断必须在解密之后做，因为 NCM 的音频段
连魔术字节一起被异或掉了。

---

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

**324 个用例**，全部用标准库 `unittest`，不需要安装 pytest：

- **AES**：FIPS-197 两组官方向量、加解密往返、S-box 单射性、PKCS#7 边界与宽松模式
- **QMC**：`static / map / rc4` 三套密码各有一组**官方黄金向量**（sha256 覆盖 64 KiB
  掩码流 + 边界点逐字节），密钥长度故意选 96 / 384 / 301 这类「不能整除、不是 2 的幂」
  的值；TC-TEA 用 `tc_tea` crate 的官方向量；尾部四种形态（QTag / STag / musicex / PC 长度+key）
  与长 key（>1280 字节）各有用例；另测 `--ekey` 手工密钥、错 key 必须报错、`.part` 清理
- **NCM**：测试用的 `.ncm` 不是样本文件，而是按 `docs/FORMAT.md` 现场造出来的 ——
  用 `AES128ECB.encrypt` 生成 key/meta 块，用同一张 keystream 表异或回去。
  覆盖 4 MiB 分块边界、非 256 对齐的尾块、新旧两种 key 布局、封面长度重复/单写、
  截断文件、非 NCM 输入；外加 `.part` 原子写（失败不留半成品）与体积对照告警
- **ID3**：不带第三方库、自带二十行的最小解析器回读自己写的标签 —— 版本号与
  synchsafe 长度、**帧长度是普通大端而非 synchsafe**（用 200 字节载荷钉住）、
  中文 UTF-16LE、多艺人的 `/` 连接、`APIC` 逐字节布局、超大封面跳过、
  已被标记/非 mp3 时不写
- **multipart**：中文字段、128 KiB 二进制保真、`../../evil.mp3` 清洗、重复文件名、超大字段；
  **缺收尾 `--boundary--` 一律按截断拒绝**（半份内容不落盘）、Windows 保留名
  （`CON`/`nul`/`COM1`）与尾点尾空格清洗
- **鉴权与配额**：Basic / Bearer / 二者组合、畸形 base64、无冒号、口令含冒号；
  限速窗口滚动与跨日重置（假时钟）；日文件数、日字节数、磁盘清理按 mtime 从旧到新；
  **批量上传不能绕过日配额**、XFF 信任模型的五种组合、记账桶的过期回收与硬上限
- **HTTP**：随机端口起真实服务，跑通上传→转换→下载全链路；
  另测 401（含 `WWW-Authenticate`）、429（含 `Retry-After`）、400/404/413/**416**、路径穿越，
  「静态资源公开、`/api/*` 需要凭证」这条边界，以及 `Range`/`206`/`HEAD`、`ETag`/`304`、
  `/api/job` 轮询、`/api/ready` 的 503、`/api/stats` 的字段与鉴权、**日志里不出现明文令牌**
- **CLI 与部署**：子命令退出码、目录展开、`probe --json`、`ncm` 落盘；
  systemd 单元里每个 flag 都必须被 argparse 接受（防「本地全绿、上线 unrecognized arguments」）

CI 在 Python 3.9–3.14 六个版本上跑同一套测试，另有一个 `lint` 作业跑 `ruff` 与 `mypy`。
ffmpeg 相关用例在检测不到 ffmpeg 时自动跳过。runner 若没预装 ffmpeg，CI 会装
`imageio-ffmpeg` 的静态二进制（不走 apt，避免多 job 抢锁）。

### 开发工具

```bash
pip install -e ".[dev]"     # ruff + mypy + coverage，都写在 pyproject.toml 里
ruff check .                # E,F,W,I,UP,B,SIM；line-length 100
mypy                        # check_untyped_defs，src 全量
coverage run --source=src/audioforge -m unittest discover -s tests -t .
coverage report -m          # 门限 80%（当前 86%）
pre-commit install          # 可选：提交前自动跑 ruff + unittest
```

---

## 部署

本机自用（推荐）：

```bash
pip install .
audioforge serve                     # 127.0.0.1:8000，只有本机能访问
```

Docker：见 `Dockerfile` / `docker-compose.yml`，镜像里装了 ffmpeg，结果挂载到 `/data`。

公网部署：**[deploy/README.md](deploy/README.md)** 给了 systemd 单元、Nginx 反代配置、
环境变量模板和一份「部署后自检」清单；**[docs/DEPLOY.md](docs/DEPLOY.md)** 是本机 / Docker /
反代的完整说明。systemd 单元要求 **≥ 242**（加固全生效）；`systemd-analyze verify` 想让输出为空
则需要 **≥ 247**（`ProtectProc=` 是 247 引入的，更老的版本只告警并忽略它）。
跨版本逐条实测见 `deploy/README.md` §3.4，
更老的系统用 `deploy/audio-forge.service.rhel7`。

对外之前必须做一件事 —— **打开鉴权**：

```bash
audioforge serve --host 0.0.0.0 --auth 'alice:强口令'      # 浏览器弹框
audioforge serve --host 0.0.0.0 --token "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

配额默认就开着（120 次/分、2000 文件/日、20 GB/日、磁盘 20 GB），见下一节。

版本与逐条变更见 **[CHANGELOG.md](CHANGELOG.md)**；发现漏洞怎么报、以及部署前必须知道的
三件事见 **[SECURITY.md](SECURITY.md)**。

---

## 安全边界与合规

**默认只监听 `127.0.0.1`，且默认不鉴权** —— 这个组合是安全的；一旦你改 `--host`，
就必须同时开 `--auth` 或 `--token`（CLI 会打印警告）。

| 层 | 挡什么 | 默认 |
| --- | --- | --- |
| 监听地址 | 非本机访问 | `127.0.0.1` |
| `--auth` / `--token` | 未授权访问 `/api/*` | 关（本机自用） |
| `--max-mb` | 单个超大请求 | 200 MB |
| `--rate` | 高频打点 | 120 次/分/IP |
| `--daily-files` / `--daily-gb` | 单 IP 长期占用 | 2000 文件 / 20 GB 每日 |
| `--disk-gb` | 磁盘被写满 | 20 GB，超了先清最旧结果 |
| `--max-out-mb` | 单个产物把盘写满（mp3→wav 会放大 2–4 倍） | 2048 MB，超了丢弃产物并报错 |
| `--conn-timeout` | 慢连接长期占线程 | 60 s |
| `--trust-proxy` | 反代之后配额退化成「全局共享」 | 关（开也只信白名单来源） |
| `--ttl` | 结果长期滞留 | 1 小时 |
| 文件名清洗 | 路径穿越、非法字符 | 强制，不可关 |

仍然需要注意：

- **静态资源不鉴权**（`/`、`/app.js`、`/style.css`）—— 它们不含数据，且 Bearer 模式要靠页面
  取令牌。真正的门在 `/api/*`。
- **Bearer 令牌会出现在 `?token=`**（为了 `<a href>` 下载能带凭证）。它可能落在浏览器历史、
  反代日志里。反代配置里已加 `Referrer-Policy: no-referrer` 防止外泄给第三方；介意的话就用
  Basic 认证。应用自己的访问日志会把 query 里的凭证打码成 `token=***`，
  所以 `journalctl -u audio-forge` 里是安全的。
- **`500` 不再回显异常文本**：只回一个 8 位 request id，细节进日志（旧版会把
  `OSError: [Errno 13] ... /home/xxx/...` 这类带绝对路径的信息回给客户端）。
- **配额是进程内计数，重启清零，多实例不共享**。要做多实例共享配额得换 Redis。
- 容器内必须 `--host 0.0.0.0` 才能被端口映射访问，这是唯一需要放开的场景。
- 上传文件名会剥掉目录部分、控制字符和 `<>:"/\|?*`，落盘路径再做一次 `resolve()` 校验。

**关于 NCM / QMC 与版权**：`.ncm`（网易云音乐）与 QMC（QQ 音乐）都是客户端加密格式。
解密能力只应作用于**你自己合法拥有、且平台条款允许离线使用**的音频。本项目：

- 只做容器剥离与格式转换，不联网、不上传任何文件到第三方、不包含任何内容分发功能；
- 仓库不附带任何音频样本，测试 fixture 全部现场生成（QMC 的黄金向量是掩码流的哈希，
  不是音频）；
- `STag` / `musicex` 尾部那批文件**不含密钥**，本项目不实现也不调用任何取密钥的接口，
  只接受使用者自己提供的 `--ekey`；
- 请勿用它批量下载、分发或二次销售受版权保护的音乐；由此产生的责任由使用者自负。

⚠️ **DMCA 与合规风险（务必先看）**：上游参考实现 `unlock-music/unlock-music` 已在
2022-11 因 QQ 音乐相关投诉被 GitHub **DMCA 下架**。规避技术措施在部分法域
（如美国 DMCA §1201、中国《著作权法》的技术措施条款）即使对自购内容也可能构成违法。
**把本仓库保持为私有、或只在你自己的机器上使用，是风险最低的做法**；
若要公开发布，请自行评估所在地法律与平台政策，必要时先移除 QMC 通道。

---

## 常见问题

**Q：`/api/health` 里 `transcode` 是 false？**
没找到 ffmpeg。装好后重启，或 `audioforge serve --ffmpeg /绝对路径/ffmpeg`。
Windows 上如果 ffmpeg 装在非 PATH 目录，用环境变量 `AUDIOFORGE_FFMPEG` 指过去最省事。

**Q：解出来的文件能播放但元数据（歌名/歌手）没了？**
NCM 把元数据放在单独的加密块里，默认只还原音频码流、不动字节。要写回去就加 `--tags`
（或网页上勾「写 ID3 标签」、API 里传 `tags=1`）：它会构造一个 ID3v2.3 头**前置**到
mp3 前面，歌名/歌手/专辑/封面都在里面，码流本身逐字节不变。
限制：只对 mp3 生效（flac 该用 Vorbis comment）；文件本身已带 ID3 头时不重复写；
封面超过 8 MiB 就跳过（只影响标签，不影响音频）。

**Q：转码后体积变大了？**
转成 `wav`/`aiff` 是未压缩的，体积必然膨胀（320k mp3 约 2–4 倍）。
要小就用 `flac`（无损压缩）或 `opus`（低码率最划算）。

**Q：几十个文件一起传会不会超时？**
服务端是线程池（默认 4 并发），大文件转码本身耗 CPU。Linux/macOS 上建议把 `--max-mb`
按需调大，并把 `--ttl` 留足下载时间。

**Q：为什么不用 Flask / FastAPI？**
这个工具的使用场景是「本机跑一下」，标准库足够，装上就能跑、不会因为依赖版本冲突坏掉。
真要做成多人服务，建议重写那一层，而不是把 `http.server` 硬撑大。

---

## License

MIT。NCM 相关常量与格式结论来自对公开样本的实测分析，仅供互操作与研究使用。
