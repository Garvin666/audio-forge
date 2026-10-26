# 部署指南

三种场景，按需要挑一个：本机自用、Docker、公网反代。三者的差别只在「网络可达性」和
「谁来兜鉴权」，代码本身没有环境分支。

---

## 0. 部署前先确认三件事

```bash
# 1) Python 版本 >= 3.9
python -V

# 2) ffmpeg 在不在（不在也能跑，只是少了通用转码）
ffmpeg -version

# 3) 端口 8000 没被占用
python -c "import socket;s=socket.socket();print(s.connect_ex(('127.0.0.1',8000)))"
```

输出 `0` 表示已被占用，换 `--port 9000` 之类的。

---

## 1. 本机自用（最简）

```bash
git clone https://github.com/Garvin666/audio-forge.git
cd audio-forge
pip install .
audioforge serve
```

默认 `127.0.0.1:8000`。只有本机能访问，不需要任何鉴权。

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 改成 `0.0.0.0` 才对局域网开放（**必须同时开鉴权**，否则 CLI 会警告） |
| `--port` | `8000` | |
| `--auth` | 关 | `用户:口令`，启用 HTTP Basic（也可用 `AUDIOFORGE_AUTH`） |
| `--token` | 关 | 启用 Bearer 令牌（也可用 `AUDIOFORGE_TOKEN`），页面用 `?token=` 传入 |
| `--max-mb` | `200` | 单次请求上限 |
| `--rate` | `120` | 每 IP 每分钟请求数，0 不限 |
| `--daily-files` | `2000` | 每 IP 每日文件数，0 不限 |
| `--daily-gb` | `20` | 每 IP 每日上传量，0 不限 |
| `--disk-gb` | `20` | 结果目录上限，超出按 mtime 清旧，0 不限 |
| `--no-limits` | 关 | 一键关闭速率与配额（`--max-mb` 除外） |
| `--ttl` | `3600` | 结果保留秒数，过期整目录清理 |
| `--workdir` | 系统临时目录 | 结果落盘位置 |
| `--ffmpeg` | 自动探测 | 显式指定 ffmpeg |
| `-q` | 关 | 不打印访问日志 |

### 开机自启（Linux / systemd）

`/etc/systemd/system/audio-forge.service`：

```ini
[Unit]
Description=audio-forge local web
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/audio-forge
ExecStart=/usr/local/bin/audioforge serve --port 8000 --workdir /var/tmp/audio-forge
Restart=on-failure
NoNewPrivileges=yes
PrivateTmp=yes
# 这一行别省：systemd 下 stdout 是管道，Python 默认块缓冲，
# 结果 journalctl 里看不到任何应用输出（连启动横幅都没有）。
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now audio-forge
journalctl -u audio-forge -f
```

> 上面是最小示例。带独立用户、`ProtectSystem=strict`、日配额与磁盘闸门的**加固版**在
> [`deploy/audio-forge.service`](../deploy/audio-forge.service)，配套的安装、自检与实测数据见
> [`deploy/README.md`](../deploy/README.md) §3。
>
> **版本要求**：加固版需要 **systemd ≥ 242**（卡在 `RestrictSUIDSGID=`）；
> `ProtectProc=invisible` 要 247，更老的版本会忽略它并打印一条 `Unknown key name` 告警，
> 其余加固照常生效。判据用 `test -z "$(systemd-analyze verify …)"`，别看退出码。
> 更老的系统（CentOS 7 是 219）改用 [`deploy/audio-forge.service.rhel7`](../deploy/audio-forge.service.rhel7)，
> 逐条的版本对照与实测依据见 [`deploy/README.md`](../deploy/README.md) §3.4。

### 开机自启（Windows）

用「任务计划程序」建一个登录时触发的任务，操作填：

```
程序：C:\Python312\python.exe
参数：-m audioforge serve --port 8000
起始于：C:\path\to\audio-forge
```

或用 `nssm install audio-forge` 注册成服务。注意不要用 `.bat` 包一层 —— 批处理里
多余的 `pause` 会让非交互启动直接卡死。

---

## 2. Docker

```bash
docker build -t audio-forge .
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -v "$PWD/data:/data" \
  audio-forge
```

或直接：

```bash
docker compose up -d
docker compose logs -f
```

要点：

- **镜像里已经装了 ffmpeg**，`/api/health` 应该返回 `transcode: true`。
- 基础镜像是 `python:3.12-alpine`：本项目运行时零第三方依赖、全是纯 Python，镜像里唯一的
  原生二进制是 ffmpeg 本身，由 apk 提供并与 musl 配套编译，没有 ABI 风险。
  ⚠️ 副作用：`imageio-ffmpeg` 随包那份 glibc 静态二进制在 musl 下跑不起来 —— 本项目不装它，
  且 `find_ffmpeg()` 先查 PATH 再回落到它，所以无影响；自行加装时留意这一点。
- `docker-compose.yml` 的 healthcheck 打的是 `/api/ping`（无凭证、不计限速），
  所以开了 `AUDIOFORGE_AUTH` 也不会被判 unhealthy。鉴权变量从 `.env` 注入，
  见 `deploy/env.example`。
- 容器内必须 `--host 0.0.0.0`（Dockerfile 的 CMD 已经写好），否则宿主机端口映射打不进来。
- 端口映射建议写 `127.0.0.1:8000:8000`，只在宿主机回环上暴露；要对外由反代负责。
- 结果目录挂到 `/data`，容器重建不丢正在下载的文件。
- 磁盘要留够：转成 wav 时体积约为源文件的 3–4 倍，TTL 内的结果都会占着盘。

### 2.1 这套 Docker 配置的实测记录

不是「照文档写一遍」，是真的构建、起容器、跑真文件验过的：

| 项 | 实测值（静态 ffmpeg 版） | 上一版（apk ffmpeg） | 旧 debian 版 |
| --- | --- | --- | --- |
| 镜像大小 | **202 MB** | 279 MB | 832 MB |
| 构建（冷构建，含取 ffmpeg） | 2 分 09 秒（其中下载 41.9 MB 静态包 ≈ 29 秒） | 2 分 55 秒（ffmpeg 层 157 秒） | 3 分 49 秒 |
| 构建（改代码重建，走缓存） | 12 秒 | 12 秒 | 6.2 秒 |
| 容器常驻内存 | 17 MB | 17 MB | 17 MB |
| 镜像内 ffmpeg | 7.0.2-static（含 libmp3lame / flac / aac / libopus，**无 ffprobe**） | 8.1.3 | 5.1.x |
| 真文件解密 | 合成 200 349 B `.ncm` → 200 000 B mp3，与真机直解**逐字节一致**（`cmp` 无差异）；`--tags` 后 200 165 B，标签之后仍是同一段码流 | 同 | 10 MB `.ncm` → 9 841 292 B，同样一致 |
| 通用转码 | mp3（33 062 B）→ flac（110 811 B），`ffprobe` 验为 44.1 kHz 单声道；容器内 `libmp3lame -q:a 5` 造的 2 秒正弦 → flac 93 KB | 同 | mp3 → flac，1.38 秒，48 kHz 双声道 |
| 加固后能否工作 | `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges` 下 ping/ready/health/stats、上传、解密（含 ID3）、下载、Range、转码全通；`touch /` 回 `Read-only file system`；`docker cp` 会被拒（rootfs 只读），得用 `docker exec -i sh -c 'cat > /tmp/x' < file` | 同 | 同 |
| Range | `bytes=0-99` → 206 + `Content-Length: 100`；`HEAD` → 200 + 全长；越界 → 416 + `Content-Range: bytes */110811` | 同 | 同 |

**202 MB 是怎么来的，以及为什么不再往下压**：

1. `python:3.12-alpine` 基础镜像 **74.7 MB**（官方 CPython，换 `alpine:3.22` + `apk add python3`
   能省 ~30 MB，但要自己跟 Python 版本、冷构建多 15 分钟，不划算）；
2. 静态 `ffmpeg` 单文件 **79.8 MB**（`COPY --from` 从构建阶段搬进来，不带 ffprobe）；
3. `pip install .` **12.1 MB**；
4. 源码与元数据 < 1 MB。

之前停在 279 MB，是因为 alpine 的 `ffmpeg-libs` 是个 **189 MB 的单体**：`libx265` 21 MB、
`libavcodec` 20 MB、`libaom`/`libSvtAv1Enc`/`libjxl` 各占几 MB，全是 `ffmpeg` 的
`DT_NEEDED` 硬依赖 —— `apk del` 无从下手，删任何一个 `ffmpeg` 都直接起不来。
静态版把这一整棵树换成一个自包含的可执行文件（连 libc 都不需要，musl 下照样跑），
省掉 109 MB。**代价是 ffmpeg 从 8.1 退到 7.0.2、且没有 ffprobe** —— 本项目只用
`-c:a` / `-q:a` / `-fs` 这些十几年没变过的参数，换来的 109 MB 更值。

再往下只剩两个小口子，都没做：删 `site-packages/pip`（-12 MB，但容器里就再也不能
`pip install`）、删 stdlib 的 `idlelib`/`tkinter`/`turtledemo`（-7 MB，纯属自找麻烦）。
收益远小于「以后有人 `docker exec` 进去发现缺东西」的成本。

⚠️ 另注：`docker images` 的 Size 与容器内 `du -sm /` 差 30% 以上（同一镜像 202 MB vs 141 MB），
对账只认 `docker image inspect -f '{{.Size}}'`，别拿 `du` 反驳。

### 2.2 构建卡在下载时怎么处理

冷构建要下载两样东西：`apk add curl xz`（几 MB）和 **41.9 MB 的静态 ffmpeg 包**。
`Dockerfile` 里已经在 curl 上做了 5 次重试（`--retry 5 --retry-delay 2`），
并且**校验 SHA256**：下载到的字节与钉在 `Dockerfile` 里的哈希不符就整层失败，
不会悄悄换一个二进制进镜像。

如果反复失败（典型表现：卡在 `curl ... ffmpeg-release-amd64-static.tar.xz` 几分钟不动）：

```bash
# 换国内源（Dockerfile 已内置这两个参数，改源比反复重试有效）
docker build --build-arg APK_MIRROR=mirrors.aliyun.com \
             --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
             -t audio-forge .

# 别把本机代理硬塞进构建：容器到宿主的代理链路常常是 502 的来源
# 实测「不带任何 --build-arg HTTP_PROXY」的直连比走代理稳
```

实测速度（本机、容器内）：`dl-cdn.alpinelinux.org` 约 60 KB/s，`mirrors.aliyun.com`
不到 1 秒下完同一个索引文件；`pypi.org` 在容器里直接 `Connection refused`，
`pypi.tuna.tsinghua.edu.cn` 正常。也就是说国内网络下这两个 build-arg 基本是必需的 ——
不加会卡在下载，不是失败，是慢到看起来像卡死。

**换 ffmpeg 源/升版本**（只有一个地方要改，两行必须同步）：

```bash
# 1) 本地先下下来，算哈希
curl -fsSL -o ff.tar.xz <新的 tar.xz 地址> && sha256sum ff.tar.xz
# 2) 把地址与哈希填进 docker build（不填哈希就用不了，这是故意的）
docker build --build-arg FFMPEG_URL=<新的 tar.xz 地址> \
             --build-arg FFMPEG_SHA256=<上一步的 sha256> -t audio-forge .
```

默认只认 **全静态**构建（`ffmpeg-*-{amd64,arm64}-static.tar.xz` 这一类）：动态链接的
glibc 版在 alpine 里加载不了。`Dockerfile` 里已经按 `TARGETARCH` 内置了 **amd64 与
arm64** 两个包 + 各自的 SHA256（arm64 包 19.3 MB / 解出 51 MB，amd64 是 41.9 MB / 79.8 MB），
`--platform linux/arm64` 会自动选中；其它架构（armhf、ppc64le…）请在命令行显式
`--build-arg FFMPEG_URL=... --build-arg FFMPEG_SHA256=...`，否则构建直接失败并给出提示。
本项目不调用 `ffprobe`，所以包里那个同体积的 `ffprobe` 直接不搬进镜像。

> 踩过的坑：`--build-arg HTTP_PROXY=http://127.0.0.1:xxxx` 里的 `127.0.0.1` 指的是**容器自己**，
> 不是宿主机，会得到 `Connection refused`；要指向宿主得用 `http://http.docker.internal:3128`
> （仅 Docker Desktop）。而这条链路在批量下载时容易回 502 —— 直连反而更稳。

> 另一个坑：Docker Hub 直连在本机是超时的（`dial tcp …:443` 无响应）。拉基础镜像走
> 镜像站即可：`docker pull docker.m.daocloud.io/library/python:3.12-alpine`，再
> `docker tag` 成本地 `python:3.12-alpine`，`Dockerfile` 不用改。

---

## 3. 公网

**顺序很重要**：先打开应用层鉴权，再上反代。反代只能挡「没登录的人」，挡不住
「登录了的人连传 500 个 200 MB 把磁盘写满」—— 后者只有应用自己知道一次请求几个文件。

```bash
# 第一步：开鉴权（口令走环境变量，别进命令行）
export AUDIOFORGE_AUTH='alice:强口令'
audioforge serve --host 127.0.0.1 --port 8000 --workdir /var/lib/audio-forge

# 或者用令牌（适合脚本调用）
export AUDIOFORGE_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
audioforge serve --host 127.0.0.1 --port 8000
```

配额默认就开着，一般不用动：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--max-mb` | 200 | 单次请求体积 |
| `--rate` | 120 | 每 IP 每分钟请求数 |
| `--daily-files` | 2000 | 每 IP 每日文件数 |
| `--daily-gb` | 20 | 每 IP 每日上传量 |
| `--disk-gb` | 20 | 结果目录上限，超了按 mtime 清旧 |

第二步是 TLS 反代。完整可用的配置、systemd 单元、环境变量模板和部署后自检清单在
**[../deploy/README.md](../deploy/README.md)**，这里不重复贴：

| 文件 | 用途 |
| --- | --- |
| `deploy/nginx.conf` | TLS + `limit_req`（30 r/s）+ `client_max_body_size` + 900s 超时 + 安全头 |
| `deploy/audio-forge.service` | systemd 单元（≥ 242，零警告需 247），含 `ProtectSystem=strict`、`SystemCallFilter=@system-service`、`CapabilityBoundingSet=` 等加固 |
| `deploy/audio-forge.service.rhel7` | 老 systemd 降级版（< 242，如 CentOS 7 的 219） |
| `deploy/env.example` | 口令/令牌模板，放 `/etc/audio-forge/env` 并 `chmod 600` |
| `deploy/README.md` | 三种鉴权模式、配额取舍、自检 curl 清单、常见问题 |

三处必须对齐，否则会出现「上传到 100% 后报错」：

| 位置 | 项 | 取值 |
| --- | --- | --- |
| Nginx | `client_max_body_size` | ≥ 应用 `--max-mb`，留 10 MB 余量 |
| Nginx | `proxy_read_timeout` | ≥ 900s（大文件转码慢） |
| 应用 | `--max-mb` | ≤ 上面那个值 |

鉴权之后仍要盯的几件事：

| 项 | 建议 |
| --- | --- |
| 磁盘 | `/var/lib/audio-forge` 单独挂盘 + 监控；`--disk-gb` 是最后一道闸，不是唯一一道 |
| 日志 | 用 `--quiet` 关应用访问日志（反代已有）；异常栈仍会进 journald |
| 出网 | 进程不需要任何出网权限，防火墙可以直接禁掉 |
| 内容合规 | 不要做成公开的「音乐下载站」，见 README 的合规段落 |
| 真实 IP | 放在反代后面**必须**加 `--trust-proxy`，否则所有请求的来源都是反代地址，`--rate` / `--daily-*` 退化成「所有人共用一个桶」。只接受来自 `--trusted-proxy`（默认 `127.0.0.1,::1`，可按 CIDR 写）的 `X-Forwarded-For` / `X-Real-IP`；裸奔时别开，否则等于让人自造 IP |

## 4. 故障排查

| 现象 | 排查 |
| --- | --- |
| 打开页面空白 | 看进程是否在跑；`curl -i http://127.0.0.1:8000/api/ping` 有没有 `{"ok": true}` |
| 页面能开但按钮一直灰 | `/api/health` 失败 → 前端拿不到能力列表；看浏览器 console |
| 上传到 100% 后报 413 | 请求超过 `--max-mb`，同时检查反代的 `client_max_body_size` |
| `/api/health` 返回 401 | 已启用鉴权。带 `-H "Authorization: Bearer <令牌>"`，或 `-u 用户:口令` |
| 容器一直 unhealthy | healthcheck 应打 `/api/ping`（不必带凭证）；若改成打 `/api/health`，配了鉴权就会 401。想让「卷掉了」也算不健康，改用 `/api/ready`（工作目录不可用时回 503） |
| 返回 429 且 `error` 提「请求过于频繁」 | 撞了 `--rate`；响应头有 `Retry-After` |
| 返回 429 且 `error` 提「今日配额已用尽」 | 撞了 `--daily-files` / `--daily-gb`，按需调大 |
| 返回 429 但 body 是 **HTML**、也没有 `Retry-After` | 撞的是 Nginx 的 `limit_req` / `limit_conn`，不是应用配额。调 `nginx.conf` 的 `rate` / `burst` / `limit_conn`。想分清就看 `Server:` 头，应用不返回它 |
| 磁盘满但 `--disk-gb` 设了 | 磁盘闸门只在**新请求进来时**触发；也可能超限前就已经写满，需外部监控兜底 |
| 提示「非 NCM 文件必须指定目标格式」 | 目标格式选了「自动」但有非 `.ncm` 文件，选具体格式 |
| 下载 404 | 超过 `--ttl` 被清理，或 job 目录被 `--workdir` 改动后找不到 |
| 转码报 `ffmpeg 转码失败` | 响应 `error` 字段里有 ffmpeg stderr 的最后三行，通常是输入编码不被支持 |
| Docker 里 `transcode: false` | 镜像构建时取 ffmpeg 失败。构建日志里搜三条之一：`sha256sum -c` 报 FAILED（上游换了包）、`ffmpeg: 静态包解出来了但跑不起来`（架构不对，比如在 arm 机器上构建 amd64 包）、`ffmpeg: 装上了但跑不起来`（COPY 阶段出的问题）。`docker run --rm --entrypoint ffmpeg audio-forge -version` 直接验。国内网络记得传 `APK_MIRROR` / `PIP_INDEX_URL` |
| 内存持续上涨 | 正常路径下不会；检查是否有超大单文件 + 极低 `--max-mb` 组合导致反复重试 |
| 中文文件名下载变乱码 | 服务端用 RFC 5987 `filename*=UTF-8''…`，老浏览器可能不认，换现代浏览器 |

自检命令：

```bash
# 能力
curl -s http://127.0.0.1:8000/api/health | python -m json.tool

# 端到端（换成任意 .ncm 路径）
curl -F "files=@song.ncm" -F "target=auto" http://127.0.0.1:8000/api/convert

# 跑测试（不影响正在运行的服务）
python -m unittest discover -s tests -t . -v
```
