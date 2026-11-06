@echo off
cd /d "%~dp0"
call npm ci
exit /b %errorlevel%
