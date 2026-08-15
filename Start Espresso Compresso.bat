@echo off
setlocal
title Espresso Compresso
cd /d "%~dp0"

where pyw.exe >nul 2>nul
if errorlevel 1 goto pythonw

if "%~1"=="" (
    start "" pyw.exe -3 "%~dp0espresso_compresso.py"
) else (
    start "" pyw.exe -3 "%~dp0espresso_compresso.py" "%~1"
)
exit /b 0

:pythonw
where pythonw.exe >nul 2>nul
if errorlevel 1 goto console_python

if "%~1"=="" (
    start "" pythonw.exe "%~dp0espresso_compresso.py"
) else (
    start "" pythonw.exe "%~dp0espresso_compresso.py" "%~1"
)
exit /b 0

:console_python
echo Python 3 with Tk is required. Trying the console launcher for diagnostics.
if "%~1"=="" (
    py -3 "%~dp0espresso_compresso.py"
) else (
    py -3 "%~dp0espresso_compresso.py" "%~1"
)
