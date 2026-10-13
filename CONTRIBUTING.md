# Contribuire

Apri una issue descrivendo versione, ambiente Windows/BlueStacks, comportamento
atteso, comportamento osservato e passi riproducibili. Rimuovi dati personali da
log e immagini. Non allegare cartelle runtime complete o credenziali.

Per una pull request:

1. Parti dal branch `main` e limita la modifica a un problema identificabile.
2. Aggiungi una regressione sintetica che riproduca la causa del problema.
3. Esegui i controlli descritti nel README e riporta risultati e test saltati.
4. Descrivi il comportamento modificato e gli eventuali limiti.

Le fixture visive reali non sono distribuite. Non sostituire i test mancanti con
asserzioni sempre vere e non pubblicare screenshot di altri giocatori.
Le fixture numeriche PvP non identificano account.

Mantieni checked e timeout dei comandi ADB, controlli Pausa/Ferma e gestione degli
input ambigui. Un input ambiguo richiede una nuova osservazione, senza reinvio
automatico. Non cambiare strategia per compensare un errore del detector.

La versione è definita in `desktop/VERSION`. Una modifica documentale non richiede
un incremento di versione né una build Windows. Per modifiche funzionali indica
nella pull request quali verifiche richiedono Windows e quali sono state eseguite.
