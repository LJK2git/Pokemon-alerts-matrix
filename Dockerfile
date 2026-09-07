FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IN_DOCKER=1 \
    DEBUG_DIR=/app/debug_runs

WORKDIR /app

# Shared libs headless Chrome needs to actually launch
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates \
    fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 \
    libxrandr2 libxss1 libxtst6 xdg-utils \
 && rm -rf /var/lib/apt/lists/*

# The actual Chrome browser binary -- this was missing before. seleniumbase's
# uc=True mode drives a real, installed Chrome (not just chromedriver), so
# without this SB(uc=True, ...) fails to even launch. That failure happens
# *before* check_for_invite() ever reaches _save_debug_artifacts(), which is
# why debug_runs stayed empty in Docker even though the same script worked
# fine locally (where Chrome is already installed).
RUN wget -q -O /tmp/chrome.deb \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Matched Chrome-for-Testing binary + chromedriver, as UC Mode expects
RUN seleniumbase get chromedriver

COPY . .

CMD ["python", "bot.py"]
