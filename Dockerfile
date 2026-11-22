# Image for the chatbot summary Container Apps Job (monthly by default; REPORT_PERIOD=week for weekly).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY *.py categories.yaml ./

RUN useradd -m appuser
USER appuser

CMD ["python", "chatbot_summary.py"]
