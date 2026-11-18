#!/bin/sh
# Desktop dependencies are inherited; SDK/APK downloads have no persistent cache.
set -eu

install_packages() {
    apt-get update
    apt-get install -y --no-install-recommends "$@"
    rm -rf /var/lib/apt/lists/*
}

setup_tools() {
    test "${ACCEPT_ANDROID_SDK_LICENSE:-0}" = 1
    install_packages unzip

    # Exact upstream archive revisions and Google repository checksum pins.
    mkdir -p /opt/android/system-images/android-30/default
    curl --fail --location --retry 3 \
        https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip \
        -o /tmp/platform-tools.zip
    echo '477254aa5f903c15cf51001717bdf347fb6b53e0  /tmp/platform-tools.zip' | sha1sum -c -
    unzip -q /tmp/platform-tools.zip -d /opt/android
    rm /tmp/platform-tools.zip

    curl --fail --location --retry 3 \
        https://dl.google.com/android/repository/emulator-linux_x64-16740729.zip \
        -o /tmp/emulator.zip
    echo 'f41f9e31eb5ca64926603c03265c50bbc0ff4172  /tmp/emulator.zip' | sha1sum -c -
    unzip -q /tmp/emulator.zip -d /opt/android
    rm /tmp/emulator.zip

    curl --fail --location --retry 3 \
        https://dl.google.com/android/repository/sys-img/android/x86_64-30_r11.zip \
        -o /tmp/system.zip
    echo '5e4de3946d46f88856c35efcc4d797b381456347  /tmp/system.zip' | sha1sum -c -
    unzip -q /tmp/system.zip -d /opt/android/system-images/android-30/default
    rm /tmp/system.zip

    # Official APK URLs/checksums only; never relax TLS verification.
    case "${FIREFOX_ANDROID_CHANNEL:-release}" in
        release)
            url=https://archive.mozilla.org/pub/fenix/releases/160.0/android/fenix-160.0-android-x86_64/fenix-160.0.multi.android-x86_64.apk
            sha256=11e9aaba70dfcccefded85cd67ebd7a6829d0f5dfad04b2f99771c3b5c71a9b7
            ;;
        nightly)
            url=https://archive.mozilla.org/pub/fenix/nightly/2026/11/2026-11-13-21-17-35-fenix-162.0a1-android-x86_64/fenix-162.0a1.multi.android-x86_64.apk
            sha256=63c38a072a25e15f1947f29fb3b1ffd352722ac86478062112b5426428754ebe
            ;;
        *) exit 2 ;;
    esac
    curl --fail --location --retry 3 "$url" -o /opt/android/firefox.apk
    echo "$sha256  /opt/android/firefox.apk" | sha256sum -c -
    printf '%s  /opt/android/firefox.apk\n' "$sha256" > /opt/android/firefox-apk.sha256
}

setup_runtime() {
    install_packages libpulse0 libvulkan1 libdrm2 libxkbcommon0 libxcb-cursor0
    dpkg-query -W > /app/browser-system-packages.txt
}

for phase in "$@"; do
    case "$phase" in
        tools) setup_tools ;;
        runtime) setup_runtime ;;
        *) printf 'Unknown Android setup phase: %s\n' "$phase" >&2; exit 2 ;;
    esac
done
