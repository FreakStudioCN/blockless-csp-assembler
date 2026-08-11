param(
  [ValidateSet("long-range-env-node", "desktop-air-monitor")]
  [string]$Case = "long-range-env-node"
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$root = Join-Path $repo "kicad-host-mvp"
$kicadCli = "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
$pcb2gcodeCommand = Get-Command pcb2gcode.exe -ErrorAction SilentlyContinue
$pcb2gcode = if ($env:PCB2GCODE_EXE) { $env:PCB2GCODE_EXE } elseif ($pcb2gcodeCommand) { $pcb2gcodeCommand.Source } else { $null }

$sourcePcb = Join-Path $root "generated\external-router-output\$Case-krt-routed.kicad_pcb"
if (-not (Test-Path -LiteralPath $sourcePcb)) {
  $sourcePcb = Join-Path $root "generated\$Case.kicad_pcb"
}

if (-not (Test-Path -LiteralPath $kicadCli)) {
  throw "KiCad CLI not found: $kicadCli"
}
if (-not $pcb2gcode -or -not (Test-Path -LiteralPath $pcb2gcode)) {
  throw "pcb2gcode not found. Add pcb2gcode.exe to PATH or set PCB2GCODE_EXE to its full path."
}
if (-not (Test-Path -LiteralPath $sourcePcb)) {
  throw "KiCad PCB not found for case '$Case': $sourcePcb"
}

$outRoot = Join-Path $repo "output\cam-real\$Case"
$gerberOut = Join-Path $outRoot "gerbers"
$gcodeOut = Join-Path $outRoot "gcode"
$webOut = Join-Path $repo "public\assets\cam-real\$Case"
$tempRoot = Join-Path $env:TEMP "blockless-cam-real\$Case"
$tempGerbers = Join-Path $tempRoot "gerbers"
$tempGcode = Join-Path $tempRoot "gcode"

New-Item -ItemType Directory -Force -Path $gerberOut, $gcodeOut, $webOut | Out-Null
Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $tempGerbers, $tempGcode | Out-Null

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

Write-Host "[1/4] Export Gerber and Excellon from KiCad PCB"
Invoke-Native $kicadCli @(
  "pcb", "export", "gerbers",
  "--output", $gerberOut,
  "--layers", "F.Cu,B.Cu,Edge.Cuts",
  "--no-protel-ext",
  "--no-x2",
  "--precision", "6",
  $sourcePcb
)
Invoke-Native $kicadCli @(
  "pcb", "export", "drill",
  "--output", $gerberOut,
  "--format", "excellon",
  "--excellon-units", "mm",
  "--excellon-zeros-format", "decimal",
  $sourcePcb
)

$baseName = [System.IO.Path]::GetFileNameWithoutExtension($sourcePcb)
$frontGerber = Join-Path $gerberOut "$baseName-F_Cu.gbr"
$backGerber = Join-Path $gerberOut "$baseName-B_Cu.gbr"
$outlineGerber = Join-Path $gerberOut "$baseName-Edge_Cuts.gbr"
$drillFile = Join-Path $gerberOut "$baseName.drl"

foreach ($required in @($frontGerber, $backGerber, $outlineGerber, $drillFile)) {
  if (-not (Test-Path -LiteralPath $required)) {
    throw "Expected KiCad CAM input missing: $required"
  }
}

Write-Host "[2/4] Copy CAM inputs into ASCII-safe work directory"
Copy-Item -LiteralPath $frontGerber -Destination (Join-Path $tempGerbers "front.gbr") -Force
Copy-Item -LiteralPath $backGerber -Destination (Join-Path $tempGerbers "back.gbr") -Force
Copy-Item -LiteralPath $outlineGerber -Destination (Join-Path $tempGerbers "outline.gbr") -Force
Copy-Item -LiteralPath $drillFile -Destination (Join-Path $tempGerbers "drill.drl") -Force

Write-Host "[3/4] Convert Gerber/Excellon to real CNC G-code with pcb2gcode"
Invoke-Native $pcb2gcode @(
  "--metric",
  "--metricoutput",
  "--front", (Join-Path $tempGerbers "front.gbr"),
  "--back", (Join-Path $tempGerbers "back.gbr"),
  "--drill", (Join-Path $tempGerbers "drill.drl"),
  "--outline", (Join-Path $tempGerbers "outline.gbr"),
  "--output-dir", $tempGcode,
  "--basename", $Case,
  "--zsafe", "2.5",
  "--zchange", "8",
  "--zwork", "-0.08",
  "--zdrill", "-1.7",
  "--zcut", "-1.7",
  "--mill-infeed", "0.08",
  "--cut-infeed", "0.45",
  "--mill-diameters", "0.20",
  "--isolation-width", "0.35",
  "--mill-feed", "260",
  "--mill-vertfeed", "90",
  "--mill-speed", "12000",
  "--drill-feed", "120",
  "--drill-speed", "9000",
  "--cut-feed", "220",
  "--cut-vertfeed", "90",
  "--cut-speed", "10000",
  "--cutter-diameter", "0.8",
  "--nog81",
  "--zero-start",
  "--ignore-warnings"
)

Write-Host "[4/4] Publish CAM outputs"
Get-ChildItem -LiteralPath $gcodeOut -File -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath $webOut -File -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath $tempGcode -File | ForEach-Object {
  Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $gcodeOut $_.Name) -Force
  if ($_.Extension -in @(".ngc", ".svg")) {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $webOut $_.Name) -Force
  }
}

$fileEntries = Get-ChildItem -LiteralPath $gcodeOut -File |
  Sort-Object Name |
  ForEach-Object {
    $kind = if ($_.Name -like "*front*.ngc") {
      "top-isolation"
    } elseif ($_.Name -like "*back*.ngc") {
      "bottom-isolation"
    } elseif ($_.Name -like "*drill*.ngc") {
      "drill"
    } elseif ($_.Name -like "*outline*.ngc") {
      "outline"
    } elseif ($_.Name -like "*processed_front_final.svg") {
      "front-preview"
    } elseif ($_.Name -like "*processed_back_final.svg") {
      "back-preview"
    } else {
      "support"
    }

    [ordered]@{
      name = $_.Name
      kind = $kind
      bytes = $_.Length
      path = $_.FullName
      url = "/assets/cam-real/$Case/$($_.Name)"
    }
  }

$manifest = [ordered]@{
  ok = $true
  case = $Case
  sourcePcb = $sourcePcb
  gerberDir = $gerberOut
  gcodeDir = $gcodeOut
  webDir = $webOut
  tool = "pcb2gcode 2.4.0"
  generatedAt = (Get-Date).ToUniversalTime().ToString("o")
  files = $fileEntries
}

$manifestPath = Join-Path $gcodeOut "manifest.json"
$webManifestPath = Join-Path $webOut "manifest.json"
$manifestJson = $manifest | ConvertTo-Json -Depth 6
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)
Copy-Item -LiteralPath $manifestPath -Destination $webManifestPath -Force

Write-Host "Done."
Write-Host "  Source PCB: $sourcePcb"
Write-Host "  Gerbers:    $gerberOut"
Write-Host "  G-code:     $gcodeOut"
Write-Host "  Manifest:   $manifestPath"
