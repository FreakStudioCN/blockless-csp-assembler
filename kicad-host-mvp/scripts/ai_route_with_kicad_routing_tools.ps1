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

$planScript = Join-Path $root "scripts\generate_plan.py"
$validateScript = Join-Path $root "scripts\validate_plans.py"
$buildScript = Join-Path $root "scripts\build_boards_with_kicad_python.py"
$routeWrapper = Join-Path $root "scripts\run_kicad_routing_tools.py"
$polishScript = Join-Path $root "scripts\polish_kicad_output.py"
$repairScript = Join-Path $root "scripts\repair_kicad_model_paths.py"
$enhanceSvgScript = Join-Path $root "scripts\enhance_kicad_svg.py"

$inputDir = Join-Path $root "generated\external-router-input"
$outputDir = Join-Path $root "generated\external-router-output"
$renderDir = Join-Path $root "generated"
$reportDir = Join-Path $root "reports"
$webRenderDir = Join-Path $repo "public\assets\kicad-renders"
$svgDir = Join-Path $root "generated\kicad-svg"
$webSvgDir = Join-Path $repo "public\assets\kicad-svg"

New-Item -ItemType Directory -Force -Path $inputDir, $outputDir, $renderDir, $reportDir, $webRenderDir, $svgDir, $webSvgDir | Out-Null

function Invoke-NativeLogged {
  param(
    [string]$Command,
    [string[]]$Arguments,
    [string]$LogPath
  )

  $output = & $Command @Arguments 2>&1
  $exitCode = $LASTEXITCODE
  $output | Set-Content -Encoding UTF8 -Path $LogPath
  $output
  if ($exitCode -ne 0) {
    throw "Command failed with exit code $exitCode. Log: $LogPath"
  }
}

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

Write-Host "[1/5] Generate and validate AI module plans"
Invoke-Native $python @($planScript)
Invoke-Native $python @($validateScript)

Write-Host "[2/5] Build unrouted KiCad boards"
Invoke-Native $python @($buildScript, "--unrouted", "--output-dir", $inputDir, "--suffix=-unrouted")

$caseConfig = @{
  "long-range-env-node" = @{
    PowerNets = @("5V", "3V3", "BAT+", "GND")
    PowerWidths = @("0.6", "0.45", "0.8", "0.7")
  }
  "desktop-air-monitor" = @{
    PowerNets = @("5V", "3V3", "GND")
    PowerWidths = @("0.6", "0.45", "0.7")
  }
}

foreach ($name in $Case) {
  if (-not $caseConfig.ContainsKey($name)) {
    throw "Unknown case '$name'. Supported cases: $($caseConfig.Keys -join ', ')"
  }

  $inputPcb = Join-Path $inputDir "$name-unrouted.kicad_pcb"
  $outputPcb = Join-Path $outputDir "$name-krt-routed.kicad_pcb"
  $routeLog = Join-Path $reportDir "$name-krt-route.log"
  $drcReport = Join-Path $reportDir "$name-krt-drc.rpt"
  $renderPng = Join-Path $renderDir "$name-krt-render.png"
  $routeSvg = Join-Path $svgDir "$name-krt-routed.svg"
  $routeSvgTop = Join-Path $svgDir "$name-krt-routed-fcu.svg"
  $routeSvgBottom = Join-Path $svgDir "$name-krt-routed-bcu.svg"

  Write-Host "[3/5] Route $name with KiCadRoutingTools"
  $routeArgs = @(
    $routeWrapper,
    $inputPcb,
    $outputPcb,
    "--nets", "*",
    "--layers", "F.Cu", "B.Cu",
    "--track-width", "0.25",
    "--clearance", "0.2",
    "--via-size", "0.75",
    "--via-drill", "0.35",
    "--ordering", "mps",
    "--max-iterations", "10000",
    "--stats"
  )
  $routeArgs += "--power-nets"
  $routeArgs += $caseConfig[$name].PowerNets
  $routeArgs += "--power-nets-widths"
  $routeArgs += $caseConfig[$name].PowerWidths
  Invoke-NativeLogged $python $routeArgs $routeLog

  Write-Host "[4/5] Repair 3D model paths, run DRC, and render $name"
  Invoke-Native $python @($polishScript, $outputPcb)
  Invoke-Native $python @($repairScript, $outputPcb)
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
  Copy-Item -LiteralPath $renderPng -Destination (Join-Path $webRenderDir "$name-krt-render.png") -Force
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
  Copy-Item -LiteralPath $routeSvgTop -Destination $routeSvg -Force
  Copy-Item -LiteralPath $routeSvgTop -Destination (Join-Path $webSvgDir "$name-krt-routed.svg") -Force
  Copy-Item -LiteralPath $routeSvgTop -Destination (Join-Path $webSvgDir "$name-krt-routed-fcu.svg") -Force
  Copy-Item -LiteralPath $routeSvgBottom -Destination (Join-Path $webSvgDir "$name-krt-routed-bcu.svg") -Force

  Write-Host "[5/5] Done $name"
  Write-Host "  PCB:    $outputPcb"
  Write-Host "  Render: $renderPng"
  Write-Host "  SVG F:  $routeSvgTop"
  Write-Host "  SVG B:  $routeSvgBottom"
  Write-Host "  Route:  $routeLog"
  Write-Host "  DRC:    $drcReport"
}
