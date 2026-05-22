FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOST=0.0.0.0
ENV PORT=8877
ENV WEB_WORKERS=4
ENV WEB_THREADS=8
ENV WEB_TIMEOUT=60

EXPOSE 8877

CMD gunicorn app:application --bind 0.0.0.0:${PORT:-8877} --workers ${WEB_WORKERS:-4} --threads ${WEB_THREADS:-8} --timeout ${WEB_TIMEOUT:-60} --access-logfile - --error-logfile -
