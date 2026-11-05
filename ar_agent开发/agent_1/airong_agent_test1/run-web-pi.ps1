# Start the airong rule-assistant web server (FastAPI + pi RPC bridge).
# Usage: powershell -File run-web-pi.ps1   (default http://127.0.0.1:8712)
# NOTE: keep this file ASCII-only; Windows PowerShell 5.1 reads BOM-less
# UTF-8 .ps1 files as GBK and corrupts non-ASCII string literals.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$projectDir = $PSScriptRoot
if (-not $env:PI_CODING_AGENT_DIR) {
    $projectModels = Join-Path $projectDir ".pi/models.json"
    if (-not (Test-Path -LiteralPath $projectModels)) {
        throw "Missing project .pi/models.json"
    }
    # Keep local runtime credentials and trust isolated from global Pi settings.
    $runtimeDir = Join-Path $projectDir "config/pi-agent"
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    Copy-Item -LiteralPath $projectModels -Destination (Join-Path $runtimeDir "models.json") -Force
    $trustPath = Join-Path $runtimeDir "trust.json"
    $trust = if (Test-Path -LiteralPath $trustPath) {
        Get-Content -LiteralPath $trustPath -Raw | ConvertFrom-Json
    } else { [PSCustomObject]@{} }
    $trust | Add-Member -NotePropertyName $projectDir -NotePropertyValue $true -Force
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($trustPath, ($trust | ConvertTo-Json), $utf8)
    $env:PI_CODING_AGENT_DIR = $runtimeDir
}
if (-not $env:PI_EXTRA_ARGS) {
    $settings = Get-Content -LiteralPath (Join-Path $projectDir ".pi/settings.json") -Raw | ConvertFrom-Json
    if (-not $settings.defaultProvider -or -not $settings.defaultModel) {
        throw "Missing defaultProvider/defaultModel in project settings"
    }
    $env:PI_EXTRA_ARGS = "--provider $($settings.defaultProvider) --model $($settings.defaultModel)"
}

$port = if ($env:PORT) { $env:PORT } else { 8712 }
$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    throw "Port $port is already in use. Stop its owner before starting."
}

$venvPython = Join-Path $PSScriptRoot ".venv-langgraph/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) { throw "Missing project .venv-langgraph. See README.md." }
& $venvPython -m uvicorn legacy.pi.main:app --host 127.0.0.1 --port $port
exit $LASTEXITCODE
        
