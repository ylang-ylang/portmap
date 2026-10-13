#!/bin/sh
# portmap installer.
#
# Install the standalone portmap command from a portmap catalog server:
#   curl -fsSL <origin>/install.sh | sh -s -- <origin>
#
# Requirements: POSIX sh, curl, tar, awk, grep, readlink and a sha256 tool
# (sha256sum or shasum). No sudo, Python, jq, Brew, or autostart. Installs a
# versioned bundle under ${XDG_DATA_HOME:-$HOME/.local/share}/portmap and
# manages an atomic ~/.local/bin/portmap symlink. Installing only places the
# command: a Docker or Podman runtime with a compatible Docker Compose CLI
# must be present before `portmap setup`. Installing does NOT configure DNS
# or take over port 80. Setup prepares split DNS and a persistent
# 127.0.0.1:80 entry without starting business projects, LAN publishing or
# boot services.

set -eu

PROGRAM=portmap
MANIFEST_PATH=/api/downloads
INSTALL_ROOT=${XDG_DATA_HOME:-$HOME/.local/share}/portmap
BIN_DIR=$HOME/.local/bin
BIN_LINK=$BIN_DIR/$PROGRAM
MARKER=.portmap-install

# Legacy standalone-client installs owned by the previous installer. Old
# bundles are deliberately RETAINED: running portmap-client workers live in
# them until the unified `portmap setup` migration stops them safely. Only a
# launcher that positively points into the legacy bundle root is removed.
LEGACY_PROGRAM=portmap-client
LEGACY_INSTALL_ROOT=${XDG_DATA_HOME:-$HOME/.local/share}/portmap-client
LEGACY_BIN_LINK=$BIN_DIR/$LEGACY_PROGRAM
LEGACY_MARKER=.portmap-client-install

note() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

ARCHIVE_TMP=
STAGE_DIR=
cleanup() {
    [ -n "$ARCHIVE_TMP" ] && rm -f "$ARCHIVE_TMP" 2>/dev/null || :
    [ -n "$STAGE_DIR" ] && rm -rf "$STAGE_DIR" 2>/dev/null || :
    return 0
}
trap cleanup EXIT

# --- origin argument -------------------------------------------------------
[ "$#" -eq 1 ] || die "usage: install.sh <origin>  (e.g. https://catalog.example.com)"
ORIGIN=$1
case $ORIGIN in
    http://*|https://*) ;;
    *) die "origin must start with http:// or https:// (got: $ORIGIN)" ;;
esac
case $ORIGIN in
    *@*) die "origin must not contain credentials" ;;
esac
ORIGIN=${ORIGIN%/}
case $ORIGIN in
    http://|https://) die "origin is missing a host" ;;
esac

# --- required tools --------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
need curl
need tar
need awk
need grep
need readlink
if command -v sha256sum >/dev/null 2>&1; then
    sha256_of() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }
else
    die "missing sha256 tool: need sha256sum or shasum"
fi

# --- platform detection ----------------------------------------------------
case "$(uname -s)" in
    Linux) CLIENT_OS=linux ;;
    Darwin) CLIENT_OS=darwin ;;
    *) die "unsupported operating system: $(uname -s) (supported: linux, darwin)" ;;
esac
case "$(uname -m)" in
    x86_64) CLIENT_ARCH=amd64 ;;
    aarch64|arm64) CLIENT_ARCH=arm64 ;;
    *) die "unsupported architecture: $(uname -m) (supported: amd64, arm64)" ;;
esac

# --- linux libc baseline ---------------------------------------------------
# Current Linux builds target glibc (Debian 11 baseline); fail fast and
# clearly on musl/Alpine or older glibc instead of installing a binary
# that cannot run. getconf is POSIX; no Python/jq needed.
if [ "$CLIENT_OS" = linux ]; then
    MIN_GLIBC=2.31
    libc_version=$(getconf GNU_LIBC_VERSION 2>/dev/null) || libc_version=""
    case $libc_version in
        glibc\ [0-9]*) ;;
        *) die "this build requires glibc $MIN_GLIBC or newer on Linux (musl/Alpine and non-glibc systems are not supported)" ;;
    esac
    libc_version=${libc_version#glibc }
    libc_ok=$(printf '%s\n' "$libc_version" | awk -v want="$MIN_GLIBC" '
        {
            n = split($1, have, ".")
            m = split(want, need, ".")
            k = (n > m) ? n : m
            for (i = 1; i <= k; i++) {
                a = (i <= n) ? have[i] + 0 : 0
                b = (i <= m) ? need[i] + 0 : 0
                if (a != b) { print (a > b) ? "ok" : "no"; exit }
            }
            print "ok"
        }
    ')
    [ "$libc_ok" = ok ] || die "system glibc $libc_version is older than the required $MIN_GLIBC; this build needs glibc $MIN_GLIBC+"
fi

# --- manifest --------------------------------------------------------------
note "Fetching portmap release manifest from $ORIGIN$MANIFEST_PATH"
manifest=$(curl -fsSL "$ORIGIN$MANIFEST_PATH") || die "failed to fetch $ORIGIN$MANIFEST_PATH"
case $manifest in
    *"available"*true*) ;;
    *) die "server reports portmap downloads as unavailable" ;;
esac

version=$(printf '%s\n' "$manifest" | awk '
    match($0, /"version": "[0-9.]+"/) {
        print substr($0, RSTART + 12, RLENGTH - 13)
        exit
    }
')
[ -n "$version" ] || die "manifest did not include a version"
case $version in
    *[!0-9.]*) die "manifest version is malformed: $version" ;;
esac

entry=$(printf '%s\n' "$manifest" | awk -v os="\"os\": \"$CLIENT_OS\"" -v arch="\"arch\": \"$CLIENT_ARCH\"" '
    BEGIN { RS = "}" }
    index($0, os) > 0 && index($0, arch) > 0 {
        if (match($0, /"filename": "[^"]+"/)) {
            filename = substr($0, RSTART + 13, RLENGTH - 14)
        }
        if (match($0, /"sha256": "[^"]+"/)) {
            sha = substr($0, RSTART + 11, RLENGTH - 12)
        }
        if (match($0, /"size": [0-9]+/)) {
            size = substr($0, RSTART + 8, RLENGTH - 8)
        }
        if (match($0, /"source_url": "[^"]+"/)) {
            source_url = substr($0, RSTART + 15, RLENGTH - 16)
        }
        printf "%s %s %s %s\n", filename, sha, size, source_url
        exit
    }
')
set -- $entry
filename=${1:-}
expected_sha=${2:-}
expected_size=${3:-}
source_url=${4:-}
[ -n "$filename" ] || die "no download published for $CLIENT_OS/$CLIENT_ARCH"
case $filename in
    "$PROGRAM-$version-"*.tar.gz) ;;
    *) die "manifest filename is malformed: $filename" ;;
esac
case $filename in
    */*|*..*) die "manifest filename must not contain path parts: $filename" ;;
esac
case $expected_sha in
    ''|*[!0-9a-f]*) die "manifest checksum is malformed for $filename" ;;
esac
[ "${#expected_sha}" -eq 64 ] || die "manifest checksum is malformed for $filename"
case $expected_size in
    ''|*[!0-9]*) die "manifest size is malformed for $filename" ;;
esac

case $source_url in
    https://github.com/ylang-ylang/portmap/releases/download/*"$filename") ;;
    *) die "manifest did not pin a trusted source URL for $filename" ;;
esac

# --- refuse to clobber an unrelated launcher -------------------------------
if [ -e "$BIN_LINK" ] || [ -L "$BIN_LINK" ]; then
    if [ -L "$BIN_LINK" ]; then
        current=$(readlink "$BIN_LINK" 2>/dev/null || printf '%s' "")
        case $current in
            "$INSTALL_ROOT"/*) ;;
            *) die "refusing to replace unrelated $BIN_LINK (points to $current)" ;;
        esac
    else
        die "refusing to replace unrelated file: $BIN_LINK"
    fi
fi

# --- download and verify ---------------------------------------------------
note "Downloading $filename ($expected_size bytes)"
ARCHIVE_TMP=$(mktemp "${TMPDIR:-/tmp}/portmap.XXXXXX")
# The installer's stdin is a pipe; progress belongs on terminal stderr.
curl_progress=--silent
if [ -t 2 ]; then
    curl_progress=--no-silent
fi
curl -fSL "$curl_progress" "$ORIGIN/downloads/portmap/$filename" -o "$ARCHIVE_TMP" \
    || die "failed to download $ORIGIN/downloads/portmap/$filename"

note "Verifying download..."
actual_size=$(wc -c < "$ARCHIVE_TMP" | tr -d ' ')
[ "$actual_size" = "$expected_size" ] \
    || die "downloaded size mismatch: expected $expected_size, got $actual_size"
actual_sha=$(sha256_of "$ARCHIVE_TMP")
[ "$actual_sha" = "$expected_sha" ] \
    || die "checksum mismatch for $filename: expected $expected_sha, got $actual_sha"

if tar -tzf "$ARCHIVE_TMP" 2>/dev/null | grep -Eq '(^|/)\.\.(/|$)|^/'; then
    die "archive $filename contains unsafe paths"
fi

# The published bundles contain regular files and directories only; refuse
# symlink/hardlink/device/fifo/socket members before extracting anything.
if tar -tvzf "$ARCHIVE_TMP" 2>/dev/null | awk '$1 ~ /^[lhcpbs]/ { bad = 1 } END { exit bad ? 0 : 1 }'; then
    die "archive $filename contains non-regular members (links, devices or fifos)"
fi

# --- stage an immutable bundle, then activate its launcher atomically -------
mkdir -p "$INSTALL_ROOT"
STAGE_DIR=$(mktemp -d "$INSTALL_ROOT/$version.XXXXXX")
note "Extracting portmap..."
if ! tar -xzf "$ARCHIVE_TMP" -C "$STAGE_DIR"; then
    rm -rf "$STAGE_DIR"; STAGE_DIR=
    die "failed to extract $filename"
fi
# Layout guard: the launcher expects the executable at the archive root.
# A wrong-layout archive must fail here, BEFORE the existing install or
# launcher link is touched.
if [ ! -f "$STAGE_DIR/$PROGRAM" ]; then
    rm -rf "$STAGE_DIR"; STAGE_DIR=
    die "archive $filename has an unexpected layout (missing ./$PROGRAM at the archive root)"
fi
if [ ! -x "$STAGE_DIR/$PROGRAM" ]; then
    rm -rf "$STAGE_DIR"; STAGE_DIR=
    die "archive $filename does not ship an executable ./$PROGRAM"
fi
# Self-checksums cannot be embedded in the archive. Commit the verified release
# metadata in the same staged directory, before changing the current launcher.
if ! {
    printf '{\n  "version": "%s",\n  "assets": [\n    {\n' "$version"
    printf '      "os": "%s",\n      "arch": "%s",\n' "$CLIENT_OS" "$CLIENT_ARCH"
    printf '      "filename": "%s",\n' "$filename"
    printf '      "sha256": "%s",\n' "$expected_sha"
    printf '      "size": %s,\n' "$expected_size"
    printf '      "source_url": "%s"\n' "$source_url"
    printf '    }\n  ]\n}\n'
} > "$STAGE_DIR/release.json"; then
    die "failed to stage the verified release manifest"
fi
printf 'installed by install.sh\n' > "$STAGE_DIR/$MARKER"
rm -f "$ARCHIVE_TMP"; ARCHIVE_TMP=
# Running workers and pending migrations can still reference any older bundle,
# including one with the same version. Never replace or delete those paths.
target=$STAGE_DIR
STAGE_DIR=

# --- managed symlink -------------------------------------------------------
mkdir -p "$BIN_DIR"
link_tmp=$BIN_DIR/.$PROGRAM.link.$$
ln -s "$target/$PROGRAM" "$link_tmp"
if ! mv -f "$link_tmp" "$BIN_LINK"; then
    rm -f "$link_tmp"
    die "failed to update $BIN_LINK"
fi


# --- retire the legacy standalone-client launcher ---------------------------
# Only remove a launcher this toolchain provably owns: a symlink into the
# legacy portmap-client bundle root. Anything else stays untouched, and the
# old bundles always remain for the runtime state migration.
legacy_note=
if [ -e "$LEGACY_BIN_LINK" ] || [ -L "$LEGACY_BIN_LINK" ]; then
    if [ -L "$LEGACY_BIN_LINK" ]; then
        legacy_target=$(readlink "$LEGACY_BIN_LINK" 2>/dev/null || printf '%s' "")
        case $legacy_target in
            "$LEGACY_INSTALL_ROOT"/*"/$LEGACY_PROGRAM")
                legacy_relative=${legacy_target#"$LEGACY_INSTALL_ROOT"/}
                legacy_version=${legacy_relative%/"$LEGACY_PROGRAM"}
                legacy_bundle=$LEGACY_INSTALL_ROOT/$legacy_version
                if printf '%s\n' "$legacy_version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
                    && [ ! -L "$legacy_bundle" ] \
                    && [ -f "$legacy_bundle/$LEGACY_PROGRAM" ] \
                    && [ ! -L "$legacy_bundle/$LEGACY_PROGRAM" ] \
                    && [ -f "$legacy_bundle/$LEGACY_MARKER" ] \
                    && [ ! -L "$legacy_bundle/$LEGACY_MARKER" ] \
                    && grep -qx 'installed by install-client.sh' "$legacy_bundle/$LEGACY_MARKER"; then
                    if rm -f "$LEGACY_BIN_LINK"; then
                        legacy_note="removed legacy launcher $LEGACY_BIN_LINK"
                    else
                        legacy_note="could not remove owned legacy launcher $LEGACY_BIN_LINK; remove it manually"
                    fi
                else
                    legacy_note="left unverified $LEGACY_BIN_LINK in place"
                fi
                ;;
            *)
                legacy_note="left unrelated $LEGACY_BIN_LINK in place (points to $legacy_target)"
                ;;
        esac
    else
        legacy_note="left unrelated $LEGACY_BIN_LINK in place (not installed by portmap)"
    fi
fi

note "Installed $PROGRAM $version"
note "  bundle: $target"
note "  launcher: $BIN_LINK"
note "  release manifest: $target/release.json"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) note "note: $BIN_DIR is not on your PATH; add it with:"
       note "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
note "Next steps:"
note "  $PROGRAM --version"
note "  $PROGRAM setup            # split DNS + persistent 127.0.0.1:80 (asks for sudo)"
note "  $PROGRAM connect <target> # direct to a remote IP, or through SSH"
note "  $PROGRAM up               # optional: publish local Docker/Podman projects"
note "Installing placed the command only; Docker or Podman with a compatible"
note "Docker Compose CLI is required before setup."
note "Installing did not configure DNS or claim port 80."
note "Setup prepares split DNS and keeps 127.0.0.1:80 ready even with no connections, starting no business projects, LAN publishing or boot services. Direct traffic still goes straight to the remote server."
note "Run \`$PROGRAM up\` separately to enable local publishing; \`$PROGRAM teardown\` releases the infrastructure portmap owns."
note "Run \`$PROGRAM status\` any time to see local publishing and access state."
