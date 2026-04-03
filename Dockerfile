FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

CMD ["xvfb-run", "-a", "python", "discord_bot.py"]