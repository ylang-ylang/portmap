FROM python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyinstaller==6.14.1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
ENTRYPOINT ["python", "tools/build_client.py"]
