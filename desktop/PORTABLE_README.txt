MECHALEOMON - VERSIONE PORTABILE
================================

AVVIO
1. Copia l'intera cartella "prod" sul PC Windows di destinazione.
2. Installa e avvia BlueStacks 5 con Digimon UP.
3. Apri il DigiWorld nel gioco.
4. Fai doppio clic su "MechaLeomon.exe".

Non servono Python, WSL o un'installazione del programma. Non separare
l'eseguibile dalla cartella "_internal": contiene i componenti necessari.

REQUISITI ESTERNI
- Windows 10/11 a 64 bit.
- BlueStacks 5 e Digimon UP già installati.
- In BlueStacks: Impostazioni > Avanzate > Android Debug Bridge attivo.

I log delle esecuzioni vengono salvati in:
%LOCALAPPDATA%\MechaLeomon\runs

Versione: vedere VERSION.txt

ANALIZZA PVP
Il pulsante "Analizza PvP" apre il confronto locale You/Target.
Attendi la pausa del bot, apri Detailed Stats con le intestazioni visibili e
premi "Scansiona automaticamente". L'app torna da sola a Combined ATK/DEF/HP,
poi acquisisce e scorre fino alle righe HP Recovery; non seleziona avversari e non
invia tap. Puoi annullare conservando i dati gia letti o continuare dopo aver
sistemato una schermata non affidabile. Verifica o correggi i campi evidenziati,
quindi premi "Calcola". Il coefficiente Skill e' modificabile senza nuove catture.
L'OCR e i modelli sono inclusi: non servono Tesseract o servizi cloud.
Alla chiusura viene preservata una pausa manuale preesistente. Se il bot era
in esecuzione prima dell'analisi, riprende; usa "Ferma" se vuoi lasciarlo fermo.
I risultati sono statici: scudi, Ignore Damage, buff e stato live non sono noti.
