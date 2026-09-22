@echo off
setlocal
cd /d "%~dp0"
title Rockbox Library Manager

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0rockbox_library_manager.py"
) else (
    py -3.13 "%~dp0rockbox_library_manager.py"
)

if errorlevel 1 (
    echo.
    echo Rockbox Library Manager exited with an error.
    echo.
    pause
)
endlocal
