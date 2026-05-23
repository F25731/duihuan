FROM python:3.12-slim

# 创建非特权用户
RUN groupadd -r duihuan && useradd -r -g duihuan -m duihuan

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 设置文件权限
RUN chown -R duihuan:duihuan /app

# 切换到非特权用户
USER duihuan

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-8877}/api/health').read()" || exit 1

ENV HOST=0.0.0.0
ENV PORT=8877
ENV WEB_WORKERS=4
ENV WEB_THREADS=8
ENV WEB_TIMEOUT=120

EXPOSE 8877

CMD gunicorn app:application --bind 0.0.0.0:${PORT:-8877} --workers ${WEB_WORKERS:-4} --threads ${WEB_THREADS:-8} --timeout ${WEB_TIMEOUT:-120} --access-logfile - --error-logfile -
