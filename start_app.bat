@echo off
cd /d "%~dp0"

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
