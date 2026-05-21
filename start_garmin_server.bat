@echo off
echo Starting HYROX Garmin Proxy Server...
echo.

REM Install dependencies if needed
pip install -r garmin_server_requirements.txt --quiet --break-system-packages 2>nul || pip install -r garmin_server_requirements.txt --quiet

echo.
python garmin_server.py
pause
