#Requires -Version 5.1
<#
.SYNOPSIS
Creates the private Python environment for photo triage (triage\.venv).

.DESCRIPTION
Needs Python 3.10 or newer installed first, for example:
    winget install Python.Python.3.12

Installs PyTorch (CPU build), OpenCLIP, Pillow and pillow-heif into triage\.venv only;
nothing is installed system-wide. About 1-2 GB of downloads.

.EXAMPLE
    .\triage\Setup-Triage.ps1
    .\triage\Setup-Triage.ps1 -Python "C:\Python312\python.exe"
#>
param(
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$venv = Join-Path $here '.venv'

if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '3.14', '3.13', '3.12', '3.11', '3.10') {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { $Python = (& py "-$v" -c "import sys; print(sys.executable)").Trim(); break }
        }
    }
}
if (-not $Python) {
    throw 'Python 3.10 or newer not found. Install it with:  winget install Python.Python.3.12   then run this again (or pass -Python <path to python.exe>).'
}

Write-Host "Using $Python"
& $Python -m venv $venv
$py = Join-Path $venv 'Scripts\python.exe'
& $py -m pip install --upgrade pip
& $py -m pip install -r (Join-Path $here 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Host ''
Write-Host 'Ready. Try it on a sample first:' -ForegroundColor Green
Write-Host "    & '$py' '$(Join-Path $here 'triage.py')' --sample 300"
Write-Host 'Then the whole library:'
Write-Host "    & '$py' '$(Join-Path $here 'triage.py')'"
