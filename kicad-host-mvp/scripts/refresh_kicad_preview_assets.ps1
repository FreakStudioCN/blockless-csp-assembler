param(
  [string[]]$Case = @("long-range-env-node", "desktop-air-monitor")
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$root = Join-Path $repo "kicad-host-mvp"
$python = "C:\Program Files\KiCad\10.0\bin\python.exe"
$kicadCli = "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"

if (-not (Test-Path -LiteralPath $python)) {
  throw "KiCad Python not found: $python"
}
if (-not (Test-Path -LiteralPath $kicadCli)) {
  throw "KiCad CLI not found: $kicadCli"
}

$polishScript = Join-Path $root "scripts\polish_kicad_output.py"
$repairScript = Join-Path $root "scripts\repair_kicad_model_paths.py"
$enhanceSvgScript = Join-Path $root "scripts\enhance_kicad_svg.py"
$outputDir = Join-Path $root "generated\external-router-output"
$renderDir = Join-Path $root "generated"
$reportDir = Join-Path $root "reports"
$webRenderDir = Join-Path $repo "public\assets\kicad-renders"
$svgDir = Join-Path $root "generated\kicad-svg"
$webSvgDir = Join-Path $repo "public\assets\kicad-svg"

New-Item -ItemType Directory -Force -Path $renderDir, $reportDir, $webRenderDir, $svgDir, $webSvgDir | Out-Null

function Invoke-Native {
  param(
    [string]$Command,
    [string[]]$Arguments
  )

  & $Command @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed with exit code $LASTEXITCODE`: $Command"
  }
}

foreach ($name in $Case) {
  $outputPcb = Join-Path $outputDir "$name-krt-routed.kicad_pcb"
  $drcReport = Join-Path $reportDir "$name-krt-drc.rpt"
  $renderPng = Join-Path $renderDir "$name-krt-render.png"
  $routeSvg = Join-Path $svgDir "$name-krt-routed.svg"
  $routeSvgTop = Join-Path $svgDir "$name-krt-routed-fcu.svg"
  $routeSvgBottom = Join-Path $svgDir "$name-krt-routed-bcu.svg"

  Write-Host "[1/3] Polish silkscreen and model paths for $name"
  Invoke-Native $python @($polishScript, $outputPcb)
  Invoke-Native $python @($repairScript, $outputPcb)

  Write-Host "[2/3] Run DRC and render $name"
  Invoke-Native $kicadCli @("pcb", "drc", "--output", $drcReport, "--format", "report", "--severity-all", $outputPcb)
  Invoke-Native $kicadCli @(
    "pcb", "render",
    "--output", $renderPng,
    "--width", "1366",
    "--height", "768",
    "--quality", "high",
    "--background", "opaque",
    "--perspective",
    "--rotate", "-55,0,38",
    "--zoom", "0.78",
    $outputPcb
  )

  Write-Host "[3/3] Export top/bottom SVGs for $name"
  Invoke-Native $kicadCli @(
    "pcb", "export", "svg",
    "--output", $routeSvgTop,
    "--mode-single",
    "--layers", "F.Cu,F.SilkS,Edge.Cuts",
    "--page-size-mode", "2",
    "--fit-page-to-board",
    "--exclude-drawing-sheet",
    $outputPcb
  )
  Invoke-Native $kicadCli @(
    "pcb", "export", "svg",
    "--output", $routeSvgBottom,
    "--mode-single",
    "--mirror",
    "--layers", "B.Cu,B.SilkS,Edge.Cuts",
    "--page-size-mode", "2",
    "--fit-page-to-board",
    "--exclude-drawing-sheet",
    $outputPcb
  )
  Invoke-Native $python @($enhanceSvgScript, $routeSvgTop, $routeSvgBottom)

  Copy-Item -LiteralPath $renderPng -Destination (Join-Path $webRenderDir "$name-krt-render.png") -Force
  Copy-Item -LiteralPath $routeSvgTop -Destination $routeSvg -Force
  Copy-Item -LiteralPath $routeSvgTop -Destination (Join-Path $webSvgDir "$name-krt-routed.svg") -Force
  Copy-Item -LiteralPath $routeSvgTop -Destination (Join-Path $webSvgDir "$name-krt-routed-fcu.svg") -Force
  Copy-Item -LiteralPath $routeSvgBottom -Destination (Join-Path $webSvgDir "$name-krt-routed-bcu.svg") -Force

  Write-Host "Done $name"
}
