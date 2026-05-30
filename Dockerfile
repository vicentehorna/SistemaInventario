# Sistema de Inventario — imagen para Render (Web Service + Docker)
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

# ODBC Driver 18 para SQL Server (misma lógica que database.py en Linux)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    unixodbc \
    unixodbc-dev \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/microsoft.gpg] \
        https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libharfbuzz0b \
    libffi-dev \
    shared-mime-info \
    fontconfig \
    fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-prod.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements-prod.txt

COPY . .

# Render inyecta PORT en tiempo de ejecución
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD /bin/sh -c 'curl -fsS "http://127.0.0.1:${PORT:-10000}/" || exit 1'

CMD ["/bin/sh", "-c", \
    "exec gunicorn app:app \
    --bind 0.0.0.0:${PORT} \
    --workers ${WEB_CONCURRENCY:-1} \
    --timeout ${GUNICORN_TIMEOUT:-120} \
    --access-logfile - \
    --error-logfile -"]
