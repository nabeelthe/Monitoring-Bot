FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nuva_bot/ nuva_bot/
COPY config.yaml .
COPY deploy/docker-entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd -m bot && mkdir -p /app/data && chown -R bot:bot /app \
    && chmod +x /usr/local/bin/entrypoint.sh

ENV NUVA_STATE=/app/data/state.json \
    PYTHONUNBUFFERED=1

VOLUME ["/app/data"]

# web dashboard + /healthz + Prometheus /metrics
EXPOSE 8088

# fails (non-zero) if config is broken → container marked unhealthy at start
HEALTHCHECK --interval=5m --timeout=20s CMD python -m nuva_bot --check-config >/dev/null || exit 1

# Entrypoint fixes volume perms (root-owned mounts) then drops to the 'bot' user.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "nuva_bot"]
