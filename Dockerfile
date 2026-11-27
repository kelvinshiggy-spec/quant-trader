FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app
RUN useradd -u 10001 -m qt && apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qt ./qt
COPY scripts ./scripts
RUN mkdir -p /app/data && chown -R qt:qt /app
USER qt
CMD ["python", "-m", "qt.engine"]
