# Production Dockerfile for PropDAO Trading Bot on Railway.com
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system utilities and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . /app/

# Default runtime configuration (can be overridden via Railway environment variables)
ENV PROPDAO_SYMBOL=BTCUSDC
ENV PROPDAO_TIMEFRAME=4h
ENV PROPDAO_LEVERAGE=1.0
ENV PROPDAO_RISK_FRACTION=0.25
ENV PROPDAO_MODE=live

# Start the trading bot worker
CMD ["python", "-u", "run_bot.py"]
