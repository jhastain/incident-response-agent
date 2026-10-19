# PagerDuty -> IR Agent webhook receiver. Deployed as a Cloud Run service (separate
# from the agent itself, which lives on Vertex AI Agent Engine).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py agent_client.py pagerduty_client.py main.py ./pdrecv/
# make the package importable as pdrecv.main
RUN touch /app/__init__.py

ENV PORT=8080
# Cloud Run sets $PORT; uvicorn binds it. Single worker — the agent call is I/O bound
# and Cloud Run scales by instance (concurrency handled at the platform level).
CMD exec uvicorn pdrecv.main:app --host 0.0.0.0 --port ${PORT}
