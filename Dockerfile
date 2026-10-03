# MuratJARVIS — Headless Dashboard Container
#
# Bu container SADECE dashboard/web API'yi çalıştırır.
# GUI (PyQt6), ses ve kamera FİZİKSEL DONANIM gerektirdiği için
# container içinde çalışmaz — masaüstü için normal kurulum yapın.
#
# Kullanım:
#   docker build -t jarvis-dashboard .
#   docker run -p 8080:8080 jarvis-dashboard

FROM python:3.12-slim

LABEL org.opencontainers.image.title="MuratJARVIS Dashboard"
LABEL org.opencontainers.image.description="Headless web dashboard for MuratJARVIS"
LABEL org.opencontainers.image.author="Murat Uzpek"

# Sistem bağımlılıkları (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python bağımlılıkları (önce cache'lenmesi için ayrı katman)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browser'lar (opsiyonel — çoğu dashboard özelliği için gerekmez)
# RUN playwright install chromium

# Kaynak kodu kopyala
COPY pyproject.toml .
COPY src/ src/

# Paketi kur (editable değil — immutable)
RUN pip install --no-cache-dir -e .

# Veri dizini (persistent volume mount noktası)
VOLUME ["/data"]

# Dashboard portu
EXPOSE 8080

# Sağlık kontrolü
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/login || exit 1

# Non-root kullanıcı
RUN useradd -m -s /bin/bash jarvis && chown -R jarvis:jarvis /app /data
USER jarvis

# Headless dashboard başlat
ENV QT_QPA_PLATFORM=offscreen
ENTRYPOINT ["python", "-m", "jarvis.dashboard.server"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
