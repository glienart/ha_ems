ARG BUILD_FROM=ghcr.io/hassio-addons/base:latest
FROM $BUILD_FROM

# Install Python dependencies
RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir --break-system-packages \
        fastapi \
        uvicorn \
        httpx \
        "pydantic>=2.0"

# Copy app
COPY app/ /app/

# Startup script
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
