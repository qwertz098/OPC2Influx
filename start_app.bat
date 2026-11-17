@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  GitHub sync: fetch the latest version before starting.
rem  Skipped with:  start_app.bat noupdate   (or set NO_UPDATE=1)
rem  Only fast-forward updates; local changes are never overwritten.
rem ---------------------------------------------------------------
rem  cmd reads a batch file while running it, so everything around
rem  "git pull" (which may replace this file) sits in ( ) blocks that
rem  are read completely before they run.
if /i "%~1"=="noupdate" set NO_UPDATE=1
if not "%NO_UPDATE%"=="1" (
    call :sync
    if defined RESTART (
        "%~f0" noupdate
        exit /b
    )
)

if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing / updating dependencies...
pip install -r requirements.txt -q

echo.
echo  =====================================================
echo   OpcUA-2-InfluxDB  ^|  http://localhost:8000
echo  =====================================================
echo.
echo  Press Ctrl+C to stop.
echo.

uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
exit /b


:sync
where git >nul 2>&1
if errorlevel 1 (
    echo [Update] Git is not installed - starting without update.
    exit /b
)
if not exist ".git" (
    echo [Update] Not a Git clone - starting without update.
    exit /b
)
echo [Update] Checking GitHub for a new version...
git fetch --quiet origin 2>nul
if errorlevel 1 (
    echo [Update] GitHub not reachable - starting the current version.
    exit /b
)
set BEHIND=0
for /f %%c in ('git rev-list --count HEAD..@{u} 2^>nul') do set BEHIND=%%c
if "%BEHIND%"=="0" (
    echo [Update] Up to date.
    exit /b
)
rem Local changes to tracked files? (git diff: exit code 1 = changes)
set DIRTY=
git diff --quiet 2>nul || set DIRTY=1
git diff --cached --quiet 2>nul || set DIRTY=1
if defined DIRTY (
    echo [Update] %BEHIND% new commit^(s^) on GitHub, but these files were changed here:
    git --no-pager diff --name-only HEAD
    echo [Update] Not updated - starting the current version.
    echo [Update] To take the GitHub version, discard the changes:  git checkout -- ^<file^>
    exit /b
)
echo [Update] %BEHIND% new commit^(s^):
git --no-pager log --oneline HEAD..@{u}
(
    git pull --ff-only --quiet
    if errorlevel 1 (
        echo [Update] Update failed - starting the current version.
        echo [Update] Run "git status" in this folder to see why.
        exit /b
    )
    echo [Update] Updated - restarting with the new version.
    set RESTART=1
    exit /b
)
