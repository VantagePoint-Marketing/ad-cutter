#!/bin/sh
# Launch HyperFrames' pinned headless Chrome with networking cut off.
# - Every host name resolves to "not found" except localhost / 127.0.0.1, where HyperFrames serves the composition.
# - A dead proxy (port 9 on loopback) catches requests to raw IP addresses, including Railway's private network;
#   loopback itself bypasses proxies by default, so the local composition server still works.
# - WebRTC can't open its own UDP paths around the proxy.
# HYPERFRAMES_BROWSER_PATH points here; REAL_CHROME is written at image build time.
. /app/docker/real-chrome.env
exec "$REAL_CHROME" \
  --host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1" \
  --proxy-server="http://127.0.0.1:9" \
  --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
  "$@"
