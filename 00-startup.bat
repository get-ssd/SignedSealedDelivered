@echo off
echo Starting SSD dev server on http://localhost:8080
cd /d "%~dp0"
py -m http.server 8080
