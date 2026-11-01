FROM python:3.11-slim

# Prevent python from buffering stdout/stderr and writing .pyc files
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

# Install minimal OS dependencies for network requests and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application logic and static templates
COPY scripts/ scripts/
COPY templates/ templates/
COPY app.py .

# Create volume mount points
RUN mkdir -p data ledgers

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
