# MechaLeomon per Windows

Questa è la versione desktop del bot. Si collega a BlueStacks tramite l'ADB
incluso nell'emulatore, analizza screenshot locali e invia tocchi solo quando
la griglia è riconosciuta con confidenza sufficiente.

Il progetto è mantenuto nel [repository desktop dedicato](../README.md).
Per setup da un nuovo clone, ambiente Python e comandi dalla radice consulta
il README principale. I comandi di questa guida partono dalla cartella `desktop/`.

## Uso quotidiano

1. Avvia BlueStacks e apri DigiWorld nel gioco.
2. Fai doppio clic su **MechaLeomon** sul desktop di Windows.
3. L'automazione parte direttamente e mostra una piccola finestra.
4. Usa **Pausa/Riprendi** o **Ferma** in qualsiasi momento.

I due pulsanti **Auto Pull 10x** e **Auto Pull 35x** arrestano in sicurezza il
worker DigiWorld e avviano due modalità separate. La scelta dell'utente decide
il flusso: il bot non tenta più di classificare automaticamente un controllo
come 10x o 35x. Entrambe le modalità richiedono la coppia allineata dei
pulsanti e non usano coordinate assolute.

**Auto Pull 10x** riconosce separatamente anche la conferma tramite titolo
cyan, pannello blu e riquadro esplicativo scuro. Dopo un tap confermato blocca
soltanto la ripetizione dello stesso controllo, consentendo alla conferma di
seguire subito il primo tap. **Auto Pull 35x** ancora invece il controllo
rilevato e lo ripete con pause variabili di 0,25–0,45 secondi (circa 2–4 tocchi
al secondo, incluso l'overhead ADB), anche mentre il gioco nasconde
temporaneamente il pulsante. Le attese restano sempre sensibili a Pausa/Ferma;
il pulsante relativo alla modalità attiva permette di tornare al bot DigiWorld,
mentre l'altro passa direttamente all'altro tipo di pull.

L'EXE installato non richiede Python, WSL, privilegi amministrativi,
accessibilità Android, cattura schermo Android o overlay. BlueStacks deve avere
ADB abilitato una sola volta in **Impostazioni → Avanzate → Android Debug
Bridge**; è il canale locale usato per screenshot e tocchi.

Tutti i processi `HD-Adb.exe` vengono eseguiti con `CREATE_NO_WINDOW`: gli
screenshot periodici restano in background e non aprono o fanno lampeggiare
finestre della console.

Nel dungeon **Network Defense Ops (Diaboromon)** il bot riconosce anche la
lobby e preme **Find a Party** quando attivo. Quando entrambi gli slot partner
sono occupati preme **Attempt**, se attivo. In battaglia preme **Give Up**
soltanto con il pulsante attivo e la prova del party **5/5** oppure del banner VS.
Overlay e pulsanti disabilitati non autorizzano il tap. Il confronto dei glifi
del contatore avviene nello stesso frame e non richiede OCR.

I log e le immagini diagnostiche sono salvati in
`%LOCALAPPDATA%\MechaLeomon\runs`.

## Routing

Ad ogni scansione il programma calcola l'intero percorso visibile con
Dijkstra. Le priorità sono:

1. chip arancioni, sempre, anche quando richiedono di tornare indietro;
2. pickup verdi del Dash, senza passi all'indietro;
3. passi viola soltanto se il loro valore di quattro passi copre interamente
   la deviazione verso la successiva frontiera;
4. avanzamento verso destra, vietando il ritorno in assenza di chip arancioni.

Un avanzamento pesa 10, un cambio di riga 12, un ritorno 24 e attraversare una
piramide aggiunge 100. Quasi ogni deviazione libera è quindi preferita a un
attacco. Il Dash rappresenta esattamente tre celle e viene usato soltanto se il
percorso pesato inizierebbe con una piramide, non quando esiste un giro libero.
Le caselle azzurre completamente illuminate restano percorribili anche quando
la loro tonalità viola supera la soglia usata per le piramidi parziali.
Il percorso viene rivalutato dopo ogni singolo passo. La versione desktop non
invia batch ciechi: un nuovo screenshot deve mostrare anche i pickup appena
entrati nel campo visivo prima che venga autorizzata la mossa successiva.
La pausa di assestamento viene applicata soltanto dopo una raccolta effettiva,
non semplicemente quando sono visibili molti pickup contemporaneamente.

## Compilazione e installazione

Da Windows, fai doppio clic su `INSTALL_WINDOWS.cmd`. Lo script:

- crea un ambiente di build locale;
- installa NumPy, Pillow, RapidOCR, ONNX Runtime e PyInstaller;
- genera PNG/ICO dall'illustrazione trasparente validata;
- esegue tutti i test senza inviare tocchi ADB;
- compila un'app Windows senza console;
- installa i file sotto `%LOCALAPPDATA%\MechaLeomon\app`;
- crea il collegamento **MechaLeomon** sul desktop dell'utente.

L'installazione è per utente e non richiede UAC/amministratore. Un EXE non
firmato può comunque mostrare la normale reputazione SmartScreen su alcuni PC;
eliminare anche quella richiede una firma Authenticode acquistata e attendibile.

## Test

```powershell
.\.venv-build\Scripts\python.exe -m unittest discover -s tests -v
.\dist\MechaLeomon\MechaLeomon.exe --self-test .\self-test.json
```

Il self-test compilato carica icona, motore di riconoscimento, modelli OCR locali
e controlli di pausa/stop senza collegarsi ad ADB e senza inviare input.

## Pacchetto portabile

Dopo la build verificata, crea una nuova cartella versionata sul desktop Windows:

```powershell
$version = (Get-Content .\VERSION -Raw).Trim()
$destination = Join-Path ([Environment]::GetFolderPath('Desktop')) "MechaLeomon-portable-$version"
.\create_portable.ps1 -Destination $destination
```

La destinazione non deve esistere già. Verifica `VERSION.txt`,
`MechaLeomon.exe` e l’hash in `SHA256.txt`. Senza `-Destination`, lo
script usa il vecchio default `Desktop\prod`; non sovrascrivere pacchetti precedenti.

La cartella risultante puo essere copiata integralmente su un altro PC Windows.
L'utente avvia `MechaLeomon.exe`; Python, WSL e l'installazione del bot
non sono necessari. BlueStacks 5, Digimon UP e l'opzione Android Debug Bridge
attiva restano requisiti esterni.

## Analizza PvP

Il pulsante **Analizza PvP** apre una finestra separata. Se il worker è attivo,
richiede la pausa e attende il suo riscontro prima di abilitare la cattura.
Durante l'analisi non è possibile riprendere o cambiare modalità; **Ferma** resta
sempre disponibile. Alla chiusura riprende soltanto il medesimo worker che era
in esecuzione prima dell'analisi: una pausa manuale preesistente viene conservata.

1. Apri **Detailed Stats** del confronto con **You** e **Target** visibili, quindi
   premi **Scansiona automaticamente**. Se la lista è rimasta a metà, l'app
   torna prima a Combined ATK/DEF/HP verificando ogni avanzamento.
2. L'app cattura, esegue l'OCR locale e scorre poi la lista con swipe ADB checked,
   mantenendo righe sovrapposte fino a HP Recovery Amount. Non invia tap e non
   seleziona avversari. Ogni pagina deve conservare titolo e intestazioni.
   **Annulla scansione** interrompe tra due operazioni e conserva i dati letti;
   in caso di pagina ferma o non affidabile puoi sistemare la schermata e usare
   **Continua scansione** senza ricominciare.
3. Controlla You/Target: rosa indica campi mancanti o conflitti; giallo valori
   dubbi. Modifica il numero o usa **Conferma riga** dopo verifica visiva.
   I campi con asterisco sono obbligatori; vuoti e OCR fallito non diventano zero.
   Le percentuali sono nelle unità visualizzate: `102.41%` oppure `102.41`.
4. Premi **Calcola**. Puoi cambiare il coefficiente Skill (default 1) e ricalcolare
   senza catturare nuovamente. Il report presenta entrambe le direzioni,
   abbreviazioni e valori esatti selezionabili/copiabili. **Nuovo confronto**
   cancella i dati precedenti: usalo cambiando avversario o statistiche.

### Fonti e limiti

Motore puro in `pvp_damage.py`, modello/validazione in `pvp_stats.py`, OCR e
fusione pagine in `pvp_ocr.py`, finestra in `pvp_gui.py`.
Formule verificate il 18 settembre 2026 contro
[Combat Guide](https://digiupdata.com/reference/combat) e
[Formula Archive](https://digiupdata.com/research/formulas); ordine canonico dalla
[scheda delle statistiche](https://digiupdata.com/stats).
Si usa Combined ATK, senza strati Boss nel PvP, con arrotondamento finale.
Counter è una risposta dopo un colpo entrante idoneo, e non è schivabile;
la probabilità Counter nella colonna appartiene a chi infligge quel Counter.
Combo Chance DOWN resta escluso perché la sua applicazione non è verificata.
Il report non stima DPS o vincitore: scudi, Ignore Damage, Skill Cards, Buddies,
buff e stato live non sono disponibili dalla scheda.

I vecchi nomi Support vengono assegnati a Normal Attack soltanto quando
l'ordine e le righe vicine eliminano l'ambiguità. Righe isolate ambigue restano
mancanti e richiedono una nuova cattura sovrapposta o inserimento manuale.
Le catture OCR non vengono salvate né inviate a servizi cloud. Gli eventi della
scansione, privi dei valori riconosciuti, vengono registrati nella diagnostica.
La regressione Detailed Stats usa valori numerici di riferimento e geometrie OCR
sintetiche; non include catture reali. Font, traduzioni,
colonne disallineate, testo spezzato e overlay possono richiedere correzioni.

### OCR e distribuzione

Il backend isolato usa `rapidocr==3.9.0` (CPU), versione fissata
perché include i tre modelli ONNX nel wheel e non scarica modelli a runtime.
Non occorre installare Tesseract. La telemetria ONNX Runtime è disabilitata.
I percorsi dei tre modelli sono espliciti e vengono verificati prima di avviare
il backend: risorse mancanti producono un errore, senza download automatico.

La build autorevole raccoglie tutti i dati RapidOCR e le librerie ONNX Runtime.
Il self-test frozen esegue realmente OCR su testo sintetico locale, oltre ai
controlli esistenti; fallisce se mancano modelli o librerie native. I modelli e le librerie OCR vengono inclusi nella cartella frozen;
la portabile deve contenere l'intero bundle. Dimensioni e hash vanno verificati
sull'artefatto prodotto dalla propria build.

`pyinstaller_hooks/hook-shapely.py` preserva il nome reale `shapely.libs` del
wheel Windows: l'hook standard cerca `Shapely.libs`, che non viene trovato su
una share WSL con filesystem sensibile alle maiuscole. Le DLL GEOS sono
necessarie al rilevamento OCR e vengono incluse nel pacchetto, senza installazioni
esterne.
