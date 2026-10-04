FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user: if the app were ever compromised, the attacker
# gets no root in the container. data/ holds the SQLite DB, so it must be writable.
RUN useradd --create-home --uid 10001 nadav && mkdir -p /app/data && chown nadav /app/data
USER nadav

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
