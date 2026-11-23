@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
if defined PURRAI_PYTHON goto configured
if exist "D:\Tools\venvs\PurrAI\Scripts\python.exe" set "PURRAI_PYTHON=D:\Tools\venvs\PurrAI\Scripts\python.exe"
if defined PURRAI_PYTHON goto configured
if exist ".venv\Scripts\python.exe" set "PURRAI_PYTHON=%CD%\.venv\Scripts\python.exe"
if defined PURRAI_PYTHON goto configured
echo Python environment not found. Set PURRAI_PYTHON to the project's Python executable.
popd
pause
exit /b 1
:configured
"%PURRAI_PYTHON%" -u scripts\start_local.py %*
set "PURRAI_EXIT=%ERRORLEVEL%"
popd
if not "%PURRAI_EXIT%"=="0" if "%~1"=="" pause
exit /b %PURRAI_EXIT%
