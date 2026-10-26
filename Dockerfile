# audio-forge 镜像：Python 运行时 + ffmpeg，装好后直接起 Web 平台。
# 构建：  docker build -t audio-forge .
# 运行：  docker run --rm -p 8000:8000 -v "$PWD/data:/data" audio-forge
#
# 这里刻意不写 `# syntax=docker/dockerfile:1`：那行只是让 BuildKit 去 Docker Hub
# 拉 frontend 镜像，本项目没用到任何 frontend 特性（heredoc 等），
# 却会在受限网络下变成构建的单点故障（实测 auth.docker.io 返回 Bad Gateway 就整个卡死在这里）。

FROM python:3.12-slim

# ffmpeg 只服务于「通用格式互转」；不装也能跑，只是该通道不可用。
# 一次 ffmpeg 要拉 ~110 个包 / ~130 MB，任何一个包失败整层就废掉，所以这里做了四件事：
#  - Acquire::Retries=8 + 外层 5 次循环：弱网下已下好的 .deb 会被复用，重试比整层重来便宜。
#  - cache 挂载：包缓存与 apt 索引留在构建缓存里（不进镜像层），重建这一层几乎瞬时。
#    也正因为不进镜像层，这里不需要 `rm -rf /var/lib/apt/lists/*`——删了反而让下次重建重拉索引。
#  - 收尾真的把 ffmpeg 跑一次做断言：装没装上看这一行。
#    注意别写成 `ffmpeg -version | head -1` 当断言——管道退出码取自 head，
#    ffmpeg 不在也返回 0，`set -e` 拦不住（dash 又没 pipefail），是个假绿。
#  - APT_MIRROR 可选：国内网络可 --build-arg APT_MIRROR=mirrors.aliyun.com 换源。
# 需要 BuildKit（`--mount=type=cache`）；Docker 23+ 默认就是 BuildKit。
ARG APT_MIRROR=
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eu; \
    if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get -o Acquire::Retries=8 update; \
    ok=0; \
    for i in 1 2 3 4 5; do \
        if apt-get -o Acquire::Retries=8 install -y --no-install-recommends ffmpeg; then \
            ok=1; break; \
        fi; \
        echo "apt: ffmpeg install attempt $i failed, retrying (cached .deb will be reused)"; \
        sleep 2; \
    done; \
    if [ "$ok" != 1 ]; then echo "apt: ffmpeg install failed after 5 attempts" >&2; exit 1; fi; \
    if ! ffmpeg -version >/dev/null 2>&1; then \
        echo "apt: ffmpeg 报成功但跑不起来" >&2; exit 1; \
    fi; \
    echo "ffmpeg 就绪：$(ffmpeg -version 2>&1 | head -1)"

WORKDIR /app

# 先只拷元数据，让依赖层可缓存（本项目零依赖，这一步几乎瞬时）
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

# 转换结果落在 /data，挂卷即可持久化
ENV PYTHONUNBUFFERED=1 \
    AUDIOFORGE_WORKDIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器内必须监听 0.0.0.0，否则宿主端口映射打不进来。
# 对外暴露前请自行加鉴权/反代，见 README「安全边界」。
CMD ["audioforge", "serve", "--host", "0.0.0.0", "--port", "8000", "--workdir", "/data"]
