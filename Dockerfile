# Use a slim Python base image for a smaller footprint
FROM python:3.11-slim

LABEL maintainer="Ioannis Kokkinis"
LABEL description="YouTube Indexer for Prowlarr/Sonarr - Torznab-compatible API"

# 1. Install system dependencies:
# nodejs: Critical for yt-dlp to decrypt YouTube's JavaScript signatures
# ffmpeg: Vital for yt-dlp to detect high-quality streams and metadata
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# 2. Update pip and install yt-dlp
# We no longer need 'requests' since we moved to the built-in urllib
RUN pip install --no-cache-dir --upgrade pip yt-dlp

# 3. Create and set app directory
WORKDIR /app

# 4. Copy application script
# Ensure your youtube_indexer.py has the 'if not q:' check we discussed
COPY youtube_indexer.py /app/

# 5. Environment variables
# PYTHONUNBUFFERED=1 ensures logs are sent to the console in real-time
ENV PYTHONUNBUFFERED=1

# 6. Expose the Torznab port
EXPOSE 9117

# 7. Add a simple healthcheck (optional but recommended)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:9117/api?t=caps || exit 1

# 8. Start the indexer
CMD ["python", "-u", "youtube_indexer.py"]
