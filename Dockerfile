FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CALLBOX_HOST=0.0.0.0 CALLBOX_PORT=8787 CALLBOX_DATA_DIR=/data CALLBOX_DEMO=0
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home callbox && mkdir /data && chown callbox:callbox /data
COPY --chown=callbox:callbox callbox ./callbox
COPY --chown=callbox:callbox web ./web
USER callbox
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2)"
CMD ["python", "-m", "callbox"]
