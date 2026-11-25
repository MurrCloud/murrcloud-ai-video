#!/bin/sh
set -eu
umask 077
: "${REDIS_PASSWORD:?A Redis password is required}"
if [ "${#REDIS_PASSWORD}" -lt 32 ]; then
    printf '%s\n' 'Redis password must contain at least 32 characters.' >&2
    exit 1
fi
password_hash=$(printf '%s' "$REDIS_PASSWORD" | sha256sum)
password_hash=${password_hash%% *}
printf 'user default on #%s ~* &* +@all\n' "$password_hash" > /run/redis/users.acl
printf '%s' "$REDIS_PASSWORD" > /run/redis/password
cat > /run/redis/redis.conf <<'EOF'
bind 0.0.0.0
protected-mode yes
port 6379
daemonize no
dir /data
aclfile /run/redis/users.acl
appendonly yes
appendfsync everysec
save ""
maxmemory 64mb
maxmemory-policy noeviction
timeout 300
tcp-keepalive 60
loglevel warning
EOF
unset REDIS_PASSWORD password_hash
exec redis-server /run/redis/redis.conf
