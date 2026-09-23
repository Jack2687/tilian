FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend
WORKDIR /app

COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN pip install --no-cache-dir -r /tmp/backend-requirements.txt
COPY . /app
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN useradd --create-home --uid 10001 tilian && chown -R tilian:tilian /app
USER tilian
WORKDIR /app/backend

EXPOSE 8000
CMD ["python", "run.py"]
