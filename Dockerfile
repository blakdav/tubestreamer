FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir flask apscheduler \
    && pip install --no-cache-dir --pre "yt-dlp[default]"

COPY app.py .
COPY templates/ templates/

EXPOSE 5000

CMD ["python", "app.py"]
