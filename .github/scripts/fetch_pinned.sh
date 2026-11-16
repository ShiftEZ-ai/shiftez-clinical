#!/usr/bin/env bash
set -euo pipefail

# Playwright owns browser revision/cache validation; keep its installer and CDN fallback.
if [ "$1" = --playwright ]; then
  shift
  if [ -n "${RELAY_BASE:-}" ]; then
    if timeout 240s env PLAYWRIGHT_DOWNLOAD_HOST="${RELAY_BASE%/}/cdn.playwright.dev" \
      PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST="${RELAY_BASE%/}/cdn.playwright.dev" \
      PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=15000 "$@"; then
      echo 'Playwright install served by relay (or existing browser cache)' >&2
      exit 0
    fi
    echo 'Playwright relay unavailable; falling back to direct CDN downloads' >&2
    timeout 300s env -u PLAYWRIGHT_DOWNLOAD_HOST -u PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST \
      PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=15000 "$@"
  else
    "$@"
  fi
  exit $?
fi

url=$1
sha=$2
destination=$3
shift 3

verify() { printf '%s  %s\n' "$sha" "$destination" | sha256sum -c -; }

for source in mirror relay direct; do
  case "$source" in
    mirror) base=${BIN_MIRROR_BASE:-} ;;
    relay) base=${RELAY_BASE:-} ;;
    direct) base= ;;
  esac
  if [ "$source" != direct ]; then
    [ -n "$base" ] || continue
    source_url="${base%/}/${url#*://}"
    options=(--connect-timeout 3 --max-time 30)
  else
    source_url=$url
    # The GitHub CDN measured 0.56 MB/s for the 121.5 MB archive; leave margin for the direct fallback.
    options=(--connect-timeout 15 --retry 3 --retry-delay 2 -C - --max-time 600 "$@")
  fi
  if curl -fsSL "${options[@]}" "$source_url" -o "$destination" && verify; then
    echo "Download served by $source: $url" >&2
    exit 0
  fi
  echo "Download failed or checksum mismatch from $source: $url" >&2
done
rm -f -- "$destination"
exit 1
