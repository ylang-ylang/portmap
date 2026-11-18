#!/bin/sh
# Dependency construction only; current application/test source is supplied separately.
set -eu

install_packages() {
    apt-get update
    apt-get install -y --no-install-recommends "$@"
    rm -rf /var/lib/apt/lists/*
}

setup_base() {
    install_packages openssl ca-certificates
    mkdir -p /app
}

setup_desktop() {
    install_packages curl xz-utils libgtk-3-0 libdbus-glib-1-2 libasound2 \
        libx11-xcb1 libxt6 libnss3-tools fonts-liberation

    curl --fail --location --retry 3 \
        https://archive.mozilla.org/pub/firefox/nightly/2026/11/2026-11-13-21-17-35-mozilla-central/firefox-162.0a1.en-US.linux-x86_64.tar.xz \
        -o /tmp/firefox.tar.xz
    echo 'c03496c48a44f2bd49022387e86b228cffae2b555c5dfa2b0dd275050aafb9d4  /tmp/firefox.tar.xz' | sha256sum -c -
    tar -xJf /tmp/firefox.tar.xz -C /opt
    rm /tmp/firefox.tar.xz

    mkdir -p /tmp/portmap-geckodriver/v0.36.0
    curl --fail --location --retry 3 --retry-all-errors \
        https://github.com/mozilla/geckodriver/releases/download/v0.36.0/geckodriver-v0.36.0-linux64.tar.gz \
        -o /tmp/portmap-geckodriver/v0.36.0.tar.gz
    echo '0bde38707eb0a686a20c6bd50f4adcc7d60d4f73c60eb83ee9e0db8f65823e04  /tmp/portmap-geckodriver/v0.36.0.tar.gz' | sha256sum -c -
    tar -xzf /tmp/portmap-geckodriver/v0.36.0.tar.gz -C /tmp/portmap-geckodriver/v0.36.0
    echo 'ad7cef75f87a17491abb212ae84923dd29f6782b8347959d21bd138efc2b5f99  /tmp/portmap-geckodriver/v0.36.0/geckodriver' | sha256sum -c -
    cp /tmp/portmap-geckodriver/v0.36.0/geckodriver /usr/local/bin/geckodriver
    rm -rf /tmp/portmap-geckodriver

    pip install --no-cache-dir -r "$(dirname "$0")/requirements.txt"
    pip freeze > /app/browser-installed.txt
    install_packages strace
    dpkg-query -W > /app/browser-system-packages.txt
}

setup_target() {
    install_packages openssh-server
    useradd --create-home --uid 10000 --shell /bin/sh probe
    mkdir -p /run/sshd /state /meta
}

for phase in "$@"; do
    case "$phase" in
        base) setup_base ;;
        desktop) setup_desktop ;;
        target) setup_target ;;
        *) printf 'Unknown browser setup phase: %s\n' "$phase" >&2; exit 2 ;;
    esac
done
