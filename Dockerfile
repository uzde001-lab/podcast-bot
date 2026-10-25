FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf /app/
# YouTube tez-tez o'zgaradi: har ishga tushganda yt-dlp ni yangilaymiz
CMD ["sh", "-c", "pip install -U -q 'yt-dlp[default]' ; python bot.py"]
