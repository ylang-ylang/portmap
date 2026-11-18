FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261101T000000Z bookworm main' > /etc/apt/sources.list && \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils genisoimage ca-certificates && \
    rm -rf /var/lib/apt/lists/*
USER 1000:1000
WORKDIR /work
ENTRYPOINT ["qemu-system-x86_64"]
