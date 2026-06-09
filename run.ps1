# trendpress launcher (PowerShell). Examples:
#   .\run.ps1 --dry-run --sites site1   # preview only, never posts
#   .\run.ps1 --sites site1             # live: creates pending post(s)
#   .\run.ps1 --health                  # weekly kill-switch check
Set-Location -Path $PSScriptRoot
$py = "python"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $py = "C:\Users\Abdul Rehman\AppData\Local\Python\pythoncore-3.14-64\python.exe"
}
& $py main.py @args
