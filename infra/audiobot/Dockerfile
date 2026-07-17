FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1
USER 997:997

ENTRYPOINT ["python", "bot.py"]
