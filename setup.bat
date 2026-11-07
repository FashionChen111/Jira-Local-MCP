@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        set "JIRA_PY=%~dp0.venv\Scripts\python.exe"
        set "JIRA_PY_ARGS="
        goto run
    )
)
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        set "JIRA_PY=py"
        set "JIRA_PY_ARGS=-3"
        goto run
    )
)
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        set "JIRA_PY=python"
        set "JIRA_PY_ARGS="
        goto run
    )
)
echo Python 3.10 or newer is required. Install it and run setup again.
pause
exit /b 1

:run
"%JIRA_PY%" %JIRA_PY_ARGS% "%~dp0scripts\install.py"
set "JIRA_EXIT=%ERRORLEVEL%"
set "JIRA_CHOICE="
set /p "JIRA_CHOICE=Press E to update configuration, or Enter to exit: "
if /I "%JIRA_CHOICE%"=="E" call :edit
exit /b %JIRA_EXIT%

:edit
"%JIRA_PY%" %JIRA_PY_ARGS% "%~dp0scripts\install.py" --edit
if "%JIRA_EXIT%"=="0" set "JIRA_EXIT=%ERRORLEVEL%"
pause
exit /b
