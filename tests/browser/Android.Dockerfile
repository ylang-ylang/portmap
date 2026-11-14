# Cache SDK/APK downloads independently of the extension and adapter source.
ARG BROWSER_BASE
FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34 AS android-tools
ARG ACCEPT_ANDROID_SDK_LICENSE=0
RUN test "$ACCEPT_ANDROID_SDK_LICENSE" = 1
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*
# Exact upstream archive names/revisions plus Google repository checksum pins.
RUN mkdir -p /opt/android/system-images/android-30/default && curl --fail --location --retry 3 https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip -o /tmp/platform-tools.zip && echo '477254aa5f903c15cf51001717bdf347fb6b53e0  /tmp/platform-tools.zip' | sha1sum -c - && unzip -q /tmp/platform-tools.zip -d /opt/android && rm /tmp/platform-tools.zip
RUN curl --fail --location --retry 3 https://dl.google.com/android/repository/emulator-linux_x64-16740729.zip -o /tmp/emulator.zip && echo 'f41f9e31eb5ca64926603c03265c50bbc0ff4172  /tmp/emulator.zip' | sha1sum -c - && unzip -q /tmp/emulator.zip -d /opt/android && rm /tmp/emulator.zip
RUN curl --fail --location --retry 3 https://dl.google.com/android/repository/sys-img/android/x86_64-30_r11.zip -o /tmp/system.zip && echo '5e4de3946d46f88856c35efcc4d797b381456347  /tmp/system.zip' | sha1sum -c - && unzip -q /tmp/system.zip -d /opt/android/system-images/android-30/default && rm /tmp/system.zip
ARG FIREFOX_ANDROID_CHANNEL=release
# Exact official APK URLs and SHA256 values obtained from those official
# downloads. Never disable TLS verification or fall back to unofficial mirrors.
RUN case "$FIREFOX_ANDROID_CHANNEL" in \
      release) url=https://archive.mozilla.org/pub/fenix/releases/160.0/android/fenix-160.0-android-x86_64/fenix-160.0.multi.android-x86_64.apk; sha256=11e9aaba70dfcccefded85cd67ebd7a6829d0f5dfad04b2f99771c3b5c71a9b7 ;; \
      nightly) url=https://archive.mozilla.org/pub/fenix/nightly/2026/11/2026-11-13-21-17-35-fenix-162.0a1-android-x86_64/fenix-162.0a1.multi.android-x86_64.apk; sha256=63c38a072a25e15f1947f29fb3b1ffd352722ac86478062112b5426428754ebe ;; \
      *) exit 2 ;; \
    esac && curl --fail --location --retry 3 "$url" -o /opt/android/firefox.apk && echo "$sha256  /opt/android/firefox.apk" | sha256sum -c - && printf '%s  /opt/android/firefox.apk\n' "$sha256" > /opt/android/firefox-apk.sha256

# Reuse exactly the desktop Selenium/geckodriver pins and current test source.
FROM ${BROWSER_BASE}
ARG FIREFOX_ANDROID_CHANNEL=release
ENV ANDROID_FIREFOX_CHANNEL=${FIREFOX_ANDROID_CHANNEL} ANDROID_HOME=/opt/android ANDROID_SDK_ROOT=/opt/android ANDROID_AVD_HOME=/tmp/android-avd
ENV PATH=/opt/android/platform-tools:/opt/android/emulator:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends libpulse0 libvulkan1 libxkbcommon0 libxcb-cursor0 && rm -rf /var/lib/apt/lists/* && dpkg-query -W > /app/browser-system-packages.txt
COPY --from=android-tools /opt/android /opt/android
CMD ["python", "/app/tests/browser/android.py"]
