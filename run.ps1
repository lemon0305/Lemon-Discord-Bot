$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}

$codexPython = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not $python -and (Test-Path $codexPython)) {
    $pythonPath = $codexPython
} elseif ($python) {
    $pythonPath = $python.Source
} else {
    Write-Error "找不到 Python。請先安裝 Python 3.11+，並勾選 Add python.exe to PATH。"
}

& $pythonPath -m pip install -r requirements.txt
& $pythonPath bot.py
