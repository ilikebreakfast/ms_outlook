FROM python:3.11-slim

# Prevent python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system-level dependencies for Tesseract OCR falling back scanners
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy v2 pipeline source code
COPY . /app

EXPOSE 8000

CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
