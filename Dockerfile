# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 运行时依赖（Pillow 需要的最小 native 库 + tzdata）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 先拷依赖清单利用缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        -r requirements.txt

# 拷贝源代码
COPY . .

# 若用户未挂载 config.py，就用模板兜底
RUN [ -f config.py ] || cp config_example.py config.py

# 预热英雄图标（失败不影响构建，首次访问 /api/champions 会再拉一次）
RUN python champion_icons.py || true

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/config || exit 1

CMD ["uvicorn", "webui:app", "--host", "0.0.0.0", "--port", "8000"]
