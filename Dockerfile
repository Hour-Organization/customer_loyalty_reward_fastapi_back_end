FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Run STARTUP_TASK (default `migrate`; `setup` also seeds an empty database), then serve on $PORT (default 8000).
CMD ["sh", "-c", "python -m app.cli \"${STARTUP_TASK:-migrate}\" && exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
