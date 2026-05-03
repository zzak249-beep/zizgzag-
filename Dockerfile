FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.9.5 loguru==0.7.2 numpy==1.26.4
COPY . .
CMD ["python", "bot.py"]
