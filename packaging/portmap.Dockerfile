FROM python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db
# Keep the legacy-glibc build independent of packages removed from live mirrors.
RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20250701T000000Z bullseye main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20250701T000000Z bullseye-security main' \
      > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyinstaller==6.14.1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
ENTRYPOINT ["python", "tools/build_portmap.py"]
