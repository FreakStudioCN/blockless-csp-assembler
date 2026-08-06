param(
  [string]$KiCadMajor = "10"
)

$source = Resolve-Path -LiteralPath "$PSScriptRoot\..\plugin\ai_modular_backplane"
$target = Join-Path $env:APPDATA "kicad\$KiCadMajor.0\scripting\plugins\ai_modular_backplane"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
if (Test-Path -LiteralPath $target) {
  Remove-Item -LiteralPath $target -Recurse -Force
}
Copy-Item -LiteralPath $source -Destination $target -Recurse -Force

Write-Host "Installed AI Modular Backplane plugin to:"
Write-Host $target
Write-Host ""
Write-Host "Restart KiCad PCB Editor, then open Tools -> External Plugins."
