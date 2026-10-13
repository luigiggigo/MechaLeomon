[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall,
    [switch]$SkipDesktopInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$venv = Join-Path $projectRoot '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'
$dist = Join-Path $projectRoot 'dist'
$publishedProduct = Join-Path $dist 'MechaLeomon'
$localBuildRoot = Join-Path $env:LOCALAPPDATA 'MechaLeomon\build'
$localDist = Join-Path $localBuildRoot 'dist'
$localWork = Join-Path $localBuildRoot 'work'
$product = Join-Path $localDist 'MechaLeomon'
$installRoot = Join-Path $env:LOCALAPPDATA 'MechaLeomon\app'

function Stop-ProductProcesses {
    # Lo stesso percorso WSL puo apparire a Windows come \\wsl.localhost o
    # \\wsl$, rendendo inaffidabile un confronto letterale di Process.Path.
    # Il nome dell'eseguibile e univoco per questa applicazione.
    foreach ($process in Get-Process -Name @('MechaLeomon', 'DigiWorldExplorer') -ErrorAction SilentlyContinue) {
        Write-Host "Chiusura di $($process.ProcessName) (PID $($process.Id))..." -ForegroundColor Yellow
        if ($process.CloseMainWindow()) {
            $process.WaitForExit(3000) | Out-Null
        }
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
            $process.WaitForExit(3000) | Out-Null
        }
    }
}

function Publish-Product {
    if ($projectRoot.StartsWith('\\wsl.localhost\') -or $projectRoot.StartsWith('\\wsl$\')) {
        $uncParts = $projectRoot.TrimStart('\').Split('\')
        if ($uncParts.Count -lt 3) { throw "Percorso WSL non valido: $projectRoot" }
        $distro = $uncParts[1]
        $localMatch = [regex]::Match($product, '^([A-Za-z]):\\(.*)$')
        if (-not $localMatch.Success) {
            throw 'Conversione del percorso di build in WSL fallita.'
        }
        $drive = $localMatch.Groups[1].Value.ToLowerInvariant()
        $localTail = $localMatch.Groups[2].Value.Replace('\', '/')
        $linuxProduct = "/mnt/$drive/$localTail"
        $linuxProjectRoot = '/' + (($uncParts[2..($uncParts.Count - 1)]) -join '/')
        $linuxPublishedProduct = "$linuxProjectRoot/dist/MechaLeomon"
        if (-not $linuxPublishedProduct.EndsWith('/dist/MechaLeomon')) {
            throw 'Conversione del percorso di pubblicazione in WSL fallita.'
        }

        # Windows puo leggere la share WSL ma alcuni sistemi bloccano la
        # creazione di file PE su di essa. Copia quindi dal lato Linux.
        & wsl.exe --distribution $distro -- rm -rf -- $linuxPublishedProduct
        if ($LASTEXITCODE -ne 0) { throw 'Pulizia della build precedente fallita.' }
        & wsl.exe --distribution $distro -- mkdir -p -- $linuxPublishedProduct
        if ($LASTEXITCODE -ne 0) { throw 'Creazione della cartella di pubblicazione fallita.' }
        & wsl.exe --distribution $distro -- cp -a -- "$linuxProduct/." $linuxPublishedProduct
        if ($LASTEXITCODE -ne 0) { throw 'Pubblicazione della build tramite WSL fallita.' }
        return
    }

    Remove-Item -LiteralPath $publishedProduct -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $dist -Force | Out-Null
    Copy-Item -LiteralPath $product -Destination $dist -Recurse -Force
}

function Find-Python {
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        if ($name -eq 'py.exe') {
            $candidate = @(& $command.Source -3 -c 'import sys; print(sys.executable)' 2>$null)
        } else {
            $candidate = @(& $command.Source -c 'import sys; print(sys.executable)' 2>$null)
        }
        if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate[-1])) {
            return $candidate[-1]
        }
    }
    throw 'Python 3.10 o più recente non trovato.'
}

Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath $python)) {
    $basePython = Find-Python
    & $basePython -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Creazione ambiente Python fallita.' }
}

if (-not $SkipDependencyInstall) {
    & $python -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt') 'pyinstaller>=6.14,<7'
    if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze fallita.' }
}

& $python (Join-Path $projectRoot 'prepare_icon.py')
if ($LASTEXITCODE -ne 0) { throw 'Preparazione icona fallita.' }

& $python -m unittest discover -s (Join-Path $projectRoot 'tests') -v
if ($LASTEXITCODE -ne 0) { throw 'Test desktop falliti.' }

# PyInstaller scrive migliaia di file. Farlo su NTFS evita i lock e i problemi
# di permessi che possono verificarsi sulla share \\wsl.localhost durante COLLECT.
Remove-Item -LiteralPath $localBuildRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $localDist, $localWork -Force | Out-Null
& $python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --distpath $localDist `
    --workpath $localWork `
    --specpath $localBuildRoot `
    --name 'MechaLeomon' `
    --additional-hooks-dir (Join-Path $projectRoot "pyinstaller_hooks") `
    --collect-all rapidocr `
    --icon (Join-Path $projectRoot 'assets\mechaleomon_icon.ico') `
    --add-data "$(Join-Path $projectRoot 'assets\mechaleomon_icon.png');assets" `
    --add-data "$(Join-Path $projectRoot 'VERSION');." `
(Join-Path $projectRoot 'app.py')
if ($LASTEXITCODE -ne 0) { throw 'Build EXE fallita.' }

Stop-ProductProcesses
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
Copy-Item -Path (Join-Path $product '*') -Destination $installRoot -Recurse -Force
$target = Join-Path $installRoot 'MechaLeomon.exe'

# Windows can build from the WSL UNC share, but executing a frozen app directly
# from that share can be blocked or stall while its DLLs are loaded. Test the
# exact installed copy from the local NTFS path instead.
$selfTest = Join-Path $env:LOCALAPPDATA 'MechaLeomon\self-test.json'
Remove-Item -LiteralPath $selfTest -Force -ErrorAction SilentlyContinue
$selfTestProcess = Start-Process -FilePath $target `
    -ArgumentList @('--self-test', $selfTest) -Wait -PassThru
if ($selfTestProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $selfTest)) {
    throw 'Self-test del programma compilato fallito.'
}
$selfTestData = Get-Content -LiteralPath $selfTest -Raw | ConvertFrom-Json
if (-not $selfTestData.icon -or -not $selfTestData.center -or -not $selfTestData.wait -or `
        -not $selfTestData.strategy -or -not $selfTestData.auto_pull -or `
        -not $selfTestData.pvp_math -or -not $selfTestData.pvp_ocr -or `
        -not $selfTestData.tk) {
    throw 'Il programma compilato non ha superato tutti i controlli.'
}

if (-not $SkipDesktopInstall) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    Remove-Item -LiteralPath (Join-Path $desktop 'DigiWorld Explorer.lnk') `
        -Force -ErrorAction SilentlyContinue
    $shortcutPath = Join-Path $desktop 'MechaLeomon.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $target
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.IconLocation = "$target,0"
    $shortcut.Description = 'MechaLeomon - automazione BlueStacks con pausa e ripresa'
    $shortcut.Save()
    Write-Host "Installato: $target" -ForegroundColor Green
    Write-Host "Collegamento desktop: $shortcutPath" -ForegroundColor Green
}

# Pubblica per ultimo nella cartella del progetto. La build e il self-test sono
# gia conclusi, quindi un vecchio EXE bloccato non puo interrompere COLLECT.
Publish-Product

Write-Host "Build verificata: $(Join-Path $publishedProduct 'MechaLeomon.exe')" -ForegroundColor Green
