$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv-app\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Application environment not found. Run the setup commands in README.md first."
}
& $python (Join-Path $PSScriptRoot "app.py")

