#!/bin/sh
set -eu
# Record endpoints/executable identities only, never WebDriver credential payloads.
exec strace -D -ff -qq -yy -s 512 -e trace=execve,connect \
  -o "${PORTMAP_BROWSER_RAW_DIR:?}/desktop-network" /usr/local/bin/geckodriver "$@"
