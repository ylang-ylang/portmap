#!/bin/sh
set -eu
# Trace real geckodriver and all actual Firefox descendants. No added capability
# is requested: inability to trace our own child is a failing prerequisite.
# Connection addresses and executable identities suffice; never trace WebDriver
# payloads containing user-entered fixture SSH credentials or private keys.
exec strace -D -ff -qq -yy -s 512 -e trace=execve,connect -o /artifacts/desktop-network /usr/local/bin/geckodriver "$@"
