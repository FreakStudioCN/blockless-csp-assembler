$commands = @("kicad", "kicad-cli", "pcbnew")
foreach ($cmd in $commands) {
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($found) {
    [PSCustomObject]@{ Command = $cmd; Found = $true; Source = $found.Source }
  } else {
    [PSCustomObject]@{ Command = $cmd; Found = $false; Source = "" }
  }
}

$knownBins = @(
  "$env:ProgramFiles\KiCad\10.0\bin",
  "$env:ProgramFiles\KiCad\9.0\bin",
  "$env:ProgramFiles\KiCad\8.0\bin"
) | Where-Object { $_ -and (Test-Path $_) }

foreach ($bin in $knownBins) {
  foreach ($exe in @("kicad.exe", "kicad-cli.exe", "pcbnew.exe")) {
    $path = Join-Path $bin $exe
    if (Test-Path -LiteralPath $path) {
      [PSCustomObject]@{ Command = $exe; Found = $true; Source = $path }
    }
  }
}

$candidateRoots = @(
  "$env:ProgramFiles\KiCad",
  "${env:ProgramFiles(x86)}\KiCad"
) | Where-Object { $_ -and (Test-Path $_) }

foreach ($root in $candidateRoots) {
  Get-ChildItem -LiteralPath $root -Recurse -Filter kicad.exe -ErrorAction SilentlyContinue |
    Select-Object -First 5 FullName
}
