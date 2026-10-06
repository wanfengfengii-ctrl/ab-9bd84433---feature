FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DB_PATH=/data/app.db

WORKDIR /app
COPY app/ ./app/
COPY tests/ ./tests/
COPY verify/ ./verify/

EXPOSE 8000

# Default: run the API. The compose `verify` service overrides the command.
CMD ["python", "-m", "app.server"]
