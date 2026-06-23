@echo off
echo ============================================
echo   Multi-Agent Platform - Dev Mode
echo ============================================
echo.

REM Start backend
echo [1/2] Starting backend on port 8000...
start "Backend" cmd /c "cd /d %~dp0 && python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload"

REM Wait for backend
timeout /t 3 /nobreak >nul

REM Start frontend
echo [2/2] Starting frontend on port 3000...
cd /d %~dp0frontend
call npm install
call npm run dev

pause
