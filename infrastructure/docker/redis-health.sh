#!/bin/sh
set -eu
export REDISCLI_AUTH="$(cat /run/redis/password)"
result=$(redis-cli --no-auth-warning -h 127.0.0.1 ping)
test "$result" = PONG
