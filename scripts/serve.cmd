@echo off
rem Double-click to open the FareRecord dashboard
cd /d "%~dp0.."
".venv\Scripts\farerecord.exe" serve --open
pause
