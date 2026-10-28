FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ ./collector/
COPY migrations/ ./migrations/

RUN useradd --system --no-create-home app
USER app

CMD ["python", "-m", "collector.main"]
