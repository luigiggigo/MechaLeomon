@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
if errorlevel 1 (
  echo.
  echo Installazione non riuscita. Leggi il messaggio qui sopra.
  pause
  exit /b 1
)
echo.
echo MechaLeomon e pronto sul desktop.
pause
