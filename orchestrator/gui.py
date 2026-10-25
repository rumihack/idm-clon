"""Interfaz gráfica (tkinter) de idm-clon.

Usa el mismo motor que la línea de comandos (orchestrator.download): la
descarga corre en un hilo aparte y lanza un core_downloader por fragmento.
Los hilos de descarga nunca tocan los widgets; solo actualizan contadores
(GuiProgress) y dejan mensajes en una cola, que la GUI lee cada 100 ms.

Uso:
  python orchestrator/gui.py
"""

import ctypes
import argparse
import queue
import shutil
import subprocess
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
DANGER = "#C42B1C"
SURFACE = "#FFFFFF"
CANVAS = "#F3F5F7"
INK = "#202124"
MUTED = "#667085"


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
    def __init__(self, root, audio_dir=None, audio_status=None):
        self.root = root
        self.audio_dir = Path(audio_dir) if audio_dir else None
        self.audio_status = Path(audio_status) if audio_status else None
        self.events = queue.Queue()
        self.worker = None
        self.active = None
        self.progress = None
        self.thread_rows = []
        self.cancelled = False
        self.paused = False

        root.title("idm-clon — Download status")
        root.geometry("760x610")
        root.minsize(650, 520)
        root.configure(bg=CANVAS)
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
        style.configure(".", font=base, background=CANVAS, foreground=INK)
        style.configure("TFrame", background=CANVAS)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("TNotebook", background=CANVAS, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8), background="#E8EBEF")
        style.map("TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", ACCENT)])
        style.configure("Surface.TLabel", background=SURFACE, foreground=INK)
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Treeview", rowheight=28, background=SURFACE, fieldbackground=SURFACE,
                         foreground=INK, borderwidth=1, relief="solid")
        style.configure("Treeview.Heading", padding=(8, 7), background="#EEF1F4",
                         foreground=INK, relief="solid")
        style.map("Treeview", background=[("selected", "#DCEBFA")], foreground=[("selected", INK)])
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Sub.TLabel", foreground="#5F5F5F")
        style.configure("Global.Horizontal.TProgressbar", thickness=20, troughcolor="#E5E9EE",
                         background=ACCENT, bordercolor="#D2D8DF", lightcolor=ACCENT,
                         darkcolor=ACCENT)

    def _build(self):
        main = ttk.Frame(self.root, padding=(18, 14))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        ttk.Label(main, text="idm-clon", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        notebook = ttk.Notebook(main)
        notebook.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        status_tab = ttk.Frame(notebook, padding=14)
        notebook.add(status_tab, text="Download status")
        status_tab.columnconfigure(0, weight=1)

        ttk.Label(status_tab, text="URL del archivo").grid(row=0, column=0, sticky="w")
        url_row = ttk.Frame(status_tab, style="Surface.TFrame", padding=10, relief="solid", borderwidth=1)
        url_row.grid(row=1, column=0, sticky="ew", pady=(5, 10))
        url_row.columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew", ipady=4)
        self.url_entry.bind("<Return>", lambda e: self.on_start())
        self.paste_btn = ttk.Button(url_row, text="Pegar", command=self.paste_url)
        self.paste_btn.grid(row=0, column=1, padx=(8, 0))

        # Hilos y carpeta de destino
        opts = ttk.Frame(status_tab, style="Surface.TFrame", padding=10, relief="solid", borderwidth=1)
        opts.grid(row=2, column=0, sticky="ew", pady=(0, 10))
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

        panel = ttk.Frame(status_tab, style="Surface.TFrame", padding=12, relief="solid", borderwidth=1)
        panel.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        panel.columnconfigure(0, weight=1)
        ttk.Label(panel, text="Download status", style="Surface.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 7))
        self.global_bar = ttk.Progressbar(panel, style="Global.Horizontal.TProgressbar", maximum=1000)
        self.global_bar.grid(row=1, column=0, sticky="ew")
        self.stats_var = tk.StringVar(value="Listo.")
        ttk.Label(panel, textvariable=self.stats_var, style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(7, 0))
        controls = ttk.Frame(panel, style="Surface.TFrame")
        controls.grid(row=3, column=0, sticky="e", pady=(10, 0))
        self.pause_btn = ttk.Button(controls, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=(0, 8))
        self.cancel_btn = ttk.Button(controls, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.start_btn = ttk.Button(controls, text="Start Download", command=self.on_start)
        self.start_btn.pack(side="left")

        ttk.Label(status_tab, text="Download threads", style="Sub.TLabel").grid(
            row=4, column=0, sticky="w", pady=(0, 5))
        table_frame = ttk.Frame(status_tab, style="Surface.TFrame", relief="solid", borderwidth=1)
        table_frame.grid(row=5, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        status_tab.rowconfigure(5, weight=1)
        self.thread_table = ttk.Treeview(
            table_frame, columns=("number", "downloaded", "info"), show="headings",
            selectmode="browse", height=6,
        )
        self.thread_table.heading("number", text="N.", anchor="center")
        self.thread_table.heading("downloaded", text="Downloaded", anchor="center")
        self.thread_table.heading("info", text="Info", anchor="w")
        self.thread_table.column("number", width=70, minwidth=60, stretch=False, anchor="center")
        self.thread_table.column("downloaded", width=120, minwidth=100, stretch=False, anchor="center")
        self.thread_table.column("info", width=440, minwidth=220, stretch=True, anchor="w")
        self.thread_table.grid(row=0, column=0, sticky="nsew")
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.thread_table.yview)
        table_scroll.grid(row=0, column=1, sticky="ns")
        self.thread_table.configure(yscrollcommand=table_scroll.set)

        log_frame = ttk.Frame(status_tab)
        log_frame.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=3, font=("Consolas", 9), relief="solid", bd=1,
                                bg=SURFACE, fg=INK, wrap="word", state="disabled",
                                padx=8, pady=6)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("error", foreground=DANGER)
        self.log_text.tag_configure("ok", foreground="#107C10")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text["yscrollcommand"] = scroll.set

        self.url_entry.focus_set()

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
        self.paused = False
        self.progress = None
        self.active = core.ActiveProcesses()
        self._clear_thread_table()
        self.thread_rows = [self.thread_table.insert("", "end", values=(1, "0 B", "Connecting..."))]
        self._thread_sizes = [None]
        self._set_busy(True)
        self.global_bar.configure(mode="indeterminate")
        self.global_bar.start(15)
        self.stats_var.set("Consultando el servidor…")
        self.log(f"URL: {url}")

        connections = int(self.threads_var.get())
        self.worker = threading.Thread(target=self._run_download, args=(url, out_dir, connections),
                                       daemon=True)
        self.worker.start()

    def toggle_pause(self):
        if self.worker is None or self.active is None:
            return
        try:
            if self.paused:
                self.active.resume_all()
                self.paused = False
                self.pause_btn.configure(text="Pause")
                self.stats_var.set("Descarga reanudada.")
            else:
                self.active.pause_all()
                self.paused = True
                self.pause_btn.configure(text="Resume")
                self.stats_var.set("Descarga pausada.")
        except (OSError, AttributeError) as exc:
            messagebox.showerror("idm-clon", f"No se pudo cambiar el estado de la descarga:\n{exc}")

    def on_cancel(self):
        if self.worker is None:
            return
        self.cancelled = True
        self.cancel_btn.configure(state="disabled", text="Canceling…")
        self.pause_btn.configure(state="disabled")
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
            if self.audio_dir and self.audio_status:
                output, _, elapsed = result
                while not self.audio_status.exists():
                    time.sleep(0.2)
                if self.audio_status.read_text(encoding="ascii").strip() != "0":
                    raise RuntimeError("No se pudo descargar el flujo de audio de YouTube")
                audio_files = [
                    path for path in self.audio_dir.glob("audio.*")
                    if path.is_file() and path.name != "audio.status"
                ]
                if not audio_files:
                    raise RuntimeError("yt-dlp no generó un archivo de audio")
                ffmpeg = shutil.which("ffmpeg")
                if not ffmpeg:
                    raise RuntimeError("Se necesita ffmpeg para integrar audio y video")

                final_output = output if output.suffix.lower() == ".mp4" else output.with_suffix(".mp4")
                merged_output = final_output.with_name(final_output.stem + ".with-audio.tmp.mp4")
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-i",
                        str(output),
                        "-i",
                        str(audio_files[0]),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-c",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-movflags",
                        "+faststart",
                        str(merged_output),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                merged_output.replace(final_output)
                if output != final_output:
                    output.unlink(missing_ok=True)
                shutil.rmtree(self.audio_dir, ignore_errors=True)
                result = (final_output, final_output.stat().st_size, elapsed)
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
            for index, item in enumerate(self.thread_table.get_children(), start=1):
                downloaded = self.progress.snapshot()[index - 1] if self.progress else 0
                self.thread_table.item(item, values=(index, core.format_bytes(downloaded), "Complete"))
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
            for index, item in enumerate(self.thread_table.get_children(), start=1):
                downloaded = self.progress.snapshot()[index - 1] if self.progress else 0
                status = "Canceled" if self.cancelled else "Error"
                self.thread_table.item(item, values=(index, core.format_bytes(downloaded), status))

    def _draw_progress(self):
        p = self.progress
        done = p.snapshot()
        if len(self.thread_rows) != len(done):
            self._build_thread_rows(len(done))
            self.global_bar.stop()
            self.global_bar.configure(mode="determinate")

        total_done = min(sum(done), p.total)
        self.global_bar["value"] = 1000 * total_done / p.total
        elapsed = time.monotonic() - p.started
        speed = (total_done - p.resumed) / elapsed if elapsed > 0 else 0
        eta = f"{(p.total - total_done) / speed:.0f} s" if speed > 0 else "--"
        if self.paused:
            self.stats_var.set(f"Pausada   {total_done / p.total:6.1%}   "
                               f"{core.format_bytes(total_done)} / {core.format_bytes(p.total)}")
        else:
            self.stats_var.set(f"{total_done / p.total:6.1%}   {core.format_bytes(total_done)} / "
                               f"{core.format_bytes(p.total)}   {core.format_bytes(speed)}/s   ETA {eta}")

        for index, (item, nbytes) in enumerate(zip(self.thread_rows, done), start=1):
            size = self._thread_sizes[index - 1]
            info = "Paused" if self.paused else (
                "Complete" if size is not None and nbytes >= size else "Receiving data..."
            )
            self.thread_table.item(item, values=(index, core.format_bytes(nbytes), info))

    def _build_thread_rows(self, count):
        base, extra = divmod(self.progress.total, count)
        self._thread_sizes = [base + (1 if i < extra else 0) for i in range(count)]
        for item in self.thread_table.get_children():
            self.thread_table.delete(item)
        self.thread_rows = [
            self.thread_table.insert("", "end", values=(index + 1, "0 B", "Connecting..."))
            for index in range(count)
        ]

    def _clear_thread_table(self):
        for item in self.thread_table.get_children():
            self.thread_table.delete(item)
        self.thread_rows = []
        self._thread_sizes = []
        self.global_bar["value"] = 0

    def _finish(self):
        self.global_bar.stop()
        self.global_bar.configure(mode="determinate")
        self.worker = None
        self.paused = False
        self._set_busy(False)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.url_entry, self.paste_btn, self.dir_entry, self.browse_btn):
            widget.configure(state=state)
        self.threads_combo.configure(state="disabled" if busy else "readonly")
        self.start_btn.configure(state="disabled" if busy else "normal", text="Start Download")
        self.pause_btn.configure(state="normal" if busy else "disabled", text="Pause")
        self.cancel_btn.configure(state="normal" if busy else "disabled", text="Cancel")

    def log(self, message, tag=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def default_download_dir():
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def main(initial_url=None, audio_dir=None, audio_status=None):
    if sys.platform == "win32":
        try:  # texto nítido en pantallas con escalado
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    app = App(root, audio_dir=audio_dir, audio_status=audio_status)
    if initial_url:
        app.url_var.set(initial_url)
        root.after(0, app.on_start)
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interfaz gráfica de idm-clon.")
    parser.add_argument("url", nargs="?", help="URL opcional para iniciar la descarga")
    parser.add_argument("--audio-dir", help=argparse.SUPPRESS)
    parser.add_argument("--audio-status", help=argparse.SUPPRESS)
    args = parser.parse_args()
    main(args.url, args.audio_dir, args.audio_status)
