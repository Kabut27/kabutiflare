FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .
RUN mkdir -p /app/data

CMD ["python3", "bot.py"]
