FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY frontend/ ./frontend/

COPY bootstrap.sh /app/bootstrap.sh
RUN chmod +x /app/bootstrap.sh

RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

RUN mkdir -p /app/tmp

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000 3002

CMD ["/app/bootstrap.sh"]
