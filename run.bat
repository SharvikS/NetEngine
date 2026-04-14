@echo off
echo Starting NetScope...
cd /d "%~dp0"
python main.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Error: Could not start NetScope.
    echo Make sure Python 3.10+ and dependencies are installed:
    echo   pip install -r requirements.txt
    pause
)
