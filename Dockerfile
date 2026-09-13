# The API as a long-lived container.
#
# Railway (or any container host) runs this. The reason it is not Lambda: the
# live run view is Server-Sent Events, and a Lambda Function URL buffers the
# whole response before returning it, which turns a four-minute run from
# "watch it happen" into "blank screen, then everything at once". A container
# speaks HTTP natively, so SSE works the way it does locally.
#
# The S3-triggered agent stays on Lambda. That one genuinely is dormant between
# revisions, so paying per invocation beats paying for idle. This is the screen
# the AD sits in front of, which is a different shape of thing.

FROM python:3.12-slim

# Python in a container: no .pyc clutter, unbuffered logs so Railway's log
# viewer shows output as it happens rather than at flush time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first, so a source-only change reuses the installed
# layer instead of reinstalling the whole tree on every deploy.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# api: FastAPI, uvicorn, SSE. db: the Postgres driver, because a container's
# disk does not survive a redeploy and SQLite there would lose the database.
# fallback: Groq and Gemini, the last rungs of the model chain.
RUN pip install --upgrade pip \
    && pip install ".[api,db,fallback]"

# Where uploaded drafts are staged before parsing. Created here so the first
# upload does not race a missing directory.
RUN mkdir -p /app/workspace/drafts

# Railway assigns the port at runtime and injects it as $PORT. The default
# keeps `docker run` working locally without setting anything.
ENV PORT=8000
EXPOSE 8000

# Shell form on purpose: $PORT has to be expanded at runtime, and exec form
# would pass the literal string "$PORT" to uvicorn.
# --proxy-headers so the app sees the real scheme behind Railway's TLS
# terminator, which is what makes `Secure` cookies work.
CMD uvicorn bluepages.api.app:app \
    --host 0.0.0.0 \
    --port ${PORT} \
    --proxy-headers \
    --forwarded-allow-ips '*'
