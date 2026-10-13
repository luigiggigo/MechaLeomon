# MechaLeomon

Bot desktop per **Digimon UP** su Windows e BlueStacks 5. Riconosce la griglia
DigiWorld dagli screenshot, pianifica il percorso e offre controlli di pausa e arresto.
Include modalità Auto Pull, gestione Network Defense Ops (Diaboromon) e analisi PvP con OCR locale.

**Versione corrente:** [`1.6.3`](desktop/VERSION) · **Piattaforma:** Windows · **Python:** 3.10+

## Funzionalità

- Percorsi con Dijkstra, priorità ai chip e gestione di pickup, ostacoli, Dash e attacchi.
- Pausa/Riprendi e Ferma, con controlli durante le attese e il recupero ADB.
- Auto Pull 10x e 35x come modalità separate.
- Azioni Diaboromon ancorate ai controlli riconosciuti sullo schermo.
- Analisi PvP con OCR eseguito localmente e valori correggibili prima del calcolo.

Consulta la [guida utente](desktop/README.md) per comportamento, limiti e modalità.

## Requisiti e avvio

1. Installa BlueStacks 5 su Windows e avvia Digimon UP.
2. Abilita **Impostazioni → Avanzate → Android Debug Bridge** in BlueStacks.
3. Installa Python 3.10+ per eseguire i sorgenti o creare una build.
4. Clona il repository:

```bash
git clone https://github.com/luigiggigo/MechaLeomon.git
cd MechaLeomon
```

Da PowerShell, nella radice:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r desktop/requirements.txt
.\.venv\Scripts\python.exe desktop/app.py
```

Una build portabile completa non richiede Python sul PC di destinazione.
Questo repository distribuisce i sorgenti; non include eseguibili Windows verificati.

## Build Windows

Da Windows esegui `desktop\INSTALL_WINDOWS.cmd`. Da WSL:

```bash
./build.sh
```

La build installa dipendenze, esegue i test, genera l'EXE con PyInstaller,
aggiorna `%LOCALAPPDATA%\MechaLeomon\app`, esegue il self-test frozen e crea
il collegamento sul desktop. Può chiudere un'istanza del bot già in esecuzione.
`--no-install` salta il collegamento ma aggiorna comunque l'app locale per il self-test.

Per creare e verificare una portabile versionata consulta la
[guida di distribuzione](docs/DISTRIBUTION.md).

## Sviluppo e test

Su Linux/WSL servono Python 3.10+, venv e Tkinter. Dalla radice:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=desktop .venv/bin/python -m pytest -q desktop/tests
.venv/bin/ruff check desktop
PYTHONPATH=desktop .venv/bin/python -m compileall -q -x '/(dist|build|\.venv-build)/' desktop
bash -n build.sh
```

I test usano simulazioni e non devono inviare input reali ad ADB.
Le catture reali di regressione sono escluse perché possono contenere nomi e dati
dei giocatori: i test che le richiedono vengono segnalati come **skipped**.
I test sintetici e la fixture numerica PvP sono inclusi. Una suite pubblica verde
non certifica le regressioni sulle catture private né la build frozen Windows.

## Struttura

| Percorso | Contenuto |
| --- | --- |
| `desktop/app.py` | GUI e avvio del worker |
| `desktop/digiworld_bot.py` | ADB e riconoscimento visivo |
| `desktop/auto_digiworld.py` | Strategia e percorsi |
| `desktop/auto_digiworld_batch2.py` | Worker di produzione |
| `desktop/auto_pull.py` | Modalità Auto Pull |
| `desktop/pvp_*.py` | Modello PvP, OCR e finestra di analisi |
| `desktop/tests/` | Test sintetici e regressioni disponibili |
| `desktop/tools/` | Strumenti diagnostici |
| `desktop/VERSION` | Versione autorevole |

## Privacy e segnalazioni

Screenshot e input passano dall'ADB locale di BlueStacks. L'OCR PvP usa modelli
locali e non invia le catture a un servizio cloud. I log e le diagnosi vengono
scritti in `%LOCALAPPDATA%\MechaLeomon\runs`: controllali prima di condividerli.
Non pubblicare credenziali, nomi dei giocatori o screenshot non anonimizzati.

Per bug e contributi consulta [CONTRIBUTING.md](CONTRIBUTING.md); per problemi
sensibili consulta [SECURITY.md](SECURITY.md).

## Stato del progetto e diritti

Progetto indipendente, non affiliato ai titolari di Digimon UP o BlueStacks.
Nomi, marchi e contenuti di terzi appartengono ai rispettivi titolari.
Il codice e la documentazione originali del progetto sono distribuiti sotto
[licenza MIT](LICENSE), copyright © 2026 luigiggigo.
La licenza MIT non si applica a marchi, personaggi, immagini o altri contenuti
di terzi, inclusi eventuali elementi Digimon presenti negli asset; non concede
autorizzazioni al loro utilizzo. Le dipendenze mantengono le rispettive licenze.
