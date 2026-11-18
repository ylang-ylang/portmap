#!/bin/sh
# Shared by the owned-network cold builder and the generated Dockerfile frontend.
set -eu

rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources
printf '%s\n' 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261101T000000Z bookworm main' > /etc/apt/sources.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils genisoimage ca-certificates
rm -rf /var/lib/apt/lists/*
