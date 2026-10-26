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
| `--host` | `127.0.0.1` | 改成 `0.0.0.0` 才对局域网开放（想清楚再改） |
| `--port` | `8000` | |
| `--max-mb` | `200` | 单次请求上限 |
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
- 容器内必须 `--host 0.0.0.0`（Dockerfile 的 CMD 已经写好），否则宿主机端口映射打不进来。
- 端口映射建议写 `127.0.0.1:8000:8000`，只在宿主机回环上暴露；要对外由反代负责。
- 结果目录挂到 `/data`，容器重建不丢正在下载的文件。
- 磁盘要留够：转成 wav 时体积约为源文件的 3–4 倍，TTL 内的结果都会占着盘。

---

## 3. 公网（Nginx 反代）

**先明确风险**：服务本身没有鉴权、没有速率限制、没有配额。放到公网等于把一个
「任意人可上传任意大文件、并消耗 CPU 转码」的入口交出去。至少补上下面三项，否则
不要上线。

### 3.1 加鉴权（Nginx Basic Auth 最小方案）

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd youruser
```

### 3.2 站点配置

```nginx
server {
    listen 443 ssl http2;
    server_name audio.example.com;

    ssl_certificate     /etc/letsencrypt/live/audio.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/audio.example.com/privkey.pem;

    # 必须和 --max-mb 对齐，略大一点留 header 余量
    client_max_body_size 210m;
    client_body_timeout 300s;

    # 上传/下载走大文件，关掉缓冲免得中转占盘
    proxy_request_buffering off;
    proxy_buffering off;

    location / {
        auth_basic           "audio-forge";
        auth_basic_user_file /etc/nginx/.htpasswd;

        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        # 大文件转码慢，读超时给足
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
}
```

后端仍然只监听 `127.0.0.1:8000`，公网流量只经过 Nginx。

### 3.3 上线清单

| 项 | 建议 |
| --- | --- |
| 鉴权 | Basic Auth / OAuth2 Proxy / 公司 SSO，三选一，别裸奔 |
| 上传上限 | `--max-mb` 与 `client_max_body_size` 同时设，后者略大 |
| 结果保留 | `--ttl` 调小（如 900 秒），减少磁盘占用和残留风险 |
| 并发 | `ServerConfig.concurrency` 默认 4；转码吃 CPU，按核数调，别超 `核数/2` |
| 磁盘 | `/data` 单独挂盘 + 定时清理；监控剩余空间 |
| 日志 | 用 `-q` 关掉访问日志，交给 Nginx 记；异常栈仍会打到标准错误 |
| 出网 | 容器/进程不需要任何出网权限，可在防火墙上禁掉 |
| 内容合规 | 不要把它做成公开的「音乐下载站」，见 README 的合规段落 |

### 3.4 反向代理后拿不到真实 IP

`server.py` 只记 `self.address_string()`，也就是直连地址（反代后恒为 127.0.0.1）。
要真实 IP 就得读 `X-Real-IP`，但这需要信任边界判断，本工具故意没做 —— 需要的话在
自己的分支里改 `log_message`，别无条件信任该头。

---

## 4. 故障排查

| 现象 | 排查 |
| --- | --- |
| 打开页面空白 | 看进程是否在跑；`curl -i http://127.0.0.1:8000/api/health` 有没有 JSON |
| 页面能开但按钮一直灰 | `/api/health` 失败 → 前端拿不到能力列表；看浏览器 console |
| 上传到 100% 后报 413 | 请求超过 `--max-mb`，同时检查反代的 `client_max_body_size` |
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
