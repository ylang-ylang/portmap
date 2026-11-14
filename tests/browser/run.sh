#!/bin/sh
# Run only after the integration owner freezes implementation.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
project="portmap-browser-$(date +%s)-$$"
artifacts=
xpi=
mode=
android=0
cleanup_only=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --development|--require-signed) [ -z "$mode" ] || { echo 'Choose one explicit install mode' >&2; exit 2; }; mode=$1; shift ;;
    --xpi) xpi=$2; shift 2 ;;
    --project) project=$2; shift 2 ;;
    --artifacts) artifacts=$2; shift 2 ;;
    --android) android=1; shift ;;
    --cleanup) cleanup_only=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$project" in portmap-browser-[a-z0-9-]*) ;; *) echo 'Project must start portmap-browser- and use lowercase letters/digits/hyphens' >&2; exit 2 ;; esac
case "$project" in *[!a-z0-9-]*) echo 'Invalid project name' >&2; exit 2 ;; esac
if [ -z "$artifacts" ]; then artifacts=$(mktemp -d /tmp/portmap-browser-artifacts.XXXXXX); fi
mkdir -p "$artifacts"
artifacts=$(CDPATH= cd -- "$artifacts" && pwd)
export BROWSER_ARTIFACTS="$artifacts"
export BROWSER_IMAGE="$project-browser:fixture"
export BROWSER_ANDROID_IMAGE="$project-android:fixture"
export BROWSER_UID="$(id -u)" BROWSER_GID="$(id -g)"
export BROWSER_KVM_GID=0
export BROWSER_SOURCE_COMMIT="${BROWSER_SOURCE_COMMIT:-$(git -C "$root" rev-parse HEAD)}"
compose() {
  docker compose --project-name "$project" --file "$root/tests/browser/compose.yaml" --file "$root/tests/browser/compose.android.yaml" "$@"
}
if [ "$cleanup_only" = 1 ]; then
  compose --profile android down --volumes --remove-orphans
  exit
fi
platform=desktop-firefox
if [ "$android" = 1 ]; then platform=android-firefox; fi
# Invalidate any earlier passing envelope before setup or prerequisite checks.
# No unexercised cases are invented; the Python runner replaces this on entry.
printf '{"status":"failed","platform":"%s","extension_sha256":null,"extension_version":null,"browser_version":null,"source_commit":null,"signature_required":false,"cases":[],"error":"Fixture prerequisites or setup did not complete; inspect command output and compose.log"}\n' "$platform" > "$artifacts/result.json"
[ -n "$mode" ] || { echo 'Use explicit --development or --require-signed' >&2; exit 2; }
if [ "$mode" = --require-signed ] && [ -z "$xpi" ]; then echo '--require-signed requires --xpi FILE' >&2; exit 2; fi
if [ "$android" = 1 ]; then
  [ "${ACCEPT_ANDROID_SDK_LICENSE:-0}" = 1 ] || { echo 'Read https://developer.android.com/studio/terms then explicitly set ACCEPT_ANDROID_SDK_LICENSE=1' >&2; exit 2; }
  BROWSER_KVM_GID=$(stat -c %g /dev/kvm)
  export BROWSER_KVM_GID
fi
# Refuse to adopt or remove pre-existing resources under a requested name.
existing=$(docker compose --project-name "$project" --file "$root/tests/browser/compose.yaml" ps --all --quiet)
[ -z "$existing" ] || { echo 'Project already exists; choose another name or explicitly use --cleanup' >&2; exit 2; }
finish() {
  code=$?
  trap - EXIT INT TERM
  compose logs --no-color > "$artifacts/compose.log" 2>&1 || true
  compose --profile android down --volumes --remove-orphans || code=1
  printf '\nProject: %s\nEvidence: %s\n' "$project" "$artifacts"
  exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
set -- "$mode"
if [ -n "$xpi" ]; then
  cp -- "$xpi" "$artifacts/input.xpi"
  set -- "$@" --xpi /artifacts/input.xpi
fi
compose build browser node-a node-b unrelated trap dns
docker image inspect --format '{{.Id}}' "$BROWSER_IMAGE" > "$artifacts/browser-image-id.txt"
if [ "$android" = 1 ]; then
  channel=release
  if [ "$mode" = --development ]; then channel=nightly; fi
  docker build --file "$root/tests/browser/Android.Dockerfile" --build-arg "BROWSER_BASE=$BROWSER_IMAGE" --build-arg ACCEPT_ANDROID_SDK_LICENSE=1 --build-arg "FIREFOX_ANDROID_CHANNEL=$channel" --tag "$BROWSER_ANDROID_IMAGE" "$root"
  docker image inspect --format '{{.Id}}' "$BROWSER_ANDROID_IMAGE" > "$artifacts/android-image-id.txt"
fi
compose up --detach --wait --wait-timeout 90 node-a node-b unrelated trap dns
export BROWSER_DNS_IP="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(compose ps --quiet dns)")"
compose images --format json > "$artifacts/images.json"
if [ "$android" = 1 ]; then
  compose --profile android run --rm --no-deps android python /app/tests/browser/android.py "$@"
else
  compose run --rm --no-deps browser python /app/tests/browser/runner.py "$@"
fi
