@echo off
REM ===========================================================================
REM  trendpress launcher (Windows CMD).  Pass any flags straight through:
REM     run.bat --dry-run --sites site1     preview only, never posts
REM     run.bat --sites site1               live: creates pending post(s)
REM     run.bat --health                    weekly kill-switch check
REM     run.bat                             live run on all active sites
REM ===========================================================================
chcp 65001 >nul 2>nul
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=C:\Users\Abdul Rehman\AppData\Local\Python\pythoncore-3.14-64\python.exe"
"%PY%" main.py %*
