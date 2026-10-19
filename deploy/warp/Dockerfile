FROM ubuntu:22.04
ARG WARP_VERSION=2026.8.2110.0
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg dbus iptables iproute2 tinyproxy \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --dearmor -o /usr/share/keyrings/cloudflare-warp.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/cloudflare-warp.gpg] https://pkg.cloudflareclient.com/ jammy main' > /etc/apt/sources.list.d/cloudflare-warp.list \
    && apt-get update && apt-get install -y --no-install-recommends cloudflare-warp=${WARP_VERSION} \
    && rm -rf /var/lib/apt/lists/*
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY start.sh /usr/local/bin/start-warp
CMD ["bash", "/usr/local/bin/start-warp"]
