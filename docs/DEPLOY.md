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

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now audio-forge
journalctl -u audio-forge -f
```

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
- `docker-compose.yml` 的 healthcheck 打的是 `/api/ping`（无凭证、不计限速），
  所以开了 `AUDIOFORGE_AUTH` 也不会被判 unhealthy。鉴权变量从 `.env` 注入，
  见 `deploy/env.example`。
- 容器内必须 `--host 0.0.0.0`（Dockerfile 的 CMD 已经写好），否则宿主机端口映射打不进来。
- 端口映射建议写 `127.0.0.1:8000:8000`，只在宿主机回环上暴露；要对外由反代负责。
- 结果目录挂到 `/data`，容器重建不丢正在下载的文件。
- 磁盘要留够：转成 wav 时体积约为源文件的 3–4 倍，TTL 内的结果都会占着盘。

### 2.1 这套 Docker 配置的实测记录

不是「照文档写一遍」，是真的构建、起容器、跑真文件验过的：

| 项 | 实测值 |
| --- | --- |
| 构建（含 apt 装 ffmpeg） | 3 分 49 秒，全量约 130 MB 依赖 |
| 构建（改代码重建，走缓存） | 6.2 秒 |
| 镜像大小 | 832 MB（其中 ffmpeg 依赖的系统库约 449 MB，应用本体仅 484 KB） |
| 容器常驻内存 | 17 MB |
| healthcheck | 启动后第 2 次探询转 `healthy`，`/api/ping` 返回 200 |
| 真文件转换 | 10.0 MB `.ncm` → 9 841 292 B mp3，与真机直解**逐字节一致**（`cmp` 无差异） |
| 通用转码 | mp3 → flac，1.38 秒，`ffprobe` 验为 48 kHz 双声道 245.952 s |
| 卷持久化 | 重启容器后旧任务仍可下载，sha256 不变 |
| 超限 | 205 MB 上传 → **5 ms** 内返回 413（未读 body 即拒），服务不受影响 |

### 2.2 构建卡在 apt 时怎么处理

一次 ffmpeg 要拉 ~110 个包 / ~130 MB，任何一个包失败整层就废掉。`Dockerfile` 里已经做了
`Acquire::Retries=8` + 5 次外层循环 + 已下好的 `.deb` 复用，正常情况下能自己扛过抖动。

如果仍反复失败：

```bash
# 换国内源（Dockerfile 已内置该参数，改源比反复重试有效）
docker build --build-arg APT_MIRROR=mirrors.aliyun.com -t audio-forge .

# 别把本机代理硬塞进构建：容器到宿主的代理链路常常是 502 的来源
# 实测「不带任何 --build-arg HTTP_PROXY」的直连比走代理稳
```

> 踩过的坑：`--build-arg HTTP_PROXY=http://127.0.0.1:xxxx` 里的 `127.0.0.1` 指的是**容器自己**，
> 不是宿主机，会得到 `Connection refused`；要指向宿主得用 `http://http.docker.internal:3128`
> （仅 Docker Desktop）。而这条链路在批量下载时容易回 502 —— 直连反而更稳。

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
| `deploy/audio-forge.service` | systemd 单元，含 `ProtectSystem=strict`、`PrivateTmp` 等加固 |
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
| 真实 IP | 应用记的是直连地址（反代后恒为 127.0.0.1）。要做按 `X-Forwarded-For` 分桶，必须配可信代理白名单，否则等于让人随便伪造 IP —— 本项目故意没做 |

## 4. 故障排查

| 现象 | 排查 |
| --- | --- |
| 打开页面空白 | 看进程是否在跑；`curl -i http://127.0.0.1:8000/api/ping` 有没有 `{"ok": true}` |
| 页面能开但按钮一直灰 | `/api/health` 失败 → 前端拿不到能力列表；看浏览器 console |
| 上传到 100% 后报 413 | 请求超过 `--max-mb`，同时检查反代的 `client_max_body_size` |
| `/api/health` 返回 401 | 已启用鉴权。带 `-H "Authorization: Bearer <令牌>"`，或 `-u 用户:口令` |
| 容器一直 unhealthy | healthcheck 应打 `/api/ping`（不必带凭证）；若改成打 `/api/health`，配了鉴权就会 401 |
| 返回 429 且 `error` 提「请求过于频繁」 | 撞了 `--rate`；响应头有 `Retry-After` |
| 返回 429 且 `error` 提「今日配额已用尽」 | 撞了 `--daily-files` / `--daily-gb`，按需调大 |
| 返回 429 但 body 是 **HTML**、也没有 `Retry-After` | 撞的是 Nginx 的 `limit_req` / `limit_conn`，不是应用配额。调 `nginx.conf` 的 `rate` / `burst` / `limit_conn`。想分清就看 `Server:` 头，应用不返回它 |
| 磁盘满但 `--disk-gb` 设了 | 磁盘闸门只在**新请求进来时**触发；也可能超限前就已经写满，需外部监控兜底 |
| 提示「非 NCM 文件必须指定目标格式」 | 目标格式选了「自动」但有非 `.ncm` 文件，选具体格式 |
| 下载 404 | 超过 `--ttl` 被清理，或 job 目录被 `--workdir` 改动后找不到 |
| 转码报 `ffmpeg 转码失败` | 响应 `error` 字段里有 ffmpeg stderr 的最后三行，通常是输入编码不被支持 |
| Docker 里 `transcode: false` | 镜像构建时 apt 装 ffmpeg 失败，`docker run --rm audio-forge ffmpeg -version` 验一下 |
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
