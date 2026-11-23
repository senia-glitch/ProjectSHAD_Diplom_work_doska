FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-runtime.txt /tmp/requirements-runtime.txt
RUN pip install --no-cache-dir -r /tmp/requirements-runtime.txt

FROM builder AS builder-dev
COPY requirements-tested.txt /tmp/requirements-tested.txt
RUN pip install --no-cache-dir -r /tmp/requirements-tested.txt

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
RUN useradd --create-home --uid 1000 app
WORKDIR /srv
COPY --chown=app:app . /srv
RUN mkdir -p /srv/.quality /srv/.pytest_cache && chown app:app /srv/.quality /srv/.pytest_cache
USER app
CMD ["python", "-m", "app.main"]

FROM runtime AS dev
COPY --from=builder-dev /opt/venv /opt/venv
