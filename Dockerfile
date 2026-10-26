# audio-forge 镜像：Python 运行时 + ffmpeg，装好后直接起 Web 平台。
# 构建：  docker build -t audio-forge .
# 运行：  docker run --rm -p 8000:8000 -v "$PWD/data:/data" audio-forge
#
# 这里刻意不写 `# syntax=docker/dockerfile:1`：那行只是让 BuildKit 去 Docker Hub
# 拉 frontend 镜像，本项目没用到任何 frontend 特性（heredoc 等），
# 却会在受限网络下变成构建的单点故障（实测 auth.docker.io 返回 Bad Gateway 就整个卡死在这里）。
#
# 镜像体积的三笔账（`docker image inspect` 实测，别再用 `du` 对账，两者差 30% 以上）：
#
#   基础镜像 python:3.12-alpine        74.7 MB
#   静态 ffmpeg 单文件                 79.8 MB
#   本包 + pip 装出来的元数据          12.1 MB
#   ------------------------------------------
#   合计                              ~167 MB
#
# 走过两条弯路，都记在这里，免得再走回去：
#  ① debian-slim + `apt install ffmpeg` —— 连带 449 MB 的 libav* 依赖树，镜像 832 MB；
#  ② alpine + `apk add ffmpeg`         —— 也要 189 MB：x265 / aom / vpx / sdl2 全是
#     libavcodec 的 DT_NEEDED，删任何一项都跑不起来，`apk del` 无从下手，镜像 279 MB。
#
# 所以现在只搬「一个静态 ffmpeg 可执行文件」进来，不装整个 ffmpeg 生态。
# 静态版没有动态依赖（连 libc 都不需要），在 musl 镜像里照样能跑 —— ABI 风险是 0，
# 而不是「碰巧能跑」。代价：镜像里没有 ffprobe（本项目从不调用它，只调 ffmpeg）；
# 确实需要就自己 `apk add ffmpeg`，或用官方 `linuxserver/ffmpeg` 作为基础镜像。
#
# ⚠️ 用 musl 后 imageio-ffmpeg 随包的那份 glibc 二进制跑不起来。本项目不装它，
#    且 transcode.find_ffmpeg() 先查 PATH 再回落到它，所以无影响；若你自行加装，
#    请确保 PATH 上的 ffmpeg 优先（默认顺序已经是这样）。


# --------------------------------------------------------------------------- #
# 阶段一：取静态 ffmpeg
# --------------------------------------------------------------------------- #
# 校验和写死：URL 上的内容一旦变了，构建直接失败，而不是悄悄换一个二进制进来。
# 两个架构各钉一个哈希（amd64 / arm64），TARGETARCH 由 BuildKit 按 `--platform`
# 或宿主架构自动填；用老构建器时它为空，退回默认 amd64。
# 升级方式：本地下载新包 → sha256sum → 改下面 case 里的两行。
FROM alpine:3.22 AS ffmpeg-src

ARG APK_MIRROR=
# 想用别的包（其它架构 / 自建精简版）：同时传 FFMPEG_URL 与 FFMPEG_SHA256
ARG FFMPEG_URL=
ARG FFMPEG_SHA256=
ARG TARGETARCH=amd64

RUN set -eu; \
    if [ -z "$FFMPEG_URL" ]; then \
        case "$TARGETARCH" in \
            amd64) \
                FFMPEG_URL=https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz; \
                FFMPEG_SHA256=abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67; \
                ;; \
            arm64|aarch64) \
                FFMPEG_URL=https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz; \
                FFMPEG_SHA256=f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1; \
                ;; \
            *) \
                echo "ffmpeg: 没有 $TARGETARCH 的预置静态包，请用 --build-arg FFMPEG_URL=... FFMPEG_SHA256=... 指定" >&2; \
                exit 1; \
                ;; \
        esac; \
    fi; \
    if [ -z "$FFMPEG_SHA256" ]; then \
        echo "ffmpeg: 传了 FFMPEG_URL 就必须同时传 FFMPEG_SHA256（不校验哈希的构建不做）" >&2; \
        exit 1; \
    fi; \
    if [ -n "$APK_MIRROR" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|$APK_MIRROR|g" /etc/apk/repositories; \
    fi; \
    apk add --no-cache curl xz >/dev/null; \
    curl -fsSL --retry 5 --retry-delay 2 "$FFMPEG_URL" -o /tmp/ff.tar.xz; \
    echo "$FFMPEG_SHA256  /tmp/ff.tar.xz" | sha256sum -c -; \
    mkdir -p /tmp/x; \
    tar -xJf /tmp/ff.tar.xz -C /tmp/x; \
    install -m 0755 /tmp/x/ffmpeg-*/ffmpeg /usr/local/bin/ffmpeg; \
    if ! /usr/local/bin/ffmpeg -version >/dev/null 2>&1; then \
        echo "ffmpeg: 静态包解出来了但跑不起来" >&2; exit 1; \
    fi; \
    echo "ffmpeg 就绪：$(/usr/local/bin/ffmpeg -version 2>&1 | head -1)"


# --------------------------------------------------------------------------- #
# 阶段二：运行时
# --------------------------------------------------------------------------- #
FROM python:3.15-alpine

COPY --from=ffmpeg-src /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg

# 单独一层做断言：静态二进制跨 libc 搬运是「应该能跑」，这里证明它真的能跑。
# 注意别写成 `ffmpeg -version | head -1` 当断言 —— 管道退出码取自 head，是个假绿。
RUN if ! ffmpeg -version >/dev/null 2>&1; then \
        echo "ffmpeg: 装上了但跑不起来" >&2; exit 1; \
    fi

WORKDIR /app

# 先只拷元数据，让依赖层可缓存（本项目零依赖，这一步几乎瞬时）
COPY pyproject.toml README.md ./
COPY src ./src

# 本项目运行时零依赖，pip 只用来装自己 + 构建期拉 setuptools，所以没有 requirements。
# 不 `pip install --upgrade pip`：升级会把新版 pip 整份写进镜像层（实测多 20 MB），
# 而基础镜像自带的 pip 已经够装一个纯 Python 包。
# PIP_INDEX_URL 可选：国内网络可 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=
RUN set -eu; \
    idx=""; \
    if [ -n "$PIP_INDEX_URL" ]; then idx="--index-url $PIP_INDEX_URL"; fi; \
    pip install --no-cache-dir $idx .

# 转换结果落在 /data，挂卷即可持久化
ENV PYTHONUNBUFFERED=1 \
    AUDIOFORGE_WORKDIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器内必须监听 0.0.0.0，否则宿主端口映射打不进来。
# 对外暴露前请自行加鉴权/反代，见 README「安全边界」。
CMD ["audioforge", "serve", "--host", "0.0.0.0", "--port", "8000", "--workdir", "/data"]
