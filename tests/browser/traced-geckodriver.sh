#!/bin/sh
set -eu
# Trace real geckodriver and all actual Firefox descendants. No added capability
# is requested: inability to trace our own child is a failing prerequisite.
exec strace -D -ff -qq -yy -s 512 -e trace=execve,connect,sendto,sendmsg,sendmmsg -o /artifacts/desktop-network /usr/local/bin/geckodriver "$@"
