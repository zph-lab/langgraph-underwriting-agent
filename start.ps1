param([int]$Port = 8009, [string]$BindHost = '127.0.0.1')
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = Join-Path $PSScriptRoot 'ar_agent开发/agent_1/airong_agent_test1/.venv-langgraph/Scripts/python.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment missing. See README.md.'
}
& $pythonPath (Join-Path $PSScriptRoot 'start.py') --port $Port --host $BindHost
exit $LASTEXITCODE
