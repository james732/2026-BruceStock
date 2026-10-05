@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if not exist "logs" mkdir "logs"
if not exist "logs" exit /b 1
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "UV_EXE=C:\Users\3\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
if not exist "%UV_EXE%" set "UV_EXE=uv"
call :run >> "logs\report.log" 2>&1
set "RESULT=%ERRORLEVEL%"
exit /b %RESULT%

:run
echo [%DATE% %TIME%] Starting report generation.
"%UV_EXE%" run main.py
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR: Report generation failed. Email skipped.
    exit /b 1
)
if not exist "analysis.html" (
    echo [%DATE% %TIME%] ERROR: analysis.html is missing. Email skipped.
    exit /b 1
)
"%UV_EXE%" run mail_report.py --report analysis.html --recipient Bruce1_Chen@asus.com --non-interactive
if errorlevel 1 (
    echo [%DATE% %TIME%] ERROR: Email delivery failed.
    exit /b 1
)
echo [%DATE% %TIME%] Report sent successfully.
exit /b 0
