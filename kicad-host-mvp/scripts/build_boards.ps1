$ErrorActionPreference = "Stop"
$python = "C:\Program Files\KiCad\10.0\bin\python.exe"
$script = Join-Path $PSScriptRoot "build_boards_with_kicad_python.py"

if (-not (Test-Path -LiteralPath $python)) {
  throw "KiCad Python not found: $python"
}

& $python $script
exit $LASTEXITCODE
