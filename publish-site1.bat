@echo off
REM Double-click me: LIVE run on site1 — creates PENDING post(s) on londonheadline.uk.
REM Posts land as 'pending' (review_mode), so review/publish them in wp-admin.
chcp 65001 >nul 2>nul
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=C:\Users\Abdul Rehman\AppData\Local\Python\pythoncore-3.14-64\python.exe"
echo ============================================================
echo  trendpress LIVE on site1  (creates pending posts)
echo ============================================================
"%PY%" main.py --sites site1
echo.
pause
