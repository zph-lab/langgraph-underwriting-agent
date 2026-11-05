@echo off
setlocal
cd /d "%~dp0"
set "AGENT_PYTHON=%~dp0.venv-langgraph\Scripts\python.exe"
if not exist "%AGENT_PYTHON%" (
    echo Missing project .venv-langgraph. See README.md. 1>&2
    exit /b 1
)
"%AGENT_PYTHON%" -m mcp_service.server %*
exit /b %ERRORLEVEL%
