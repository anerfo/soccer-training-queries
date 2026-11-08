#!/bin/sh
set -u

child=
stopping=0

shutdown() {
    stopping=1
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}

trap shutdown TERM INT

while [ "$stopping" -eq 0 ]; do
    "$@" &
    child=$!
    wait "$child"
    status=$?
    child=
    if [ "$stopping" -eq 1 ]; then
        exit 0
    fi
    if [ "$status" -eq 75 ]; then
        echo "session adopted; restarting wa.py" >&2
        sleep 1
        continue
    fi
    exit "$status"
done
