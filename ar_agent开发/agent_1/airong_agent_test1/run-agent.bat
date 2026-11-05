@echo off
setlocal
cd /d "%~dp0"
set "AGENT_PYTHON=%~dp0.venv-langgraph\Scripts\python.exe"
if not exist "%AGENT_PYTHON%" (
    echo Missing project .venv-langgraph. See README.md.
    exit /b 1
)
"%AGENT_PYTHON%" -m langgraph_app.cli %*
exit /b %ERRORLEVEL%
