FROM python:3.12-slim

# ffmpeg      - all reprojection and encoding
# exiftool    - reads camera metadata, writes GPano/GSpherical tags
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libimage-exiftool-perl \
      libgl1 \
      libglib2.0-0 \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV ORBIT_DATA=/data \
    PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
