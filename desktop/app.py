"""Compact Windows controller for MechaLeomon.

Normal launch opens the window and starts the automation automatically. The
same frozen executable is started with ``--worker`` for the ADB loop, keeping
Tk responsive and making pause/stop explicit file-based safety controls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from control import ControlFiles, parse_status_line

APP_NAME = "MechaLeomon"
APP_VERSION = Path(__file__).with_name("VERSION").read_text(
    encoding="utf-8").strip()
MAX_ACTIONS = 1_000_000
STATE_COLORS = {
    "starting": "#f5a623",
    "connected": "#20c997",
    "running": "#28d17c",
    "paused": "#ffb020",
    "stopping": "#ff7a45",
    "stopped": "#8b9bb4",
    "complete": "#28d17c",
    "error": "#ff5d73",
}
EXIT_MESSAGES = {
    2: "Il DigiWorld non è stato riconosciuto. Apri la griglia nel gioco e riprova.",
    3: "Il personaggio non è stato riconosciuto con sicurezza.",
    4: "Nessuna mossa sicura: automazione fermata.",
    5: "BlueStacks non ha applicato cinque movimenti consecutivi. Automazione fermata in sicurezza.",
    10: "BlueStacks/ADB non è disponibile. Avvia BlueStacks e abilita ADB nelle impostazioni avanzate.",
    11: ("Connessione ADB a BlueStacks interrotta. Riconnessione non riuscita. "
         "Riavvia BlueStacks o premi Riavvia."),
}


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "MechaLeomon"
    return Path.home() / ".mechaleomon"


def worker_command(controls: ControlFiles, runs: Path,
                   mode: str = "digiworld") -> list[str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--worker"]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    command += [
        "--steps", str(MAX_ACTIONS),
        "--interval", "0.70",
        "--batch-size", "1",
        "--min-confidence", "0.80",
        "--pause-file", str(controls.pause_file),
        "--stop-file", str(controls.stop_file),
        "--out", str(runs),
    ]
    if mode == "auto_pull_10x":
        command.append("--auto-pull-10x")
    elif mode == "auto_pull_35x":
        command.append("--auto-pull-35x")
    elif mode != "digiworld":
        raise ValueError(f"modalità sconosciuta: {mode}")
    return command


def acquire_windows_mutex() -> object | None:
    if os.name != "nt":
        return object()
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\MechaLeomonDesktop")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


class ExplorerWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base = data_root()
        self.controls = ControlFiles(self.base / "control")
        self.runs = self.base / "runs"
        self.log_path = self.base / "worker.log"
        self.process: subprocess.Popen | None = None
        self.log_stream = None
        self.status_lines_seen = 0
        self.state = "stopped"
        self.closing = False
        self.restart_requested = False
        self.mode = "digiworld"
        self.pvp_window = None

        self.base.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(500, self.start)
        self.root.after(150, self.poll)

    def _build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.configure(bg="#091426")
        self.root.resizable(False, False)
        width, height = 430, 365
        x = max(0, self.root.winfo_screenwidth() - width - 24)
        y = max(0, self.root.winfo_screenheight() - height - 72)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        icon_path = resource_path("assets/mechaleomon_icon.png")
        self.icon_image = None
        self.mascot_image = None
        if icon_path.exists():
            try:
                self.icon_image = tk.PhotoImage(file=str(icon_path))
                self.root.iconphoto(True, self.icon_image)
                factor = max(1, self.icon_image.width() // 72)
                self.mascot_image = self.icon_image.subsample(factor, factor)
            except tk.TclError:
                pass

        header = tk.Frame(self.root, bg="#091426")
        header.pack(fill="x", padx=18, pady=(16, 8))
        if self.mascot_image is not None:
            tk.Label(header, image=self.mascot_image, bg="#091426").pack(side="left", padx=(0, 12))
        title_box = tk.Frame(header, bg="#091426")
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text=f"{APP_NAME}  v{APP_VERSION}",
                 bg="#091426", fg="#f6fbff",
                 font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(title_box, text="Controllo locale • BlueStacks via ADB", bg="#091426",
                 fg="#7f9cbe", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        card = tk.Frame(self.root, bg="#101f35", highlightbackground="#203b5b",
                        highlightthickness=1)
        card.pack(fill="x", padx=18, pady=4)
        row = tk.Frame(card, bg="#101f35")
        row.pack(fill="x", padx=14, pady=(13, 4))
        self.dot = tk.Label(row, text="●", bg="#101f35", fg=STATE_COLORS["starting"],
                            font=("Segoe UI", 13))
        self.dot.pack(side="left")
        self.state_label = tk.Label(row, text="Avvio…", bg="#101f35", fg="#f6fbff",
                                    font=("Segoe UI Semibold", 11))
        self.state_label.pack(side="left", padx=(6, 0))
        self.counter_label = tk.Label(row, text="0 azioni", bg="#101f35", fg="#79d9ff",
                                      font=("Segoe UI", 10))
        self.counter_label.pack(side="right")
        self.message_label = tk.Label(card, text="Preparazione dell’automazione…", bg="#101f35",
                                      fg="#aabbd0", font=("Segoe UI", 9), anchor="w",
                                      justify="left", wraplength=370)
        self.message_label.pack(fill="x", padx=14, pady=(0, 13))

        mode_buttons = tk.Frame(self.root, bg="#091426")
        mode_buttons.pack(fill="x", padx=18, pady=(10, 2))
        self.pull_10x_button = tk.Button(
            mode_buttons, text="Auto Pull 10x",
            command=lambda: self.switch_mode("auto_pull_10x"),
            bg="#6e4bd8", activebackground="#8061e6", fg="white",
            activeforeground="white", relief="flat", bd=0,
            font=("Segoe UI Semibold", 10), cursor="hand2", pady=7,
        )
        self.pull_10x_button.pack(
            side="left", fill="x", expand=True, padx=(0, 5))
        self.pull_35x_button = tk.Button(
            mode_buttons, text="Auto Pull 35x",
            command=lambda: self.switch_mode("auto_pull_35x"),
            bg="#6e4bd8", activebackground="#8061e6", fg="white",
            activeforeground="white", relief="flat", bd=0,
            font=("Segoe UI Semibold", 10), cursor="hand2", pady=7,
        )
        self.pull_35x_button.pack(
            side="left", fill="x", expand=True, padx=(5, 0))

        tk.Button(self.root, text="Analizza PvP", command=self.open_pvp,
                  bg="#203b5b", fg="white", relief="flat").pack(fill="x", padx=18, pady=4)

        buttons = tk.Frame(self.root, bg="#091426")
        buttons.pack(fill="x", padx=18, pady=(8, 4))
        self.pause_button = tk.Button(
            buttons, text="Pausa", command=self.toggle_pause, state="disabled",
            bg="#00a9d6", activebackground="#16bce9", fg="white",
            activeforeground="white", relief="flat", bd=0,
            font=("Segoe UI Semibold", 11), cursor="hand2", padx=24, pady=8,
        )
        self.pause_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.restart_button = tk.Button(
            buttons, text="Riavvia", command=self.restart,
            bg="#f5a623", activebackground="#ffb942", fg="#091426",
            activeforeground="#091426", relief="flat", bd=0,
            font=("Segoe UI Semibold", 11), cursor="hand2", padx=16, pady=8,
        )
        self.restart_button.pack(side="left", fill="x", expand=True, padx=6)
        self.stop_button = tk.Button(
            buttons, text="Ferma", command=self.stop, state="disabled",
            bg="#243851", activebackground="#304b6c", fg="#f4f7fb",
            activeforeground="white", relief="flat", bd=0,
            font=("Segoe UI Semibold", 11), cursor="hand2", padx=16, pady=8,
        )
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.folder_button = tk.Button(
            self.root, text="Apri diagnostica", command=self.open_runs,
            bg="#091426", activebackground="#091426", fg="#6f8cab",
            activeforeground="#9edfff", relief="flat", bd=0,
            font=("Segoe UI", 8, "underline"), cursor="hand2",
        )
        self.folder_button.pack(anchor="e", padx=18, pady=(2, 0))

    def set_state(self, state: str, message: str, done: int | None = None) -> None:
        self.state = state
        labels = {
            "starting": "Avvio…", "connected": "Collegato", "running": "In esecuzione",
            "paused": "In pausa", "stopping": "Arresto…", "stopped": "Fermato",
            "complete": "Completato", "error": "Attenzione",
        }
        self.dot.configure(fg=STATE_COLORS.get(state, "#8b9bb4"))
        self.state_label.configure(text=labels.get(state, state.capitalize()))
        self.message_label.configure(text=message)
        if done is not None:
            self.counter_label.configure(text=f"{done:,} azioni".replace(",", "."))
        active = state in {"starting", "connected", "running", "paused"}
        self.pause_button.configure(state="normal" if active else "disabled")
        self.stop_button.configure(state="normal" if active else "disabled")
        self.restart_button.configure(
            state="disabled" if self.restart_requested or self.closing else "normal"
        )
        self.pause_button.configure(text="Riprendi" if state == "paused" else "Pausa")
        mode_buttons_state = (
            "disabled" if self.restart_requested or self.closing else "normal"
        )
        self.pull_10x_button.configure(
            text=("Torna a DigiWorld" if self.mode == "auto_pull_10x"
                  else "Passa a 10x" if self.mode == "auto_pull_35x"
                  else "Auto Pull 10x"),
            state=mode_buttons_state,
        )
        self.pull_35x_button.configure(
            text=("Torna a DigiWorld" if self.mode == "auto_pull_35x"
                  else "Passa a 35x" if self.mode == "auto_pull_10x"
                  else "Auto Pull 35x"),
            state=mode_buttons_state,
        )
        if getattr(self, "pvp_window", None) is not None:
            for button in (self.pause_button, self.restart_button, self.pull_10x_button, self.pull_35x_button):
                button.configure(state="disabled")

    def open_pvp(self) -> None:
        if self.closing or self.restart_requested:
            return
        if self.pvp_window is not None:
            self.pvp_window.window.lift()
            return
        from pvp_gui import AnalyzerWindow
        self.pvp_window = AnalyzerWindow(self)
        self.set_state(self.state, "Analizzatore aperto: automazione sospesa.")

    def start(self) -> None:
        if getattr(self, "pvp_window", None) is not None:
            self.root.after(500, self.start)
            return
        if self.process is not None:
            if self.process.poll() is None:
                return
            self.process = None
            if self.log_stream is not None:
                self.log_stream.close()
                self.log_stream = None
        self.controls.reset()
        self.status_lines_seen = 0
        starting_message = (
            f"Avvio della modalità Auto Pull {self.mode.removeprefix('auto_pull_')}…"
            if self.mode.startswith("auto_pull_") else
            "Connessione a BlueStacks…"
        )
        self.set_state("starting", starting_message, 0)
        command = worker_command(self.controls, self.runs, self.mode)
        environment = os.environ.copy()
        environment["DIGIWORLD_STATUS_FILE"] = str(self.controls.status_file)
        environment["PYTHONUTF8"] = "1"
        self.log_stream = self.log_path.open("ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                command, cwd=str(self.base), env=environment,
                stdin=subprocess.DEVNULL, stdout=self.log_stream,
                stderr=subprocess.STDOUT, creationflags=flags,
            )
        except OSError as exc:
            self.log_stream.close()
            self.log_stream = None
            self.process = None
            self.set_state("error", f"Impossibile avviare il bot: {exc}", 0)

    def toggle_pause(self) -> None:
        if getattr(self, "pvp_window", None) is not None:
            return
        if self.state == "paused":
            self.controls.resume()
            self.set_state("running", "Ripresa richiesta…")
        elif self.process is not None and self.process.poll() is None:
            self.controls.pause()
            self.set_state("paused", "Nessun nuovo comando verrà inviato finché non riprendi.")

    def stop(self) -> None:
        if getattr(self, "pvp_window", None) is not None:
            self.pvp_window.cancel_scan()
        if self.process is not None and self.process.poll() is None:
            self.controls.stop()
            self.set_state("stopping", "Arresto sicuro in corso…")

    def restart(self) -> None:
        """Stop the current worker and start a clean one without closing the UI."""
        if self.closing or self.restart_requested or getattr(self, "pvp_window", None) is not None:
            return
        if self.process is None or self.process.poll() is not None:
            self.restart_requested = False
            self.start()
            return
        self.restart_requested = True
        self.controls.stop()
        self.set_state("stopping", "Riavvio dell’automazione in corso…")
        self.root.after(3000, self._force_restart)

    def switch_mode(self, selected_mode: str) -> None:
        """Safely switch to one explicit pull mode or back to DigiWorld."""
        if self.closing or self.restart_requested or getattr(self, "pvp_window", None) is not None:
            return
        if selected_mode not in {"auto_pull_10x", "auto_pull_35x"}:
            raise ValueError(f"modalità Auto Pull sconosciuta: {selected_mode}")
        self.mode = "digiworld" if self.mode == selected_mode else selected_mode
        target = {
            "digiworld": "bot DigiWorld",
            "auto_pull_10x": "Auto Pull 10x",
            "auto_pull_35x": "Auto Pull 35x",
        }[self.mode]
        if self.process is None or self.process.poll() is not None:
            self.start()
            return
        self.restart_requested = True
        self.controls.stop()
        self.set_state("stopping", f"Passaggio a {target} in corso…")
        self.root.after(3000, self._force_restart)

    def _force_restart(self) -> None:
        """Terminate a worker that cannot observe the safe stop request."""
        if (self.restart_requested and not self.closing and
                self.process is not None and self.process.poll() is None):
            self.process.terminate()

    def open_runs(self) -> None:
        self.runs.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(self.runs)  # type: ignore[attr-defined]

    def _read_status(self) -> None:
        if not self.controls.status_file.exists():
            return
        try:
            lines = self.controls.status_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return
        for line in lines[self.status_lines_seen:]:
            payload = parse_status_line(line)
            if payload:
                self.set_state(payload["state"], str(payload.get("message", "")),
                               int(payload.get("done", 0)))
        self.status_lines_seen = len(lines)

    def poll(self) -> None:
        self._read_status()
        if self.process is not None:
            return_code = self.process.poll()
            if return_code is not None:
                if self.log_stream is not None:
                    self.log_stream.close()
                    self.log_stream = None
                self.process = None
                if self.restart_requested and not self.closing:
                    self.restart_requested = False
                    self.set_state("starting", "Riconnessione a BlueStacks…", 0)
                    self.root.after(150, self.start)
                elif self.state not in {"stopped", "complete"}:
                    if return_code == 0:
                        self.set_state("stopped", "Automazione fermata.")
                    else:
                        self.set_state("error", EXIT_MESSAGES.get(
                            return_code, f"Il bot si è fermato (codice {return_code}). Consulta la diagnostica."))
        if self.closing and self.process is None:
            self.root.destroy()
            return
        self.root.after(150, self.poll)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.restart_requested = False
        if self.process is None:
            self.root.destroy()
        else:
            self.stop()
            self.root.after(3000, self._force_close)

    def _force_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.root.destroy()


def run_self_test(output: Path) -> int:
    """Exercise bundled imports and control behavior without ADB input or a GUI."""
    import auto_digiworld as strategy
    import auto_pull
    import digiworld_bot as bot
    from auto_digiworld_batch2 import controlled_wait
    from pvp_damage import rounded
    from pvp_ocr import self_test as pvp_ocr_self_test

    with tempfile.TemporaryDirectory(prefix="mechaleomon-selftest-") as temp:
        controls = ControlFiles(Path(temp))
        controls.reset()
        checks = {
            "icon": resource_path("assets/mechaleomon_icon.png").is_file(),
            "center": bot.cell_center((100, 200, 600, 700), 4, 4) == (550, 650),
            "wait": controlled_wait(controls.pause_file, controls.stop_file, 0.001),
            "strategy": callable(strategy.choose),
            "auto_pull": callable(auto_pull.run),
            "pvp_math": rounded(0.5) == 1,
            "pvp_ocr": pvp_ocr_self_test(),
        }
    try:
        root = tk.Tk()
        root.withdraw()
        icon = tk.PhotoImage(file=str(resource_path("assets/mechaleomon_icon.png")))
        root.iconphoto(True, icon)
        root.update_idletasks()
        root.destroy()
        checks["tk"] = True
    except tk.TclError:
        checks["tk"] = False
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(checks, indent=2), encoding="utf-8")
    return 0 if all(checks.values()) else 1


def main() -> int:
    if "--worker" in sys.argv:
        sys.argv.remove("--worker")
        import auto_digiworld_batch2 as worker
        import digiworld_bot as bot
        try:
            return int(worker.main() or 0)
        except (bot.AdbInputError, subprocess.SubprocessError, OSError) as exc:
            # Last-resort boundary for frozen PyInstaller workers: ADB/pipe
            # errors must become a GUI status and exit code, never its
            # "Unhandled exception in script" dialog.
            worker.emit_status("error", worker.ADB_RECOVERY_MESSAGE)
            if sys.stderr is not None:
                print(f"ADB worker stopped safely: {exc}", file=sys.stderr)
            return worker.ADB_RECOVERY_EXIT
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        if index + 1 >= len(sys.argv):
            return 64
        return run_self_test(Path(sys.argv[index + 1]))

    mutex = acquire_windows_mutex()
    if mutex is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "MechaLeomon è già aperto.")
        root.destroy()
        return 0
    root = tk.Tk()
    ExplorerWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
