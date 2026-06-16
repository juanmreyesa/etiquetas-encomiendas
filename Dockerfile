FROM python:3.12-slim

# cups-client aporta `lp`/`lpstat` para imprimir contra el CUPS del host
# (se comunica por el socket /run/cups/cups.sock montado desde el host)
RUN apt-get update \
    && apt-get install -y --no-install-recommends cups-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py crypto.py db.py i18n.py labels.py mailer.py ./
COPY templates ./templates
COPY static ./static

# Defaults sembrados en el primer arranque (luego se editan desde /admin).
ENV ETIQUETAS_DB=/data/etiquetas.db \
    PRINTER=Epson_L3210 \
    BASE_URL=http://localhost:8088

EXPOSE 8000

# 2 workers alcanzan de sobra para uso doméstico; timeout amplio por si lp tarda
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "app:app"]
