"""Local PvP review window with checked, cancellable ADB scrolling."""
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from pvp_damage import report
from pvp_ocr import CaptureSession, LocalOCR, parse_page
from pvp_stats import LABELS, REQUIRED, Field, parse_number

PVP_SWIPE_DURATION_MS = 600
PVP_SWIPE_SETTLE_SECONDS = .25
PVP_RETRY_SECONDS = .15
PVP_POST_SWIPE_ATTEMPTS = 3
PVP_RECOVERY_SETTLE_SECONDS = .25
PVP_INITIAL_ROWS = frozenset((
    'Combined ATK', 'Combined DEF', 'Combined HP', 'ATK', 'DEF', 'HP',
))


class AnalyzerWindow:
    def __init__(self, owner):
        self.owner = owner
        self.window = tk.Toplevel(owner.root)
        self.window.title('Analizza PvP — revisione locale')
        self.window.geometry('1100x780')
        self.session = CaptureSession()
        self.backend = None
        self.adb = self.serial = None
        self.requested_serial = None
        self.busy = False
        self.closed = False
        self.close_requested = False
        self.cancel_event = threading.Event()
        self.scan_stop_marker = self._stop_marker()
        self.highest_scanned_index = -1
        self.scan_log = None
        self.results = queue.Queue()
        self.original_process = owner.process
        self.was_paused = owner.controls.pause_file.exists()
        self.had_worker = owner.process is not None and owner.process.poll() is None
        if self.had_worker:
            owner.controls.pause()
        self.deadline = time.monotonic()+30
        self.ready = False
        self.message = tk.StringVar(value='Attendo conferma della pausa del worker…')
        ttk.Label(self.window, textvariable=self.message, wraplength=1000).pack(fill='x',padx=12,pady=8)
        ttk.Label(self.window, text='Apri Detailed Stats e avvia la scansione: la lista tornerà automaticamente all’inizio.\n'
                  'Giallo = da verificare; rosa = mancante/conflitto. Modifica o conferma ogni dato dubbio. Percentuali in unità visualizzate.').pack(fill='x',padx=12)
        bar = ttk.Frame(self.window); bar.pack(fill='x',padx=12,pady=8)
        self.scan_button = ttk.Button(bar,text='Scansiona automaticamente',command=self.toggle_scan,state='disabled')
        self.scan_button.pack(side='left')
        ttk.Label(bar,text='Coefficiente Skill:').pack(side='left',padx=8)
        self.coefficient = tk.StringVar(value='1.0')
        ttk.Entry(bar,textvariable=self.coefficient,width=8).pack(side='left')
        ttk.Button(bar,text='Calcola',command=self.calculate).pack(side='left',padx=8)
        ttk.Button(bar,text='Nuovo confronto',command=self.reset).pack(side='left')
        notebook = ttk.Notebook(self.window); notebook.pack(fill='both',expand=True,padx=12,pady=8)
        review = ttk.Frame(notebook); notebook.add(review,text='Statistiche: You / Target')
        output = ttk.Frame(notebook); notebook.add(output,text='Risultati esatti (copiabili)')
        self.notebook, self.output_tab = notebook, output
        self.output = tk.Text(output,wrap='none',font=('Consolas',10))
        out_y = ttk.Scrollbar(output,orient='vertical',command=self.output.yview)
        out_x = ttk.Scrollbar(output,orient='horizontal',command=self.output.xview)
        self.output.configure(yscrollcommand=out_y.set,xscrollcommand=out_x.set)
        out_y.pack(side='right',fill='y'); out_x.pack(side='bottom',fill='x'); self.output.pack(fill='both',expand=True)
        canvas = tk.Canvas(review,highlightthickness=0)
        scroll = ttk.Scrollbar(review,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); canvas.pack(side='left',fill='both',expand=True)
        rows = ttk.Frame(canvas); canvas.create_window((0,0),window=rows,anchor='nw')
        rows.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        for col,text in enumerate(('Statistica (* richiesta)','You','Stato/confidenza','Target','Stato/confidenza','')):
            ttk.Label(rows,text=text).grid(row=0,column=col,padx=5,pady=5)
        self.entries = {}
        for row,label in enumerate(LABELS,1):
            ttk.Label(rows,text=f'{row:02} {label}'+(' *' if label in REQUIRED else '')).grid(row=row,column=0,sticky='w',padx=5)
            for side in range(2):
                variable = tk.StringVar()
                entry = tk.Entry(rows,textvariable=variable,width=20)
                entry.grid(row=row,column=1+side*2,padx=5,pady=2)
                status = ttk.Label(rows,width=20)
                status.grid(row=row,column=2+side*2)
                self.entries[side,label] = (variable,entry,status)
                entry.bind('<FocusOut>',lambda e,s=side,l=label:self.edit(s,l))
            ttk.Button(rows,text='Conferma riga',command=lambda l=label:self.confirm(l)).grid(row=row,column=5)
        self.refresh()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.after(100,self.poll)

    def refresh(self):
        for (side,label),(variable,entry,status) in self.entries.items():
            field = self.session.sheets[side].fields[label]
            variable.set(field.text)
            color = 'white' if field.status in ('recognized','manual') else '#fff0b3' if field.status == 'suspicious' else '#ffd9df'
            entry.configure(bg=color)
            status.configure(text=f'{field.status} {field.confidence:.0%}')

    def edit(self,side,label,force=False):
        variable,_,_ = self.entries[side,label]
        old = self.session.sheets[side].fields[label]
        if variable.get() == old.text and not force:
            return
        try:
            self.session.sheets[side].correct(label,variable.get())
        except ValueError:
            self.session.sheets[side].fields[label] = Field(variable.get(), 0, "suspicious")
            self.entries[side,label][1].configure(bg="#fff0b3")
            self.entries[side,label][2].configure(text="Formato non valido")
            return
        field = self.session.sheets[side].fields[label]
        self.entries[side,label][1].configure(bg='white')
        self.entries[side,label][2].configure(text=f'{field.status} 100%')

    def confirm(self,label):
        for side in range(2):
            self.edit(side,label,True)

    def reset(self):
        if self.busy:
            return
        self.session = CaptureSession()
        self.highest_scanned_index = -1
        self.scan_log = None
        self.refresh()
        self.output.delete('1.0','end')
        self.scan_button.configure(text='Scansiona automaticamente',state='normal')
        self.message.set('Nuovo confronto: apri Detailed Stats e avvia la scansione.')

    def toggle_scan(self):
        if self.busy:
            self.cancel_scan()
            return
        if not self.ready:
            return
        self.busy = True
        self.cancel_event.clear()
        # A stop.request left by a worker that has already terminated is
        # historical state, not a cancellation of this new scan.  Snapshot it
        # here so only a Stop issued after the click interrupts the scanner.
        self.scan_stop_marker = self._stop_marker()
        self.scan_button.configure(text='Annulla scansione',state='normal')
        # Commit typed corrections before merging new pages.
        for side,label in self.entries:
            self.edit(side,label)
        self.message.set('Scansione automatica e OCR locale in corso…')
        if self.scan_log is None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ_pvp')
            run_dir = self.owner.runs / stamp
            run_dir.mkdir(parents=True, exist_ok=False)
            self.scan_log = run_dir / 'events.jsonl'
        threading.Thread(target=self.scan_automatic,daemon=True).start()

    def cancel_scan(self):
        if not self.busy:
            return
        self.cancel_event.set()
        self.scan_button.configure(text='Annullamento…',state='disabled')
        self.message.set('Annullamento richiesto: concludo l’operazione in corso senza altri input.')

    def _stop_marker(self):
        try:
            stat = self.owner.controls.stop_file.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns

    def _continue_after(self, seconds):
        if self.cancel_event.is_set() or self.closed or self.owner.closing:
            return False
        if self._stop_marker() != self.scan_stop_marker:
            return False
        return not self.cancel_event.wait(max(0.0, seconds))

    def _log(self, event):
        import digiworld_bot as bot
        if self.scan_log is None:
            return
        payload = {'time_utc': datetime.now(timezone.utc).isoformat(), **event}
        bot.log_event(self.scan_log, payload)

    def _capture_page(self, bot, backend, after_swipe):
        attempts = PVP_POST_SWIPE_ATTEMPTS if after_swipe else 1
        last_error = None
        for attempt in range(attempts):
            if not self._continue_after(0):
                return None
            image = bot.screenshot(self.adb,self.serial)
            try:
                return parse_page(backend.read(image))
            except ValueError as exc:
                last_error = exc
                if attempt + 1 >= attempts or not self._continue_after(PVP_RETRY_SECONDS):
                    break
        raise last_error

    def _dispatch_scroll(self, bot, geometry, action):
        dispatch = bot.dispatch_swipe_with_recovery(
            self.adb, self.serial,
            geometry.x, geometry.start_y, geometry.x, geometry.end_y,
            PVP_SWIPE_DURATION_MS, action=action,
            requested_serial=self.requested_serial,
            should_continue=self._continue_after,
            logger=lambda event: self._log({'adb_recovery': event}),
            recovery_settle_seconds=PVP_RECOVERY_SETTLE_SECONDS,
        )
        self.serial = dispatch.serial
        self._log({
            'status': 'swipe_result',
            'action': action,
            'outcome': dispatch.outcome,
            'serial': dispatch.serial,
            'adb_swipe': [geometry.x, geometry.start_y,
                          geometry.x, geometry.end_y,
                          PVP_SWIPE_DURATION_MS],
        })
        return dispatch

    def scan_automatic(self):
        started = time.monotonic()
        highest = self.highest_scanned_index
        seen_labels = {
            label for label in LABELS
            if any(sheet.fields[label].status != 'missing'
                   for sheet in self.session.sheets)
        }
        try:
            import digiworld_bot as bot
            if self.adb is None:
                self.adb = bot.resolve_adb()
                self.requested_serial = bot.requested_serial_value()
                self.serial = bot.resolve_serial(self.adb,self.requested_serial)
            if self.backend is None:
                self.backend = LocalOCR()
            after_swipe = False
            seeking_top = highest < 0
            previous_rewind_index = None
            repair_passes = 0
            while self._continue_after(0):
                page = self._capture_page(bot, self.backend, after_swipe)
                if page is None:
                    break
                indices = [LABELS.index(label) for label in page.visible_labels]
                scroll_indices = [
                    LABELS.index(label) for label in page.visible_labels
                    if not label.startswith('Combined ')
                ]
                page_lowest = min(scroll_indices, default=len(LABELS))
                page_highest = max(indices, default=-1)
                if seeking_top and not PVP_INITIAL_ROWS.issubset(page.rows):
                    if (previous_rewind_index is not None
                            and page_lowest >= previous_rewind_index):
                        raise ValueError(
                            'Detailed Stats non è tornata all’inizio. Dati invariati; '
                            'nessun altro swipe è stato inviato.')
                    if page.rewind is None:
                        raise ValueError(
                            'Detailed Stats riconosciuta, ma la geometria per tornare '
                            'all’inizio non è affidabile.')
                    self._log({
                        'status': 'rewind_page',
                        'visible_labels': list(page.visible_labels),
                        'lowest_label_index': page_lowest,
                    })
                    previous_rewind_index = page_lowest
                    dispatch = self._dispatch_scroll(
                        bot, page.rewind, 'pvp_stats_rewind')
                    if dispatch.outcome == 'failed':
                        raise RuntimeError(
                            'Connessione ADB non recuperata durante il ritorno '
                            'all’inizio. Lo swipe non è stato reinviato.')
                    if dispatch.outcome == 'stopped':
                        break
                    if (dispatch.outcome == 'sent'
                            and not self._continue_after(PVP_SWIPE_SETTLE_SECONDS)):
                        break
                    after_swipe = True
                    continue
                seeking_top = False
                previous_rewind_index = None
                self.results.put(('page', page))
                seen_labels.update(page.rows)
                self._log({
                    'status': 'page_scanned',
                    'visible_labels': list(page.visible_labels),
                    'highest_label_index': page_highest,
                })
                if page_highest <= highest:
                    self.results.put(('stopped',
                        'La schermata non è avanzata. Dati conservati: puoi sistemare la lista e continuare.',
                        highest, self.serial, time.monotonic()-started))
                    return
                highest = page_highest
                if page.final:
                    missing = [label for label in REQUIRED
                               if label not in seen_labels]
                    if missing and repair_passes == 0:
                        repair_passes = 1
                        seeking_top = True
                        highest = -1
                        after_swipe = False
                        self._log({
                            'status': 'repair_pass_required',
                            'missing_required_labels': missing,
                        })
                        continue
                    if missing:
                        self.results.put(('stopped',
                            'Scansione incompleta dopo il controllo: mancano '
                            + ', '.join(missing) + '.',
                            highest, self.serial, time.monotonic()-started))
                        return
                    self.results.put(('complete',
                        'Scansione completata: verifica i campi evidenziati.',
                        highest, self.serial, time.monotonic()-started))
                    return
                if page.swipe is None:
                    self.results.put(('stopped',
                        'Geometria di scorrimento non affidabile. Dati conservati: puoi continuare dalla schermata corrente.',
                        highest, self.serial, time.monotonic()-started))
                    return
                geometry = page.swipe
                dispatch = self._dispatch_scroll(
                    bot, geometry, 'pvp_stats_scroll')
                if dispatch.outcome == 'failed':
                    self.results.put(('error',
                        'Connessione ADB non recuperata. Nessuno swipe è stato reinviato.',
                        highest, self.serial, time.monotonic()-started))
                    return
                if dispatch.outcome == 'stopped':
                    break
                if dispatch.outcome == 'sent' and not self._continue_after(PVP_SWIPE_SETTLE_SECONDS):
                    break
                after_swipe = True
            self.results.put(('cancelled',
                'Scansione annullata. I dati già letti sono stati conservati.',
                highest, self.serial, time.monotonic()-started))
        except Exception as exc:  # noqa: BLE001 — ogni errore di cattura/OCR deve arrivare alla GUI, mai lasciare un thread morto.
            self._log({'status': 'scan_error', 'error_type': type(exc).__name__,
                       'error': str(exc)})
            self.results.put(('error', str(exc), highest, self.serial,
                              time.monotonic()-started))

    def poll(self):
        if self.closed:
            return
        owner = self.owner
        if not self.ready:
            alive = owner.process is not None and owner.process.poll() is None
            if not alive or owner.controls.pause_ack_file.exists():
                self.ready = True
                self.scan_button.configure(state='normal')
                self.message.set('Automazione sospesa. Apri Detailed Stats e avvia la scansione.')
            elif time.monotonic() > self.deadline:
                self.message.set('Pausa non confermata: cattura bloccata. Ferma il worker per continuare.')
        try:
            event = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            kind = event[0]
            if kind == 'page':
                page = event[1]
                for side,label in self.entries:
                    self.edit(side,label)
                added = self.session.merge(page)
                self.refresh()
                self.message.set(f'Pagina {self.session.pages}: {added} nuovi valori; scansione in corso…')
            else:
                _, message, highest, serial, elapsed = event
                self.busy = False
                self.highest_scanned_index = max(self.highest_scanned_index, highest)
                self.serial = serial
                self.cancel_event.clear()
                self.message.set(f'{message} Tempo: {elapsed:.1f} s.')
                if kind == 'complete':
                    self.scan_button.configure(text='Scansione completata',state='disabled')
                else:
                    self.scan_button.configure(text='Continua scansione',state='normal')
                if self.close_requested:
                    self.close()
                    return
        self.window.after(100,self.poll)

    def calculate(self):
        try:
            for (side,label),(variable,_,_) in self.entries.items():
                self.edit(side,label)
                if label in REQUIRED and variable.get() != self.session.sheets[side].fields[label].text:
                    raise ValueError(f'Numero non valido: {label}')
            values = []
            for name,sheet in zip(('You','Target'),self.session.sheets):
                try:
                    values.append(sheet.values())
                except ValueError as exc:
                    raise ValueError(f'{name}: {exc}') from exc
            coefficient = parse_number(self.coefficient.get())
            text = report(*values,coefficient)
        except ValueError as exc:
            messagebox.showerror('Verifica statistiche',str(exc),parent=self.window)
            return
        self.output.delete('1.0','end'); self.output.insert('1.0',text)
        self.notebook.select(self.output_tab)

    def close(self):
        if self.busy:
            self.close_requested = True
            self.cancel_scan()
            self.message.set('Chiusura richiesta: attendo la fine sicura dell’operazione in corso.')
            return
        self.closed = True
        owner = self.owner
        resumed = (self.had_worker and not self.was_paused
                   and owner.process is self.original_process
                   and owner.process.poll() is None
                   and not owner.controls.stop_file.exists() and not owner.closing)
        if resumed:
            owner.controls.resume()
        owner.pvp_window = None
        self.window.destroy()
        if resumed:
            owner.set_state('running', 'Automazione ripresa dopo l’analisi PvP.')
        else:
            owner.set_state(owner.state, 'Analizzatore chiuso; stato precedente della pausa preservato.')
