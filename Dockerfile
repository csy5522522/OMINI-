FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QT_QPA_PLATFORM=offscreen
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libdbus-1-3 libopengl0 libgl1 libegl1 libxkbcommon0 libfontconfig1 libfreetype6 libxcb1 ca-certificates fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt
COPY *.py ./
COPY web ./web
COPY assets ./assets
RUN useradd --create-home --uid 10001 omini && mkdir /data && chown omini:omini /data
USER omini
EXPOSE 18765
CMD ["python", "web_server.py", "--host", "0.0.0.0", "--port", "18765", "--data-dir", "/data"]
