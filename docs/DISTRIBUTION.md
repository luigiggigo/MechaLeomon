# Build e distribuzione

La build autorevole è `desktop/build_windows.ps1`, richiamata da `build.sh`
o da `desktop/INSTALL_WINDOWS.cmd`. Richiede Windows, Python Windows 3.10+
e PowerShell; il wrapper WSL richiede anche `wslpath`.

Il processo installa le dipendenze, esegue i test, crea il bundle PyInstaller,
lo installa per utente ed esegue il self-test frozen. Non richiede privilegi
amministrativi. I test che dipendono da screenshot privati vengono saltati nel
checkout pubblico: riporta questa limitazione quando distribuisci una build.

## Portabile

Dalla radice, dopo una build riuscita, in PowerShell:

```powershell
$version = (Get-Content .\desktop\VERSION -Raw).Trim()
$destination = Join-Path ([Environment]::GetFolderPath('Desktop')) "MechaLeomon-portable-$version"
.\desktop\create_portable.ps1 -Destination $destination
Get-Content (Join-Path $destination 'VERSION.txt')
Get-Content (Join-Path $destination 'SHA256.txt')
Get-FileHash (Join-Path $destination 'MechaLeomon.exe') -Algorithm SHA256
```

La destinazione deve essere nuova. Confronta l'hash calcolato con `SHA256.txt`,
verifica la versione e conserva l'intera cartella, inclusa `_internal`.
L'EXE da solo non costituisce una distribuzione completa.

La presenza di sorgenti o di un tag non prova che esista una build Windows
verificata. Pubblica binari soltanto dopo build, self-test frozen e verifica
della portabile. Gli eseguibili non firmati possono attivare Windows SmartScreen.
