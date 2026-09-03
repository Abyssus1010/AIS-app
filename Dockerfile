FROM python:3.11-slim

WORKDIR /app

# tesseract-ocr: the system binary pytesseract shells out to - needed for
# air_draft_resolver.py's OCR fallback on scanned ship-particulars PDFs
# (pytesseract itself is just a Python wrapper and does nothing without it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV DB_PATH=/data/ais.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
