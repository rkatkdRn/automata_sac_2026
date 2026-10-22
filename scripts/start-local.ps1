$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Create the virtual environment first: py -3.12 -m venv .venv; then install requirements.txt."
    exit 1
}
& $Python (Join-Path $PSScriptRoot "run.py") @args
exit $LASTEXITCODE
