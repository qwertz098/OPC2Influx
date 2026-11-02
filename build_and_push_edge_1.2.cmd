@echo off
rem Builds release v0.1.2 again and pushes it as a separate image
rem (opcua2influxdb12), e.g. to run it side by side with the current version.
rem The source comes from the git tag via a temporary worktree, so this
rem folder's checkout is not touched.
rem Machine-specific values (EDGE_IP) are read from build_and_push_edge.local.cmd.
set EDGE_IP=192.168.1.1
set RELEASE_TAG=v0.1.2
set IMAGE_NAME=opcua2influxdb12
set IMAGE_TAG=latest

cd /d "%~dp0"

if exist "build_and_push_edge.local.cmd" call "build_and_push_edge.local.cmd"

set BUILDER=secure-edge-pro-%EDGE_IP:.=-%
set BUILDKIT_TOML=%TEMP%\buildkitd-%BUILDER%.toml
set WORKTREE=%TEMP%\opc2influx-%RELEASE_TAG%

echo Fetching release tags from GitHub...
git fetch --tags --force origin || (
    echo git fetch failed - check the network, then retry.
    PAUSE
    exit /b 1
)

rem Fresh worktree of the release tag
if exist "%WORKTREE%" git worktree remove --force "%WORKTREE%"
git worktree prune
git worktree add --detach "%WORKTREE%" %RELEASE_TAG% || (
    echo Could not check out %RELEASE_TAG%.
    PAUSE
    exit /b 1
)

docker buildx inspect %BUILDER% >nul 2>&1 || (
    (
        echo [registry."%EDGE_IP%:5000"]
        echo   http = true
    ) > "%BUILDKIT_TOML%"
    docker buildx create --name %BUILDER% --config "%BUILDKIT_TOML%" --bootstrap
)
docker buildx use %BUILDER%
docker buildx build --platform linux/arm64/v8 --tag %EDGE_IP%:5000/%IMAGE_NAME%:%IMAGE_TAG% --push "%WORKTREE%"
set BUILD_RESULT=%ERRORLEVEL%

git worktree remove --force "%WORKTREE%"

echo.
if "%BUILD_RESULT%"=="0" (
    echo Pushed %EDGE_IP%:5000/%IMAGE_NAME%:%IMAGE_TAG% from %RELEASE_TAG%
) else (
    echo Build failed - see the messages above.
)
PAUSE
exit /b %BUILD_RESULT%
