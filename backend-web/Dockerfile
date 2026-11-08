# Backend-Web 服务 Dockerfile
# 单阶段构建，直接使用Python源码

# 基础镜像走 docker.io 官方名（由 buildx 的 buildkitd 镜像加速配置拉取，见流水线 BUILDKIT 配置）
FROM python:3.11-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 默认保留基础镜像中的 Debian 软件源和 Signed-By 签名校验。
# 官方源不可达时才覆盖主机名，例如 --build-arg APT_MIRROR=mirrors.ustc.edu.cn。
# 只接受主机名，不接受 trusted=yes 或其它 apt 选项。
ARG APT_MIRROR=
RUN set -eu; \
    if [ -n "${APT_MIRROR}" ]; then \
      case "${APT_MIRROR}" in \
        *[!A-Za-z0-9.-]*|-*|*-|*..*|.*) \
          echo "APT_MIRROR must be a hostname, not an apt option" >&2; \
          exit 1 ;; \
      esac; \
      changed=0; \
      for sources in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "${sources}" ]; then \
          sed -i \
            -e "s|https://deb.debian.org|https://${APT_MIRROR}|g" \
            -e "s|http://deb.debian.org|http://${APT_MIRROR}|g" \
            "${sources}"; \
          changed=1; \
        fi; \
      done; \
      if [ "${changed}" -ne 1 ]; then \
        echo "Debian apt source file not found" >&2; \
        exit 1; \
      fi; \
    fi
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

RUN apt-get update && apt-get install -y curl ca-certificates gcc \
    fonts-dejavu-core fonts-liberation \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制项目源码
COPY common /app/common
COPY backend-web /app/backend-web
COPY launcher /app/launcher

# 从pyproject.toml提取并安装依赖
RUN python -c "\
import tomllib; \
deps = tomllib.load(open('/app/backend-web/pyproject.toml','rb')).get('project',{}).get('dependencies',[]); \
open('/tmp/requirements.txt','w').write('\n'.join(deps))" && \
    pip install --no-cache-dir -r /tmp/requirements.txt

RUN python -m playwright install --with-deps chromium
RUN python -m patchright install --with-deps chromium

# 创建日志目录
RUN mkdir -p /app/backend-web/logs /app/static/uploads

# 暴露端口
EXPOSE 8089

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV TZ=Asia/Shanghai

# 启动服务
CMD ["python", "backend-web/main.py"]
