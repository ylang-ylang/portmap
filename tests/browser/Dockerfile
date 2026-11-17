# Test-only images; never used by Portmap packaging or deployment.
FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34 AS base
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*

FROM base AS fixture
COPY src /app/src
COPY tests/browser/fixture.py tests/browser/dns.py /app/tests/browser/

FROM base AS target
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10000 --shell /bin/sh probe \
    && mkdir -p /run/sshd /state /meta
COPY tests/browser/fixture.py tests/browser/target.py /app/tests/browser/
CMD ["python", "/app/tests/browser/target.py"]

FROM base AS browser
# Toolchain layers do not change when adapter/extension/test source changes.
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils libgtk-3-0 libdbus-glib-1-2 libasound2 libx11-xcb1 libxt6 libnss3-tools fonts-liberation && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --retry 3 https://archive.mozilla.org/pub/firefox/nightly/2026/11/2026-11-13-21-17-35-mozilla-central/firefox-162.0a1.en-US.linux-x86_64.tar.xz -o /tmp/firefox.tar.xz && echo 'c03496c48a44f2bd49022387e86b228cffae2b555c5dfa2b0dd275050aafb9d4  /tmp/firefox.tar.xz' | sha256sum -c - && tar -xJf /tmp/firefox.tar.xz -C /opt && rm /tmp/firefox.tar.xz
RUN --mount=type=cache,id=portmap-browser-geckodriver,target=/var/cache/portmap-geckodriver if [ ! -f /var/cache/portmap-geckodriver/v0.36.0/geckodriver ]; then curl --fail --location --retry 3 --retry-all-errors https://github.com/mozilla/geckodriver/releases/download/v0.36.0/geckodriver-v0.36.0-linux64.tar.gz -o /var/cache/portmap-geckodriver/v0.36.0.tar.gz && echo '0bde38707eb0a686a20c6bd50f4adcc7d60d4f73c60eb83ee9e0db8f65823e04  /var/cache/portmap-geckodriver/v0.36.0.tar.gz' | sha256sum -c - && mkdir -p /var/cache/portmap-geckodriver/v0.36.0 && tar -xzf /var/cache/portmap-geckodriver/v0.36.0.tar.gz -C /var/cache/portmap-geckodriver/v0.36.0; fi && echo 'ad7cef75f87a17491abb212ae84923dd29f6782b8347959d21bd138efc2b5f99  /var/cache/portmap-geckodriver/v0.36.0/geckodriver' | sha256sum -c - && cp /var/cache/portmap-geckodriver/v0.36.0/geckodriver /usr/local/bin/geckodriver
COPY tests/browser/requirements.txt /tmp/browser-requirements.txt
RUN pip install --no-cache-dir -r /tmp/browser-requirements.txt && pip freeze > /app/browser-installed.txt
RUN apt-get update && apt-get install -y --no-install-recommends strace && rm -rf /var/lib/apt/lists/* && dpkg-query -W > /app/browser-system-packages.txt
COPY src /app/src
COPY extension/firefox /app/extension/firefox
COPY tools/build_firefox_extension.py /app/tools/build_firefox_extension.py
COPY pyproject.toml /app/pyproject.toml
COPY tests/browser /app/tests/browser
ENV MOZ_HEADLESS=1
CMD ["python", "/app/tests/browser/runner.py"]
