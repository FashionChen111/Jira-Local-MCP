@echo off
setlocal DisableDelayedExpansion
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
%JIRA_PY% %JIRA_PY_ARGS% "%~dp0scripts\install.py"
set "JIRA_EXIT=%ERRORLEVEL%"
set "JIRA_CHOICE="
set /p "JIRA_CHOICE=Press E to update configuration, or Enter to exit: "
if /I "%JIRA_CHOICE%"=="E" (
    %JIRA_PY% %JIRA_PY_ARGS% "%~dp0scripts\install.py" --edit
    pause
)
exit /b %JIRA_EXIT%
