FROM python:3.11-slim

# Install system dependencies (FFmpeg)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure data directories exist
RUN mkdir -p logs media

# Expose API port
EXPOSE 8000

# Run the bot
CMD ["python", "bot.py"]
