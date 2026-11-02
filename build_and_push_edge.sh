#!/bin/bash
set -e -x

EDGE_IP="${EDGE_IP:-192.168.140.1}"
IMAGE_NAME="${IMAGE_NAME:-opcua2influxdb}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# One builder per gateway IP: buildkit only reads its registry config when a
# builder is created, so a new IP gets a fresh builder that allows plain HTTP.
BUILDER="secure-edge-pro-${EDGE_IP//./-}"
BUILDKIT_TOML="${XDG_CACHE_HOME:-$HOME/.cache}/opc2influx/buildkitd-${BUILDER}.toml"

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  mkdir -p "$(dirname "$BUILDKIT_TOML")"
  printf '[registry."%s:5000"]\n  http = true\n' "$EDGE_IP" > "$BUILDKIT_TOML"
  docker buildx create --name "$BUILDER" --config "$BUILDKIT_TOML" --bootstrap
fi

docker buildx use "$BUILDER"

# Release shown on the settings page, e.g. v0.1.4 or v0.1.4-3-gabc1234
APP_RELEASE="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"

docker buildx build --platform linux/arm64/v8 \
                    --build-arg APP_RELEASE="${APP_RELEASE}" \
                    --tag ${EDGE_IP}:5000/${IMAGE_NAME}:${IMAGE_TAG} \
                    --push .
