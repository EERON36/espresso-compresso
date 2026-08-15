@echo off
setlocal
title Espresso Compresso
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if errorlevel 1 goto console_python

if "%~1"=="" (
    start "" pythonw.exe "%~dp0espresso_compresso.py"
) else (
    start "" pythonw.exe "%~dp0espresso_compresso.py" "%~1"
)
exit /b 0

:console_python
echo The windowed Python launcher was not found.
echo Starting in a console so any error remains visible.
where py.exe >nul 2>nul
if errorlevel 1 goto plain_python
if "%~1"=="" (
    py -3 "%~dp0espresso_compresso.py"
) else (
    py -3 "%~dp0espresso_compresso.py" "%~1"
)
exit /b %errorlevel%

:plain_python
where python.exe >nul 2>nul
if errorlevel 1 goto no_python
if "%~1"=="" (
    python "%~dp0espresso_compresso.py"
) else (
    python "%~dp0espresso_compresso.py" "%~1"
)
exit /b %errorlevel%

:no_python
echo Python 3 with Tk is required but was not found.
echo Install Python, then try this launcher again.
pause
exit /b 1
