@echo off
set EDGE_IP=192.168.1.1
set IMAGE_NAME=opcua2influxdb
set IMAGE_TAG=latest

docker buildx inspect secure-edge-pro >nul 2>&1 || (
    docker buildx create --name secure-edge-pro --config buildkitd-secure-edge-pro.toml
)
docker buildx use secure-edge-pro
docker buildx build --platform linux/arm64/v8 --tag %EDGE_IP%:5000/%IMAGE_NAME%:%IMAGE_TAG% --push .
PAUSE