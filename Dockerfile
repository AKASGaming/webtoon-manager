FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/AKASGaming/webtoon-manager"
LABEL org.opencontainers.image.description="Web-based GUI for managing and downloading webtoons"
LABEL org.opencontainers.image.title="webtoon-manager"

# Install system dependencies
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories
RUN mkdir -p /app/downloads /app/db /app/cache/thumbnails

COPY . .

EXPOSE 8128

CMD ["python", "app.py"]
