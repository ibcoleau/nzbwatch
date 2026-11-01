FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd --gid 10001 nzbwatch && useradd --uid 10001 --gid 10001 --create-home nzbwatch && mkdir -p /data && chown -R nzbwatch:nzbwatch /data
COPY app ./app
USER nzbwatch
EXPOSE 8787
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--no-access-log"]
