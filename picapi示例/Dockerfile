FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends exiftool \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir fastapi uvicorn[standard]

WORKDIR /app
COPY app.py /app/app.py
RUN mkdir -p /data/gallery /data/db

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
