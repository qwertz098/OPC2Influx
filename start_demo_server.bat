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
echo   OPC UA Demo Server — opc.tcp://localhost:48400
echo  =====================================================
echo.
echo  Nodes:
echo    ProductionLine_0 / _1 / _2
echo      Motor_Drive, Motor_Feed, Conveyor (nested objects)
echo    HeatExchanger_0 / _1
echo    Tank_0 / _1 / _2
echo    PowerMonitor
echo.
echo  Values update every 0.5 s with sine-wave dynamics.
echo  Add this server in the web UI:
echo    Endpoint: opc.tcp://localhost:48400
echo.
echo  Press Ctrl+C to stop.
echo.
python "%~dp0demo_server.py"
pause
