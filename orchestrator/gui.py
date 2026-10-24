"""Interfaz gráfica (tkinter) de idm-clon.

Usa el mismo motor que la línea de comandos (orchestrator.download): la
descarga corre en un hilo aparte y lanza un core_downloader por fragmento.
Los hilos de descarga nunca tocan los widgets; solo actualizan contadores
(GuiProgress) y dejan mensajes en una cola, que la GUI lee cada 100 ms.

Uso:
  python orchestrator/gui.py
"""

import ctypes
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import orchestrator as core

THREAD_CHOICES = ("1", "2", "4", "8", "16")
POLL_MS = 100
ACCENT = "#0078D4"
ACCENT_HOVER = "#106EBE"
DANGER = "#C42B1C"
DANGER_HOVER = "#A4262C"
DISABLED = "#8A8A8A"


class GuiProgress:
    """Sustituto de core.ProgressBar: guarda el avance para que la GUI lo dibuje."""

    def __init__(self, app, total, done):
        self.app = app
        self.total = total
        self.done = list(done)
        self.resumed = sum(done)
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def __enter__(self):
        self.app.progress = self  # la GUI crea las barras en su próximo sondeo
        return self

    def __exit__(self, *exc):
        pass

    def update(self, index, nbytes):
        with self.lock:
            self.done[index] = nbytes

    def log(self, message):
        self.app.events.put(("log", message.strip(), False))

    def snapshot(self):
        with self.lock:
            return list(self.done)


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.active = None
        self.progress = None
        self.part_bars = []
        self.cancelled = False

        root.title("idm-clon")
        root.geometry("720x620")
        root.minsize(600, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._setup_style()
        self._build()
        root.after(POLL_MS, self.poll)

    # ---------- construcción ----------

    def _setup_style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        base = ("Segoe UI", 10)
        self.root.option_add("*Font", base)
        style.configure(".", font=base)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Sub.TLabel", foreground="#5F5F5F")
        style.configure("Stats.TLabel", font=("Segoe UI", 10), foreground="#333333")
        style.configure("Part.TLabel", font=("Consolas", 9), foreground="#5F5F5F")
        style.configure("Global.Horizontal.TProgressbar", thickness=22)

    def _build(self):
        main = ttk.Frame(self.root, padding=(20, 16))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        ttk.Label(main, text="idm-clon", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(main, text="Descargas multiparte con core_downloader (C++/libcurl)",
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 12))

        # URL
        ttk.Label(main, text="URL del archivo").grid(row=2, column=0, sticky="w")
        url_row = ttk.Frame(main)
        url_row.grid(row=3, column=0, sticky="ew", pady=(2, 10))
        url_row.columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew", ipady=4)
        self.url_entry.bind("<Return>", lambda e: self.on_start())
        self.paste_btn = ttk.Button(url_row, text="Pegar", command=self.paste_url)
        self.paste_btn.grid(row=0, column=1, padx=(8, 0))

        # Hilos y carpeta de destino
        opts = ttk.Frame(main)
        opts.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        opts.columnconfigure(3, weight=1)
        ttk.Label(opts, text="Hilos").grid(row=0, column=0, sticky="w")
        self.threads_var = tk.StringVar(value="8")
        self.threads_combo = ttk.Combobox(opts, textvariable=self.threads_var, values=THREAD_CHOICES,
                                          state="readonly", width=5)
        self.threads_combo.grid(row=0, column=1, padx=(8, 20), ipady=2)
        ttk.Label(opts, text="Guardar en").grid(row=0, column=2, sticky="w")
        self.dir_var = tk.StringVar(value=str(default_download_dir()))
        self.dir_entry = ttk.Entry(opts, textvariable=self.dir_var)
        self.dir_entry.grid(row=0, column=3, sticky="ew", padx=(8, 8), ipady=2)
        self.browse_btn = ttk.Button(opts, text="Examinar…", command=self.browse_dir)
        self.browse_btn.grid(row=0, column=4)

        # Botón principal (tk.Button para poder darle color)
        self.start_btn = tk.Button(main, text="Iniciar Descarga", command=self.on_start,
                                   font=("Segoe UI Semibold", 13), fg="white", bg=ACCENT,
                                   activeforeground="white", activebackground=ACCENT_HOVER,
                                   relief="flat", bd=0, cursor="hand2", pady=10)
        self.start_btn.grid(row=5, column=0, sticky="ew", pady=(0, 14))
        self.start_btn.bind("<Enter>", lambda e: self._hover(True))
        self.start_btn.bind("<Leave>", lambda e: self._hover(False))

        # Progreso global
        self.global_bar = ttk.Progressbar(main, style="Global.Horizontal.TProgressbar", maximum=1000)
        self.global_bar.grid(row=6, column=0, sticky="ew")
        self.stats_var = tk.StringVar(value="Listo.")
        ttk.Label(main, textvariable=self.stats_var, style="Stats.TLabel").grid(
            row=7, column=0, sticky="w", pady=(4, 10))

        # Barras por fragmento
        self.parts_frame = ttk.LabelFrame(main, text="Fragmentos", padding=(10, 6))
        self.parts_frame.grid(row=8, column=0, sticky="ew")
        self.parts_frame.columnconfigure(1, weight=1)
        self.parts_frame.columnconfigure(4, weight=1)
        self.parts_placeholder = ttk.Label(self.parts_frame, text="—", style="Sub.TLabel")
        self.parts_placeholder.grid(row=0, column=0, sticky="w")

        # Registro
        main.rowconfigure(9, weight=1)
        log_frame = ttk.Frame(main)
        log_frame.grid(row=9, column=0, sticky="nsew", pady=(12, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=6, font=("Consolas", 9), relief="flat",
                                bg="#F3F3F3", fg="#333333", wrap="word", state="disabled",
                                padx=8, pady=6)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("error", foreground=DANGER)
        self.log_text.tag_configure("ok", foreground="#107C10")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text["yscrollcommand"] = scroll.set

        self.url_entry.focus_set()

    def _hover(self, inside):
        if self.start_btn["state"] == "disabled":
            return
        busy = self.worker is not None
        if busy:
            self.start_btn["bg"] = DANGER_HOVER if inside else DANGER
        else:
            self.start_btn["bg"] = ACCENT_HOVER if inside else ACCENT

    # ---------- acciones ----------

    def paste_url(self):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_var.set(text)

    def browse_dir(self):
        folder = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if folder:
            self.dir_var.set(folder)

    def on_start(self):
        if self.worker is not None:
            self.on_cancel()
            return
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("idm-clon", "Introduce una URL que empiece por http:// o https://")
            return
        out_dir = Path(self.dir_var.get().strip() or ".")
        if not out_dir.is_dir():
            messagebox.showwarning("idm-clon", f"La carpeta no existe:\n{out_dir}")
            return

        self.cancelled = False
        self.progress = None
        self.active = core.ActiveProcesses()
        self._clear_parts()
        self._set_busy(True)
        self.global_bar.configure(mode="indeterminate")
        self.global_bar.start(15)
        self.stats_var.set("Consultando el servidor…")
        self.log(f"URL: {url}")

        connections = int(self.threads_var.get())
        self.worker = threading.Thread(target=self._run_download, args=(url, out_dir, connections),
                                       daemon=True)
        self.worker.start()

    def on_cancel(self):
        self.cancelled = True
        self.start_btn.configure(state="disabled", text="Cancelando…", bg=DISABLED)
        self.active.kill_all()

    def on_close(self):
        if self.worker is not None:
            if not messagebox.askyesno("idm-clon", "Hay una descarga en curso. ¿Cancelarla y salir?\n"
                                       "Los fragmentos se conservan para reanudar más tarde."):
                return
            self.active.kill_all()
        self.root.destroy()

    def _run_download(self, url, out_dir, connections):
        """Hilo de trabajo: no toca widgets, solo encola eventos."""
        def log(message, error=False):
            self.events.put(("log", message, error))

        try:
            result = core.download(url, output_dir=out_dir, connections=connections,
                                   progress=lambda total, done: GuiProgress(self, total, done),
                                   active=self.active, log=log)
            self.events.put(("done", result))
        except Exception as exc:  # cualquier fallo se muestra en la GUI
            self.events.put(("failed", exc))

    # ---------- sondeo y dibujo (hilo de la GUI) ----------

    def poll(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.worker is not None and self.progress is not None:
            self._draw_progress()
        self.root.after(POLL_MS, self.poll)

    def _handle(self, event):
        kind = event[0]
        if kind == "log":
            self.log(event[1], "error" if event[2] else None)
            if event[1].startswith("Uniendo"):
                self.stats_var.set("Uniendo fragmentos…")
        elif kind == "done":
            output, size, elapsed = event[1]
            if self.progress is not None:
                self._draw_progress()
            self._finish()
            self.global_bar["value"] = 1000
            speed = core.format_bytes(size / elapsed if elapsed > 0 else 0)
            self.stats_var.set(f"Completado: {core.format_bytes(size)} en {elapsed:.1f} s ({speed}/s)")
            self.log(f"OK {output} ({size} bytes)", "ok")
        elif kind == "failed":
            exc = event[1]
            self._finish()
            if self.cancelled:
                self.stats_var.set("Cancelado. Pulsa Iniciar Descarga para reanudar.")
                self.log("Descarga cancelada por el usuario.", "error")
            else:
                self.stats_var.set("Error en la descarga.")
                self.log(f"Error: {exc}", "error")
                messagebox.showerror("idm-clon", f"La descarga falló:\n{exc}")

    def _draw_progress(self):
        p = self.progress
        done = p.snapshot()
        if not self.part_bars:
            self._build_parts(len(done))
            self.global_bar.stop()
            self.global_bar.configure(mode="determinate")

        total_done = min(sum(done), p.total)
        self.global_bar["value"] = 1000 * total_done / p.total
        elapsed = time.monotonic() - p.started
        speed = (total_done - p.resumed) / elapsed if elapsed > 0 else 0
        eta = f"{(p.total - total_done) / speed:.0f} s" if speed > 0 else "--"
        self.stats_var.set(f"{total_done / p.total:6.1%}   {core.format_bytes(total_done)} / "
                           f"{core.format_bytes(p.total)}   {core.format_bytes(speed)}/s   ETA {eta}")

        for (bar, label, size), nbytes in zip(self.part_bars, done):
            fraction = min(nbytes / size, 1.0) if size else 1.0
            bar["value"] = 1000 * fraction
            label["text"] = f"{fraction:4.0%}"

    def _build_parts(self, count):
        self.parts_placeholder.grid_remove()
        # Tamaño de cada fragmento: se reparte igual que core.split_ranges.
        base, extra = divmod(self.progress.total, count)
        sizes = [base + (1 if i < extra else 0) for i in range(count)]
        columns = 1 if count <= 4 else 2
        rows = -(-count // columns)
        for i, size in enumerate(sizes):
            col, row = divmod(i, rows)
            c = col * 3
            ttk.Label(self.parts_frame, text=f"#{i + 1:02d}", style="Part.TLabel").grid(
                row=row, column=c, sticky="w", padx=(0 if col == 0 else 16, 6), pady=1)
            bar = ttk.Progressbar(self.parts_frame, maximum=1000)
            bar.grid(row=row, column=c + 1, sticky="ew", pady=1)
            label = ttk.Label(self.parts_frame, text="  0%", style="Part.TLabel", width=5, anchor="e")
            label.grid(row=row, column=c + 2, sticky="e", padx=(4, 0))
            self.part_bars.append((bar, label, size))

    def _clear_parts(self):
        for child in self.parts_frame.winfo_children():
            if child is not self.parts_placeholder:
                child.destroy()
        self.part_bars = []
        self.parts_placeholder.grid()
        self.global_bar["value"] = 0

    def _finish(self):
        self.global_bar.stop()
        self.global_bar.configure(mode="determinate")
        self.worker = None
        self._set_busy(False)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.url_entry, self.paste_btn, self.dir_entry, self.browse_btn):
            widget.configure(state=state)
        self.threads_combo.configure(state="disabled" if busy else "readonly")
        if busy:
            self.start_btn.configure(state="normal", text="Cancelar", bg=DANGER, activebackground=DANGER_HOVER)
        else:
            self.start_btn.configure(state="normal", text="Iniciar Descarga", bg=ACCENT,
                                     activebackground=ACCENT_HOVER)

    def log(self, message, tag=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def default_download_dir():
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def main():
    if sys.platform == "win32":
        try:  # texto nítido en pantallas con escalado
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
