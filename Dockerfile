# Use a small official Python image
FROM python:3.11-slim

# Create app dir
WORKDIR /app

# Install system deps required for Docker SDK (none special) and tzdata if you need timezone handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user (optional, but note: must still access docker socket or run with proper group)
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
