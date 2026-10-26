# syntax=docker/dockerfile:1

# audio-forge 镜像：Python 运行时 + ffmpeg，装好后直接起 Web 平台。
# 构建：  docker build -t audio-forge .
# 运行：  docker run --rm -p 8000:8000 -v "$PWD/data:/data" audio-forge

FROM python:3.12-slim

# ffmpeg 只服务于「通用格式互转」；不装也能跑，只是该通道不可用
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

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
