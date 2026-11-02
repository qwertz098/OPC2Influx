@echo off
rem Defaults. Do not edit them here: put machine-specific values into
rem build_and_push_edge.local.cmd next to this script (ignored by git), e.g.
rem     set EDGE_IP=192.168.140.1
rem so git pull can always update this script.
set EDGE_IP=192.168.1.1
set IMAGE_NAME=opcua2influxdb
set IMAGE_TAG=latest

rem Build from the folder this script lives in, whatever the caller's cwd
cd /d "%~dp0"

if exist "build_and_push_edge.local.cmd" call "build_and_push_edge.local.cmd"

rem Everything runs inside one parenthesised block: cmd parses it completely
rem before running it, so when git pull overwrites this file the current run
rem is unaffected and the new version takes effect on the next start.
(
    echo Fetching the newest version from GitHub...
    git pull --ff-only || (
        echo.
        echo git pull failed - commit/stash local changes or check the network, then retry.
        echo Machine-specific settings belong in build_and_push_edge.local.cmd.
        PAUSE
        exit /b 1
    )

    docker buildx inspect secure-edge-pro >nul 2>&1 || (
        docker buildx create --name secure-edge-pro --config buildkitd-secure-edge-pro.toml
    )
    docker buildx use secure-edge-pro
    docker buildx build --platform linux/arm64/v8 --tag %EDGE_IP%:5000/%IMAGE_NAME%:%IMAGE_TAG% --push .
    PAUSE
    exit /b
)
