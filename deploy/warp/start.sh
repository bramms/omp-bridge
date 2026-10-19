#!/bin/bash
set -eu
mkdir -p /run/dbus
rm -f /run/dbus/pid
dbus-daemon --system
warp-svc &
warp_pid=$!
proxy_pid=
trap 'kill "$warp_pid" ${proxy_pid:-} 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' TERM INT
for attempt in {1..30}; do
    warp-cli --accept-tos status >/dev/null 2>&1 && break
    sleep 1
done

# Only return traffic and local DNS may use IPv4. New upstream connections
# must use IPv6 through WARP; a disconnected tunnel cannot fall back to direct.
iptables -A OUTPUT -m owner --uid-owner tinyproxy -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner tinyproxy -o lo -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner tinyproxy -j REJECT
ip6tables -A OUTPUT -m owner --uid-owner tinyproxy -o lo -j ACCEPT
ip6tables -A OUTPUT -m owner --uid-owner tinyproxy -o CloudflareWARP -j ACCEPT
ip6tables -A OUTPUT -m owner --uid-owner tinyproxy -j REJECT

if warp-cli --accept-tos registration show >/dev/null 2>&1; then
    warp-cli --accept-tos mode warp
    warp-cli --accept-tos connect
else
    echo 'No registration. Register this node explicitly once; see docs/ПОСІБНИК.md.'
fi
tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf &
proxy_pid=$!
wait -n "$warp_pid" "$proxy_pid"
exit 1
