[CmdletBinding()]
param(
    [string]$Destination = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'prod'),
    [string]$ProductPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$projectProduct = Join-Path $projectRoot 'dist\MechaLeomon'
$localBuildProduct = Join-Path $env:LOCALAPPDATA 'MechaLeomon\build\dist\MechaLeomon'
$product = if ($ProductPath) {
    $ProductPath
} elseif (($projectRoot.StartsWith('\\wsl.localhost\') -or
        $projectRoot.StartsWith('\\wsl$\')) -and
        (Test-Path -LiteralPath (Join-Path $localBuildProduct 'MechaLeomon.exe'))) {
    # Alcune configurazioni Windows impediscono di leggere file PE dalla share
    # WSL. La build NTFS e la stessa copia gia verificata dal self-test.
    $localBuildProduct
} else {
    $projectProduct
}
$sourceExe = Join-Path $product 'MechaLeomon.exe'
$readme = Join-Path $projectRoot 'PORTABLE_README.txt'

if (-not (Test-Path -LiteralPath $sourceExe)) {
    throw "Build non trovata: $sourceExe"
}
if (Test-Path -LiteralPath $Destination) {
    throw "La destinazione esiste gia: $Destination"
}

New-Item -ItemType Directory -Path $Destination | Out-Null
Copy-Item -Path (Join-Path $product '*') -Destination $Destination -Recurse

$portableExe = Join-Path $Destination 'MechaLeomon.exe'
Copy-Item -LiteralPath $readme -Destination (Join-Path $Destination 'LEGGIMI.txt')
Copy-Item -LiteralPath (Join-Path $projectRoot 'VERSION') `
    -Destination (Join-Path $Destination 'VERSION.txt')

$hash = (Get-FileHash -LiteralPath $portableExe -Algorithm SHA256).Hash
Set-Content -LiteralPath (Join-Path $Destination 'SHA256.txt') `
    -Value "MechaLeomon.exe  $hash" -Encoding ASCII

Write-Host "Pacchetto portabile creato: $Destination" -ForegroundColor Green
Write-Host "Avvio: $portableExe" -ForegroundColor Green
