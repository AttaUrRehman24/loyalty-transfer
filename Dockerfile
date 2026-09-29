# One image for the API, the worker, the mock partners, tests and lint.
FROM python:3.12-slim

# No .pyc files in the image; logs go straight to stdout without buffering.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Install dependencies first so code changes do not re-download packages.
# Dev tools are included so `make test` and `make lint` run in this same image.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app && chown -R app /srv
USER app

EXPOSE 8000

# Default command: the API. docker-compose.yml overrides it for other services.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
