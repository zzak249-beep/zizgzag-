FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir numpy==1.26.4 pandas==2.2.2 \
 && pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "-u", "bot.py"]
