@echo off
REM Double-click me: PREVIEW the flow on site1 (writes data\preview\*.html, NEVER posts).
chcp 65001 >nul 2>nul
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=C:\Users\Abdul Rehman\AppData\Local\Python\pythoncore-3.14-64\python.exe"
echo ============================================================
echo  trendpress DRY-RUN on site1  (preview only, no posting)
echo ============================================================
"%PY%" main.py --dry-run --sites site1
echo.
pause
