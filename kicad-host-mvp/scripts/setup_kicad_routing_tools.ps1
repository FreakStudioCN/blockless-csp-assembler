param(
  [string]$PythonPath = "C:\Program Files\KiCad\10.0\bin\python.exe"
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$tools = Join-Path $repo "tools\KiCadRoutingTools-0.19.3"
$deps = Join-Path $repo "tools\kicad-python-deps"
$requirements = Join-Path $tools "requirements.txt"
$builder = Join-Path $tools "build_router.py"

if (-not (Test-Path -LiteralPath $PythonPath)) {
  throw "KiCad Python not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $requirements)) {
  throw "KiCadRoutingTools requirements not found: $requirements"
}

New-Item -ItemType Directory -Force -Path $deps | Out-Null

Write-Host "[1/2] Install Python dependencies into the ignored local tools directory"
& $PythonPath -m pip install --upgrade --target $deps -r $requirements
if ($LASTEXITCODE -ne 0) {
  throw "Failed to install KiCadRoutingTools Python dependencies."
}

Write-Host "[2/2] Install or build the Rust routing extension"
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$deps;$tools;$(Join-Path $tools 'rust_router')"
try {
  & $PythonPath $builder
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the KiCadRoutingTools Rust extension."
  }
} finally {
  $env:PYTHONPATH = $previousPythonPath
}

Write-Host "KiCadRoutingTools is ready. Run npm run kicad:ai-route."
