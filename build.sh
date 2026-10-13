#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Uso: ./build.sh [opzioni]

Compila e verifica MechaLeomon usando Python e PowerShell di Windows.
Per impostazione predefinita installa anche l'app e il collegamento sul desktop.

Opzioni:
  --skip-deps   Riusa le dipendenze presenti in desktop/.venv-build
  --no-install  Compila e verifica senza installare/aggiornare il collegamento
  --portable    Dopo la build crea anche Desktop\prod
  -h, --help    Mostra questo aiuto
EOF
}

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_script="$project_root/desktop/build_windows.ps1"
portable_script="$project_root/desktop/create_portable.ps1"
skip_deps=false
skip_install=false
make_portable=false

while (($#)); do
    case "$1" in
        --skip-deps) skip_deps=true ;;
        --no-install) skip_install=true ;;
        --portable) make_portable=true ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Opzione sconosciuta: %s\n\n' "$1" >&2
            usage >&2
            exit 64
            ;;
    esac
    shift
done

if [[ ! -f "$build_script" ]]; then
    printf 'Script di build non trovato: %s\n' "$build_script" >&2
    exit 1
fi

if ! command -v powershell.exe >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Errore: powershell.exe non è disponibile.
Esegui questo script da WSL su Windows; la build richiede Python e PyInstaller
per Windows per produrre MechaLeomon.exe.
EOF
    exit 1
fi

to_windows_path() {
    if command -v wslpath >/dev/null 2>&1; then
        wslpath -w "$1"
    else
        printf '%s\n' "$1"
    fi
}

windows_build_script="$(to_windows_path "$build_script")"
build_args=(
    -NoProfile
    -ExecutionPolicy Bypass
    -File "$windows_build_script"
)

if [[ "$skip_deps" == true ]]; then
    build_args+=(-SkipDependencyInstall)
fi
if [[ "$skip_install" == true ]]; then
    build_args+=(-SkipDesktopInstall)
fi

printf 'Avvio build Windows di MechaLeomon...\n'
powershell.exe "${build_args[@]}"

if [[ "$make_portable" == true ]]; then
    if [[ ! -f "$portable_script" ]]; then
        printf 'Script portabile non trovato: %s\n' "$portable_script" >&2
        exit 1
    fi
    windows_portable_script="$(to_windows_path "$portable_script")"
    printf 'Creazione pacchetto portabile...\n'
    powershell.exe -NoProfile -ExecutionPolicy Bypass \
        -File "$windows_portable_script"
fi

printf '\nBuild completata con successo.\n'
printf 'EXE: %s\n' "$project_root/desktop/dist/MechaLeomon/MechaLeomon.exe"
if [[ "$skip_install" == false ]]; then
    printf 'Installazione: %%LOCALAPPDATA%%\\MechaLeomon\\app\n'
fi
if [[ "$make_portable" == true ]]; then
    printf 'Pacchetto portabile: Desktop\\prod\n'
fi
