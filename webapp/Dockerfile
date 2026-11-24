FROM python:3.12-slim

WORKDIR /app

# Stdlib only at skeleton stage (a12 budget-forced single process); add pinned
# requirements here when later phases need them.
COPY server/ server/

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/app.sqlite \
    PORT=8000

EXPOSE 8000

CMD ["python", "-m", "server.main"]
