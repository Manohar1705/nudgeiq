FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies first, so code changes do not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a normal user, not root.
RUN useradd --create-home --uid 10001 nudgeiq
USER nudgeiq

EXPOSE 8000

# The tables are created automatically when the app connects to PostgreSQL.
# One worker on purpose: the repository shares a single database connection.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
