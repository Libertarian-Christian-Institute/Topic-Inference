# Image for the weekly summary Container Apps Job.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY *.py categories.yaml ./

RUN useradd -m appuser
USER appuser

CMD ["python", "weekly_summary.py"]
