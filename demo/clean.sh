#!/bin/bash

set -e

DIR="$( cd "$( dirname "$0" )" && pwd )"

PID_FILE="$DIR/servers.pid"

if [ -f $PID_FILE ]; then
    echo "servers.pid exists!"
    exit 1
fi

for port in 8080 8081 8082; do
    rm -rf demo/$port
    rm -rf demo/media_store.$port
done

rm -rf $DIR/etc
