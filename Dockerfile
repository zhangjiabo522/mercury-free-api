FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PROXY_HOST=0.0.0.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
RUN useradd --create-home --uid 10001 proxyuser && mkdir /app/data && chown proxyuser:proxyuser /app/data
COPY --chown=proxyuser:proxyuser proxy.py protocol.py upstream.py ./
COPY --chown=proxyuser:proxyuser admin.py generation.py responses_api.py storage.py tooling.py ./
COPY --chown=proxyuser:proxyuser assets ./assets
COPY --chown=proxyuser:proxyuser static ./static
USER proxyuser
EXPOSE 8000
CMD ["python", "proxy.py"]
