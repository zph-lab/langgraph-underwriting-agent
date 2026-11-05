# Start LangGraph runtime (localhost, single worker).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$port = if ($env:PORT) { $env:PORT } else { 8712 }
$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($conn) { throw "Port $port is already in use. Stop its owner before starting." }
$venvPython = Join-Path $PSScriptRoot ".venv-langgraph/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) { throw "Missing project .venv-langgraph. See README.md for environment setup." }
& $venvPython -m uvicorn backend.main:app --host 127.0.0.1 --port $port
exit $LASTEXITCODE
