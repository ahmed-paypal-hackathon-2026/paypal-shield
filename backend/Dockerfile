# Server for the FastAPI core (Person 1's app lives in backend/app).
# Owned by Person 4. Python 3.11 slim, non-root, explicit uvicorn entrypoint.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Sandbox guard: refuses to boot against a non-Sandbox PayPal base URL unless
# ALLOW_NON_SANDBOX=true is explicitly set by the operator.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]

EXPOSE 8000

USER 1000
