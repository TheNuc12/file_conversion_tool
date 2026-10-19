# Rotary Sign server. Build and run:
#   docker build -t rotary-sign .
#   docker run -p 8080:8080 -v rotary-sign-data:/data -e ROTARY_SIGN_URL=https://sign.example.com rotary-sign
# The first-time setup code is printed in the container log (docker logs <container>).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ROTARY_SIGN_DATA=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY rotary ./rotary
RUN pip install --no-cache-dir ".[server]" \
    && useradd --system --uid 10001 --home-dir /data rotary \
    && mkdir -p /data && chown rotary /data

USER rotary
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/login', timeout=4)" || exit 1
CMD ["rotary-sign", "run", "--host", "0.0.0.0", "--port", "8080"]
