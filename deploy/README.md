# 对外部署：鉴权与配额

本目录是「把 audio-forge 放到公网」的落地材料。**鉴权和配额在应用里实现**，
Nginx 只做 TLS、连接级限流和安全头 —— 因为只有应用知道「一次请求里几个文件、多少字节」，
反代看不到这些。

```
deploy/
├── audio-forge.service         systemd 单元（加固全生效 ≥ 242，verify 零输出 ≥ 247）
├── audio-forge.service.rhel7   老 systemd 降级版（< 242，如 CentOS 7 的 219）
├── env.example                 口令/令牌模板，放 /etc/audio-forge/env
└── nginx.conf                  反代配置（TLS + 连接限流 + 超时 + 安全头）
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

## 2.6 「nginx 反代容器」串联实测

上面那组是把 nginx 跑在 Windows 宿主、应用跑在本机进程。更接近真实部署的是
**nginx 容器 → 应用容器**（Docker 网络内部按服务名寻址）。这一组也跑通了，23 项断言全过。

配置**直接取自仓库的 `deploy/nginx.conf`**，只用 sed 改了 4 行（upstream 地址、证书两条、server_name），
其余原样。

| 项 | 实测 |
| --- | --- |
| `nginx -t` | syntax ok |
| 80 → 443 | `301`，`Location: https://…` |
| ALPN / HTTP/2 | `ALPN protocol: h2`，实发请求 `http_version=2` |
| 四个安全头 | `/`、`/api/health`、`/api/ping` 三个 location **全部齐全** |
| 鉴权穿透 | 无凭证 `/api/health` → `401` + `WWW-Authenticate: Basic realm="audio-forge"` |
| 真实文件 | 10.0 MB `.ncm` 上传转 mp3 1.0 s 级，经反代下载 **sha256 与真机直解一致** |
| `client_max_body_size 210m` | 215 MB → **413**，且响应体是 nginx 的 HTML（不是应用 JSON） |
| `limit_req 30r/s burst 40` | 300 次 `/api/ping`：**过 195 / 拦 105**，429 体为 nginx HTML |
| `limit_conn 8` | 24 路并发下载 52 MB flac：**过 8 / 拦 16**，恰好等于上限 |
| 拦截来源可查 | error.log 分别记到 `limiting requests` 106 次、`limiting connections` 16 次 |

**三个测法上的坑（都踩过）：**

1. **宿主 Windows 版 curl 不支持 HTTP/2**（Schannel 后端，`curl --http2` 直接报
   "the installed libcurl version does not support this"）。要验 h2 得用容器里
   libcurl+nghttp2 的 curl，或 `openssl s_client -alpn h2`。
2. **测并发连接前必须让 `limit_req` 的桶排空**。刚打完 300 次就立刻测并发，
   24 路会被 `limit_req` 全挡下，看起来像 `limit_conn` 设成了 1。两条限流会互相污染结论，
   要分开测、并到 error.log 里核对到底是 `limiting requests` 还是 `limiting connections`。
3. **别跟随 301**。`return 301 https://$host$request_uri` 里的 `$host` **不含端口**
   （生产 80→443 是对的），测试环境跟随会打到没映射的 443 上，报「连接被拒绝」。
   测跳转要关掉自动跟随，只看 `Location` 头。

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
audio-forge 1.1.0  http://127.0.0.1:8000
  临时目录 : /var/lib/audio-forge/audioforge
  上传上限 : 200 MB
  通用转码 : ffmpeg version 7.1 ...
  鉴权     : basic+bearer（用户 alice + Bearer 令牌）
  配额     : 120 次/分 · 2000 文件/日 · 20 GB/日 · 磁盘 20 GB
```

### 3.1 装之前先验单元文件

**systemd < 242 的机器（CentOS 7 是 219）请用 `deploy/audio-forge.service.rhel7`**，
完整版会因为 `RestrictSUIDSGID=` 等指令不被识别而失效 —— 逐条对照见 §3.4。

```bash
# 判据是「零输出」而不是只看退出码 —— 见下方说明
sudo cp deploy/audio-forge.service /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/audio-forge.service   # 带可执行位会被警告，见 §3.4 坑 4
test -z "$(systemd-analyze verify /etc/systemd/system/audio-forge.service 2>&1)" \
  && echo "单元文件 OK" || echo "有告警/错误（Unknown lvalue 就换降级版），看上面输出"
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

### 3.3 加固指令不是「写上就算」：逐条对照实测

「配置值存在」不等于「真生效」。做法：用 drop-in 把 `audio-forge.service` 的 `ExecStart`
换成探针（**其余加固指令全部继承，一个字不改**），再造一个零加固的对照单元，跑同一探针比差异。

| 指令 | 加固组 | 对照组 | 判定 |
| --- | --- | --- | --- |
| `NoNewPrivileges=yes` | `NoNewPrivs: 1` | `NoNewPrivs: 0` | 生效 |
| （seccomp） | 18 个 filter | 1 个 | 生效 |
| （能力边界）`CapBnd` | `…f7fcffff`：缺 `SYS_MODULE` / `SYS_RAWIO` / `MKNOD` | `…ffffffff` | 生效 |
| `PrivateDevices=yes` | `/dev` **18 项**，tmpfs，无 `kmsg` | `/dev` **175 项**，有 `kmsg` / `sda` / `ttyS0` | 生效 |
| `ProtectHome=yes` | `/home` `/root` `/run/user` 均被挂成 **tmpfs ro**；`/home/_marker` 不可见 | 无独立挂载；`_marker` **可见** | 生效 |
| `ProtectKernelModules=yes` | `/lib/modules/_marker` 不可见 | **可见** | 生效 |
| `ProtectKernelTunables=yes` | `/proc/sys` 挂载标志 = **ro** | 无独立挂载 | 生效 |
| `ProtectControlGroups=yes` | `/sys/fs/cgroup` = **ro** | **rw** | 生效 |
| `ProtectSystem=strict` | 写 `/usr` `/etc` `/boot` → **`Read-only file system`** | 也写不进去，但报 **`Permission denied`**（只是权限不够） | 生效 |
| `ReadWritePaths=/var/lib/audio-forge` | 该目录 **rw**，写入成功 | — | 生效 |
| `PrivateTmp=yes` | `/tmp` 是 `systemd-private-<id>-audio-forge.service-<rand>` 私有挂载 | 共享 `/tmp` | 生效 |
| `RestrictNamespaces=yes` | root 下 `unshare -n / -m / -p` 全被拒 | root 下 `unshare -n` **成功** | 生效 |
| `RestrictSUIDSGID=yes` | `chmod u+s` 被拒 | **成功** | 生效 |
| `LockPersonality=yes` | `setarch -R` 被拒 | **成功** | 生效 |

**两个测法上的坑，不写下来就会得出错的结论：**

1. **别用「命令是否报错」判断 `ProtectSystem`**。以非 root 跑时，对照组写 `/usr` 同样失败
   （`Permission denied`），看起来像「也保护住了」。必须看**错误类型**（EROFS 才是保护）或看**挂载标志**。
2. **`RestrictNamespaces` / `RestrictSUIDSGID` / `LockPersonality` 必须用 root 身份测**。
   以 `User=audioforge` 跑时 `CapEff=0`，`unshare` 在**对照组也会被拒**——那是「本来就没权限」，
   不是「被 systemd 限制」。用 root 身份重跑后差异才显形。
   （同理 `/root/_marker` 两组都不可见，是因为 `/root` 是 `0700 root`，这条**不作为证据**。）

### 3.4 换个 systemd 版本还能不能用：跨版本实测

上面全部是在 systemd 257（Debian 13）上测的。这份单元里的加固指令不是每个版本都有，
换台机器可能直接启动失败。

**两档要求，别混着说：**

| 目标 | 需要 | 判据 |
| --- | --- | --- |
| **能启动、加固全部生效** | systemd ≥ 242 | 启动后实测（见下） |
| **`systemd-analyze verify` 零输出** | systemd ≥ 247 | `test -z "$(systemd-analyze verify …)"` |

242–246 的关系是「能用但有告警」：`ProtectProc=`（247 引入）会被忽略，systemd 只打印一行
`Unknown key name 'ProtectProc' in section 'Service', ignoring.` 然后继续用其余全部加固。
**实测（systemd 245 / Ubuntu 20.04）**：整份单元直接 `verify`，输出恰好就是这一行，
`RC=0` —— 也就是说它既不会失败，也**不能靠退出码发现问题**（这也是判据必须是「零输出」的原因）。

| 指令 | 上游引入版本 | 219（CentOS 7） | 239（CentOS 8） | 245（Ubuntu 20.04） | 257（Debian 13） |
| --- | --- | --- | --- | --- | --- |
| `NoNewPrivileges` | 187 | ✓ | ✓ | ✓ | ✓ |
| `PrivateDevices` | 209 | ✓ | ✓ | ✓ | ✓ |
| `ProtectSystem` / `ProtectHome` | 214 | ✓ | ✓ | ✓ | ✓ |
| `TasksMax` | 227 | ✓ | ✓ | ✓ | ✓ |
| `AmbientCapabilities` | 229 | ✗ | ✓ | ✓ | ✓ |
| `ReadWritePaths` | 231 | ✗ 用 `ReadWriteDirectories` | ✓ | ✓ | ✓ |
| `MemoryMax` | 231 | ✗ 用 `MemoryLimit` | ✓ | ✓ | ✓ |
| `RestrictRealtime` | 231 | ✗ | ✓ | ✓ | ✓ |
| `ProtectKernelTunables` / `ProtectKernelModules` / `ProtectControlGroups` | 232 | ✗ | ✓ | ✓ | ✓ |
| **`ProtectSystem=strict`（取值）** | 232 | ✗ 只认 `yes`/`no`/`full` | ✓ | ✓ | ✓ |
| `SystemCallErrorNumber` | 232 | ✗ | ✓ | ✓ | ✓ |
| `RestrictNamespaces` | 233 | ✗ | ✓ | ✓ | ✓ |
| `LockPersonality` | 235 | ✗ | ✓（红帽反向移植） | ✓ | ✓ |
| `RestrictSUIDSGID` | 242 | ✗ | ✓（红帽反向移植） | ✓ | ✓ |
| `ProtectHostname` | 242 | ✗ | ✓（红帽反向移植） | ✓ | ✓ |
| `ProtectKernelLogs` | 244 | ✗ | ✗ | ✓ | ✓ |
| `ProtectClock` | 245 | ✗ | ✗ | ✓ | ✓ |
| **`ProtectProc=invisible`** | 247 | ✗ | ✗ | ✗（忽略，仅告警） | ✓ |

- 引入版本取自上游 `man/systemd.exec.xml` 的 version-info 指针与 `NEWS`，两边对得上
  （例：`* A new boolean sandboxing option RestrictSUIDSGID= has been added` 出现在 `CHANGES WITH 242` 段）。
- **245 这一列是真启动测的**：单元原样拷进去 → `systemd-analyze verify` 零输出 → `is-active=active` →
  `/api/ping` 200、无凭证 `/api/health` 401、Basic 与 Bearer 200 → `journalctl -u` 9 行应用输出
  （证实在 245 上 `PYTHONUNBUFFERED` 同样是必需项）→ 运行时取证 `NoNewPrivs=1`、`CapEff=0`、
  `CapBnd=…f7fcffff`、18 个 seccomp filter、`/dev` 只剩 18 项、服务 ns 里写 `/usr` 得
  `Read-only file system`、`/var/lib/audio-forge` 仍可写 —— 与 257 上一致。
- **219 与 239 是指令表扫描测的**：`systemd-analyze verify` 在容器里的 219 上**根本起不来**
  （它依赖 cgroup v1，本机是 cgroup v2，报 `Cannot determine cgroup we are running in`），
  只能退一步扫 systemd 二进制里的指令关键字表（`Service.<指令名>`）。

**老机器怎么办**：用 `deploy/audio-forge.service.rhel7`。它把上面 ✗ 的几项换成旧名或删掉
（`strict`→`full`、`ReadWritePaths`→`ReadWriteDirectories`、`MemoryMax`→`MemoryLimit`），
其余（ExecStart、鉴权、PYTHONUNBUFFERED）与完整版**逐字相同**。
17 个配置键在 219 的指令表里已逐条确认存在；在 245 上真启动过一遍：`verify` 零输出、
`/api/ping` 200、`ProtectSystem=full` 生效、`/usr` `/etc` 只读、`/var/lib/audio-forge` 可写。
代价是**丢掉 6 条内核/命名空间/setuid 相关的保护**，这些在 219 上没有替代写法，
只能靠「非 root 跑 + 反代限流」兜底。

**四个只有真做才知道的坑：**

1. **别拿版本号对照上游变更表下结论**。CentOS 8 的 systemd 239 里**有** `RestrictSUIDSGID`（上游 242）
   和 `LockPersonality`（上游 235）—— 是红帽反向移植的。版本号只能给下界，
   判据要用 `systemd-analyze verify` 的「零输出」（未知指令会打印 `Unknown lvalue`），
   它测的是**这台机器上这个构建**。
2. **指令「名」存在 ≠ 取值支持**。219 的二进制里能扫到 `ProtectSystem`，
   但 `strict` 取值不在（232 才有）；219 里出现的 `strict` 是 **`DevicePolicy=` 的取值**，
   跟 `ProtectSystem` 毫无关系。扫二进制必须看相邻字符串，不能只数名字出现次数。
3. **`verify` 只认合法单元名**。直接拿 `audio-forge.service.rhel7` 去验会 `EXIT=1 Invalid argument`，
   要先复制成 `*.service`。
4. **单元文件不能带可执行位**（`docker cp` 容易带进来）。257 上会警告
   `is marked executable`，于是「零输出」判据误报 —— 验之前先 `chmod 644`。
   同理，**降级单元在现代 systemd 上会打印一条** `MemoryLimit= 请改用 MemoryMax=` 的弃用告警，
   那是预期的，别当成错误。

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
