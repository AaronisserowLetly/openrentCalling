FROM python:3.11-slim


WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium && playwright install-deps chromium

COPY . .

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 600 dashboard.app:app"]
