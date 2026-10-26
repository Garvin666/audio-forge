# audio-forge 镜像：Python 运行时 + ffmpeg，装好后直接起 Web 平台。
# 构建：  docker build -t audio-forge .
# 运行：  docker run --rm -p 8000:8000 -v "$PWD/data:/data" audio-forge
#
# 这里刻意不写 `# syntax=docker/dockerfile:1`：那行只是让 BuildKit 去 Docker Hub
# 拉 frontend 镜像，本项目没用到任何 frontend 特性（heredoc 等），
# 却会在受限网络下变成构建的单点故障（实测 auth.docker.io 返回 Bad Gateway 就整个卡死在这里）。
#
# 基础镜像用 alpine（musl）而不是 debian-slim（glibc）：
#  - 本项目运行时零第三方依赖、全是纯 Python，镜像里唯一的原生二进制就是 ffmpeg 本身，
#    由 apk 提供、与 musl 配套编译，没有 ABI 风险；
#  - debian 下 `apt install ffmpeg` 会连带拉进整棵 libav* 依赖树（449 MB），
#    alpine 的 ffmpeg 包只有它的四分之一左右 —— 镜像从 832 MB 降到 ~200 MB。
# ⚠️ 用 musl 后 imageio-ffmpeg 随包的那份 glibc 静态二进制跑不起来。本项目不装它，
#    且 transcode.find_ffmpeg() 先查 PATH 再回落到它，所以无影响；若你自行加装，
#    请确保 PATH 上的 ffmpeg 优先（默认顺序已经是这样）。
FROM python:3.12-alpine

# ffmpeg 只服务于「通用格式互转」；不装也能跑（NCM 通道是纯 Python），只是该通道不可用。
# 一次 ffmpeg 要拉几十个包，任何一个失败整层就废掉，所以这里做了三件事：
#  - 外层 5 次循环：弱网下重试比整层重来便宜（apk --no-cache 不留半成品缓存）。
#  - 收尾真的把 ffmpeg 跑一次做断言：装没装上看这一行。
#    注意别写成 `ffmpeg -version | head -1` 当断言——管道退出码取自 head，
#    ffmpeg 不在也返回 0，`set -e` 拦不住（sh 又没 pipefail），是个假绿。
#  - APK_MIRROR 可选：国内网络可 --build-arg APK_MIRROR=mirrors.aliyun.com 换源。
ARG APK_MIRROR=
RUN set -eu; \
    if [ -n "$APK_MIRROR" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|$APK_MIRROR|g" /etc/apk/repositories; \
    fi; \
    ok=0; \
    for i in 1 2 3 4 5; do \
        if apk add --no-cache ffmpeg; then \
            ok=1; break; \
        fi; \
        echo "apk: ffmpeg install attempt $i failed, retrying"; \
        sleep 2; \
    done; \
    if [ "$ok" != 1 ]; then echo "apk: ffmpeg install failed after 5 attempts" >&2; exit 1; fi; \
    if ! ffmpeg -version >/dev/null 2>&1; then \
        echo "apk: ffmpeg 报成功但跑不起来" >&2; exit 1; \
    fi; \
    echo "ffmpeg 就绪：$(ffmpeg -version 2>&1 | head -1)"

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
