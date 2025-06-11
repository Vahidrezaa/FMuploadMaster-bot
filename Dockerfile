FROM python:3.11-slim-bullseye

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN pip install flask==2.3.2

COPY . .

EXPOSE 10000

CMD ["python", "FMUploadMaster-bot.py"]
