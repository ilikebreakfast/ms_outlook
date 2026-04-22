FROM python:3.11-slim

# Install system dependencies:
#   tesseract  — OCR engine
#   clamav     — antivirus scanner
#   freshclam  — ClamAV signature updater
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    clamav \
    clamav-freshclam \
    && rm -rf /var/lib/apt/lists/*

# Update ClamAV virus signatures at image build time.
# They will also be refreshed at container start via docker-compose.
RUN freshclam || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Output directories are mounted as volumes — create them so they exist
# even without a volume mount (e.g. quick local test runs).
RUN mkdir -p attachments raw_text parsed logs database config

CMD ["python", "main.py"]
