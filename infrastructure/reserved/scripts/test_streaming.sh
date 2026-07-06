#!/bin/bash
# Streaming (SSE / chunked) passthrough test through the full production link:
#   client -> Cloudflare -> CloudFront (VPC Origin) -> NLB -> Envoy -> stream tenant
#
# Verifies NO layer buffers the response: chunks must arrive incrementally
# (~1s apart), not all at once at the end. Critical for SSE, chunked transfer,
# and AI/LLM streaming token output.
#
# Prereq: a 'stream' tenant registered (Python SSE server emitting 10 chunks, 1/s,
# with per-chunk flush + X-Accel-Buffering: no). Route in app_routes.
#
# Usage: ./test_streaming.sh [host]
set -u
HOST="${1:-stream.webhost.jaydencrazy.win}"

echo "=== Streaming test: chunk arrival timing over $HOST/sse ==="
echo "PASS if chunks arrive ~1s apart (streamed); FAIL if all land at the end (buffered)."
echo

prev=0
n=0
firstts=0
lastts=0
curl -N -s "https://$HOST/sse" --max-time 15 2>/dev/null | while IFS= read -r line; do
  [ -z "$line" ] && continue
  now=$(date +%s.%N)
  echo "  $(printf '%.3f' "$now")  <-  $line"
  n=$((n+1))
done

echo
echo "Interpretation: if the timestamps above increase by ~1.0s per chunk, the"
echo "full chain streams correctly (no buffering at CloudFront/NLB/Envoy)."
