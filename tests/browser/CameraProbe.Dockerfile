# Isolated prerequisite only; builds no Portmap source and installs no extension.
ARG CAMERA_BASE
FROM ${CAMERA_BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libdrm2 && rm -rf /var/lib/apt/lists/*
COPY camera_probe.py /probe/camera_probe.py
ENTRYPOINT ["python", "/probe/camera_probe.py"]
