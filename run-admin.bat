@echo off
REM Re-launches Net Engine as Administrator. Required to apply
REM network adapter (IP / DNS) configuration changes.
cd /d "%~dp0"
powershell -Command "Start-Process -Verb RunAs cmd.exe '/k cd /d %~dp0 && python main.py'"
