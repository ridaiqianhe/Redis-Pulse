FROM python:3.10-slim-bullseye
WORKDIR /app
RUN pip install --no-cache-dir Flask==2.3.3 Werkzeug==2.3.7 gunicorn==23.0.0
COPY monitor /app/monitor
CMD ["gunicorn", "--bind", "127.0.0.1:16380", "--workers", "1", "--threads", "4", "--timeout", "30", "monitor.app:app"]
