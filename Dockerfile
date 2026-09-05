# Ambit — container for a hosted demo (Render / Railway / Fly).
#
# Local development does not need this. Use:
#   PYTHONPATH=src python -m uvicorn ambit.app:app --port 8000
#
# This exists only so a judge can click a public URL without you running a
# server on your own laptop during review. See docs/DEPLOY.md.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render/Railway inject $PORT and expect the process to bind 0.0.0.0:$PORT.
# AMBIT_HOST defaults to 127.0.0.1 for local safety, so it is overridden here.
ENV AMBIT_HOST=0.0.0.0
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# start.sh generates the signing key and seeds one demo grant on first boot
# only — it is a no-op on every boot after that. The free tier's disk is not
# guaranteed to persist across a redeploy, so this makes a fresh box
# demoable without anyone running execution/issue_grant.py by hand.
CMD ["bash", "start.sh"]
