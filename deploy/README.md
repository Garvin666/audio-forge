# 对外部署：鉴权与配额

本目录是「把 audio-forge 放到公网」的落地材料。**鉴权和配额在应用里实现**，
Nginx 只做 TLS、连接级限流和安全头 —— 因为只有应用知道「一次请求里几个文件、多少字节」，
反代看不到这些。

```
deploy/
├── audio-forge.service   systemd 单元（含加固项）
├── env.example           口令/令牌模板，放 /etc/audio-forge/env
└── nginx.conf            反代配置（TLS + 连接限流 + 超时 + 安全头）
```

---

## 1. 鉴权三种模式

| 模式 | 启动参数 | 谁适合 | 浏览器体验 |
| --- | --- | --- | --- |
| 关（默认） | 无 | 只在本机用 | 直接可用，**任何能访问端口的人都能传** |
| Basic | `--auth 用户:口令` | 人用，少配置 | 原生弹框，输一次即可，之后自动带凭据 |
| Bearer | `--token 令牌` | 脚本 / CI / 分享给他人 | 页面地址加 `?token=xxx` 打开一次，令牌存 sessionStorage |
| 两者都开 | 两个都给 | 混合场景 | 任一通过即可 |

```bash
# Basic（推荐给人工使用）
audioforge serve --host 0.0.0.0 --auth alice:'强口令'

# Bearer（推荐给自动化）
audioforge serve --host 0.0.0.0 --token "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"

# 口令写在环境变量里，避免 ps 泄漏
export AUDIOFORGE_AUTH='alice:强口令'
audioforge serve --host 0.0.0.0
```

**静态资源不鉴权**：`/`、`/app.js`、`/style.css`、`/favicon.ico` 谁都拿得到（它们不含数据）。
这是有意的 —— Bearer 模式下页面必须先加载出来，JS 才有机会从 `?token=` 里取出令牌。

**`/api/*` 全部需要凭证**，包括 `/api/health` 和 `/api/file/...`。
唯一例外是 `/api/ping`：恒回 `{"ok": true}`，不校验凭证、不计限速，专门给容器
healthcheck 和监控探针用（否则探活要么失败在 401，要么每 30 秒吃掉一次额度）。
下载是 `<a href>` 直连、带不了自定义头，所以也接受 `?token=` 查询参数。
令牌进 URL 会留在浏览器历史和代理日志里，**只建议用于自己发起的下载**；
`deploy/nginx.conf` 里配了 `Referrer-Policy: no-referrer` 防止它跟着 Referer 漏到第三方。

---

## 2. 配额

按客户端 IP 记账，固定窗口（分钟 / 日），进程内存储，重启清零。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--max-mb` | 200 | 单次请求体积上限（超出直接 413） |
| `--rate` | 120 | 每 IP 每分钟请求数（超出 429 + `Retry-After`） |
| `--daily-files` | 2000 | 每 IP 每天成功转换的文件数 |
| `--daily-gb` | 20 | 每 IP 每天上传总量（GB） |
| `--disk-gb` | 20 | 结果目录总量上限，超出时按 mtime 从旧到新删，降到 80% 停 |
| `--ttl` | 3600 | 结果保留秒数 |
| `--no-limits` | — | 一键关掉以上全部（除 `--max-mb`） |

设计取舍：

- **拒绝也要管请求体**：401 / 403 / 413 / 429 全部走同一条路 —— body ≤ 32 MB 读干净
  再回错误（否则客户端还在 `sendall` 就被 RST，拿不到错误响应），更大的直接关连接
  （不读，避免被用来做无界读取；不关的话残留分段字节会被当成下一条请求行，是走私口子）。
- 磁盘闸门给 20% 余量（清理到 80% 就停），避免每个请求都触发一次清理。
- 内存计数，**多实例不共享**。要做多实例共享配额得换 Redis，不在本项目范围。

---

## 2.5 反代层的两个坑（真机踩过，已修在 `nginx.conf` 里）

| 坑 | 现象 | 正确写法 |
| --- | --- | --- |
| **限流默认回 503** | `limit_req` / `limit_conn` 超限时 nginx 返回 `503 Service Temporarily Unavailable`，与应用层的 429 不一致，运维会误判成「服务挂了」 | 在 http 块显式写 `limit_req_status 429;` 与 `limit_conn_status 429;` |
| **安全头被 add_header 顶掉** | 只在 server 级写四个安全头、再在某个 location 里加一条 `Referrer-Policy`，该 location 的响应里 `nosniff`、`X-Frame-Options`、`HSTS` **全部消失**（nginx 的 add_header 是「子级有任意一条就不继承父级」） | 每个 location 各写全四个 `add_header`；server 级那份只兜底错误页 |

实测数据（nginx 1.28.3，`deploy/nginx.conf` 原样起服务）：

- 串行 300 次 `/api/ping`：过 200 / 拦 100（`rate=30r/s burst=40`）；串行 120 次时 0 拦 —— 突发额度足够吃下，别把「没触发 429」当成限流没生效。
- 并发 24 个健康检查：过 8 / 拦 16（`limit_conn 8`）。
- 声明 300 MB 的 POST：nginx 在读头阶段就回 `413`，不会真的收完 300 MB。
- 10 MB `.ncm` 经反代上传转 flac 约 0.7–0.8 s，52 MB 下载约 0.6 s，字节一致。

---

## 3. Linux + systemd 部署

```bash
# 1) 装包（用独立 venv，别污染系统 Python）
sudo useradd --system --home /var/lib/audio-forge --shell /usr/sbin/nologin audioforge
sudo mkdir -p /opt/audio-forge /var/lib/audio-forge /etc/audio-forge
sudo chown -R audioforge:audioforge /var/lib/audio-forge
sudo -u audioforge python3 -m venv /opt/audio-forge/venv
sudo -u audioforge /opt/audio-forge/venv/bin/pip install \
    "git+https://github.com/Garvin666/audio-forge.git"

# 2) 口令与令牌
sudo cp deploy/env.example /etc/audio-forge/env
sudo chmod 600 /etc/audio-forge/env
sudo -e /etc/audio-forge/env        # 改掉两行占位

# 3) 服务
sudo cp deploy/audio-forge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now audio-forge
systemctl status audio-forge --no-pager
journalctl -u audio-forge -f
```

启动横幅会自己打印当前鉴权模式和配额：

```
audio-forge 1.0.0  http://127.0.0.1:8000
  临时目录 : /var/lib/audio-forge/audioforge
  上传上限 : 200 MB
  通用转码 : ffmpeg version 7.1 ...
  鉴权     : basic+bearer（用户 alice + Bearer 令牌）
  配额     : 120 次/分 · 2000 文件/日 · 20 GB/日 · 磁盘 20 GB
```

### 3.1 装之前先验单元文件

```bash
# 判据是「零输出」而不是只看退出码 —— 见下方说明
sudo cp deploy/audio-forge.service /etc/systemd/system/
test -z "$(systemd-analyze verify /etc/systemd/system/audio-forge.service 2>&1)" \
  && echo "单元文件 OK" || echo "有告警/错误，看上面输出"
```

> **为什么不能只看退出码**：实测 `systemd-analyze verify` 对
> `ProtectSystem=banana`（非法枚举值）和 `Restrat=`（键名拼错）**仍然返回 0**，
> 只在 stdout 抱怨一句就继续。唯一会返回非 0 的是「`ExecStart` 指向的可执行文件不存在」。
> 所以正确判据是 **输出为空 + 退出码 0**，两个都要。
>
> 另外：`verify` 只检查 systemd 自己的语法与语义，**不会检查你传给程序的参数对不对**。
> `ExecStart` 里那 10 个 `--host/--port/--workdir/...` 是否都存在，得另外核
> （`audioforge serve --help` 对一遍）。

### 3.2 这个单元真的启动过（实测记录）

不是「写完就交」，是在一个 systemd 257 当 PID 1 的 Debian 13 环境里按上面步骤 1–3 装好、
真跑过的：

| 项 | 实测结果 |
| --- | --- |
| `systemd-analyze verify` | **零输出**（真路径，非 bind-mount） |
| `systemctl enable` 后重启 | 开机自启，`is-active=active` |
| 进程属主 | `audioforge`（`User=` 生效） |
| 监听 | 只 `127.0.0.1:8000`；非回环探测不可达（符合预期） |
| `/api/ping` | 200（无凭证） |
| `/api/health` | 401（无凭证）/ 200（带凭证） |
| **`ProtectSystem=strict` 是否真只读** | 在服务自己的 mount namespace 里试写：`/var/lib/audio-forge` **成功**；`/etc`、`/usr` 均 `Read-only file system` |
| 真实上传 | 10 MB `.ncm` → 9 841 292 B mp3，sha256 与真机直解**逐字节一致**，落盘属主 `audioforge` |
| `Restart=on-failure` | `kill -9` 主进程 → 6 秒内换新 PID，`NRestarts=1`，探活恢复 200 |
| `systemctl stop` | 干净退出，无残留进程 |

**两个只有真启动才看得见的行为**（已写进单元文件注释）：

1. **不加 `Environment=PYTHONUNBUFFERED=1`，`journalctl -u audio-forge` 里一行应用输出都没有**
   —— 只有 systemd 自己的 `Started` / `Stopped` 四行。原因是 systemd 下 stdout 是管道，
   Python 默认块缓冲，横幅和访问日志全卡在缓冲区；被 `SIGKILL` 时永久丢失。
   Docker 路径看不出这个问题（Dockerfile 里设了同一个变量），所以**别拿 Docker 的日志表现推断 systemd**。
2. **`EnvironmentFile` 指向不存在的文件时，服务不是「起来但没配置」，而是根本起不来**：

   ```
   Failed to load environment files: No such file or directory
   Failed with result 'resources'.
   ```

   `systemctl start` 返回非 0，`is-active` 停在 `activating`。排查时按这条报错直接查 `/etc/audio-forge/env`。

---

## 4. Nginx 接入

```bash
sudo cp deploy/nginx.conf /etc/nginx/conf.d/audio-forge.conf
sudo -e /etc/nginx/conf.d/audio-forge.conf     # 换域名与证书路径
sudo nginx -t && sudo systemctl reload nginx
```

三处必须对齐，否则会出现「上传到 100% 后报错」：

| 位置 | 项 | 取值 |
| --- | --- | --- |
| Nginx | `client_max_body_size` | ≥ 应用 `--max-mb`，留 10 MB 余量 |
| Nginx | `proxy_read_timeout` | ≥ 900s（大文件转码慢） |
| 应用 | `--max-mb` | ≤ 上面那个值 |

---

## 5. 部署后自检

```bash
HOST=https://audio.example.com
TOKEN=你在 env 里写的令牌

# 1) 未授权必须 401，且带 Basic 挑战头
curl -si "$HOST/api/health" | head -3
#   HTTP/2 401
#   www-authenticate: Basic realm="audio-forge", charset="UTF-8"

# 2) 带令牌应 200，并回显鉴权模式与今日用量
curl -s -H "Authorization: Bearer $TOKEN" "$HOST/api/health" | python -m json.tool
#   "auth": {"mode": "basic+bearer", "usage": {...}, "disk_mb": 0.0}

# 3) 静态页面不需要凭证（Bearer 模式靠它拿令牌）
curl -so /dev/null -w "%{http_code}\n" "$HOST/"

# 4) 端到端
curl -s -H "Authorization: Bearer $TOKEN" \
     -F "files=@song.ncm" -F "target=auto" "$HOST/api/convert"

# 5) 限流生效（把 --rate 调小后连打）
for i in $(seq 1 10); do
  curl -so /dev/null -w "%{http_code} " -H "Authorization: Bearer $TOKEN" "$HOST/api/health"
done; echo
#   期望看到末尾出现 429

# 6) 重启后 counters 归零（进程内计数）
sudo systemctl restart audio-forge
```

---

## 6. 常见问题

**Q：配了 `--auth`，但浏览器一直弹框、输对了也不进？**
口令里带 `:` 时要整体加引号：`--auth 'alice:a:b'`。仍然不行就看
`curl -si -u alice:口令 .../api/health` 是否 200 —— 那是浏览器缓存了旧凭据，清站点数据。

**Q：Bearer 模式打开页面一片「需要凭证」？**
地址少写了 `?token=`。令牌取一次后会存进 sessionStorage，之后同一标签页直接访问即可。

**Q：`/api/health` 一直 401，监控探活失败？**
换成 `/api/ping` —— 它不校验凭证、不计限速，就是为探活准备的。若确实要探
`/api/health` 的明细，带 Bearer 最省事：`-H "Authorization: Bearer $TOKEN"`。
不要为了探活把 `/api/health` 改成公开 —— 它会泄漏 ffmpeg 版本、磁盘占用和当前用量。

**Q：429 太多？**
先看是哪一项：响应里的 `error` 会写明「请求过于频繁」还是「今日配额已用尽」。
前者调 `--rate`，后者调 `--daily-files` / `--daily-gb`。用户分散在 NAT 后面时，
所有请求算同一个 IP，要么放宽配额，要么把服务放到反代后面并按 `X-Forwarded-For` 分桶
（本项目故意没做 —— 无条件信任该头等于让人随意伪造 IP，要做就得配可信代理白名单）。

**Q：配额重置时间？**
固定窗口：分钟配额的窗口起点是该 IP 的首次请求，日配额同理。不是自然日零点。
单机自用工具够用；要「每天零点重置」得换实现。

**Q：多实例部署？**
不行。配额、限流、job 目录都在本地进程/文件系统里，两实例之间互不可见。
