"""Selector MP4; todos los widgets se utilizan exclusivamente desde Tkinter."""

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if __package__:
    from . import media
else:
    import media


class SelectionDialog:
    def __init__(self, root, url, folder, finished):
        self.finished = finished
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.choices = []
        self.window = tk.Toplevel(root)
        self.window.title("Seleccionar descarga MP4 — idm-clon")
        self.window.geometry("680x330")
        self.window.minsize(550, 330)
        self.window.protocol("WM_DELETE_WINDOW", self.on_cancel)
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        self.status = tk.StringVar(value="Consultando los formatos de YouTube…")
        ttk.Label(frame, textvariable=self.status, wraplength=620).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        ttk.Label(frame, text="Calidad MP4").grid(row=1, column=0, sticky="w")
        self.quality = ttk.Combobox(frame, state="disabled", width=60)
        self.quality.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(frame, text="Nombre").grid(row=2, column=0, sticky="w")
        self.name = tk.StringVar()
        ttk.Entry(frame, textvariable=self.name).grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(frame, text="Carpeta").grid(row=3, column=0, sticky="w")
        self.folder = tk.StringVar(value=str(folder))
        ttk.Entry(frame, textvariable=self.folder).grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text="Examinar…", command=self.browse).grid(row=3, column=2, padx=(8, 0))
        ttk.Label(frame, text="Solo formatos MP4 directos compatibles. Códec y FPS indicados en cada opción.", wraplength=620).grid(row=4, column=0, columnspan=3, sticky="w", pady=10)
        self.download = ttk.Button(frame, text="Descargar", state="disabled", command=self.confirm)
        self.download.grid(row=5, column=1, sticky="e", pady=8)
        ttk.Button(frame, text="Cancelar", command=self.on_cancel).grid(row=5, column=2, padx=(8, 0))
        self.worker = threading.Thread(target=self.query, args=(url,), daemon=True)
        self.worker.start()
        self.window.after(100, self.poll)

    def query(self, url):
        try:
            info = media.fetch_metadata(url, self.cancel)
            choices = media.available_choices(info, self.cancel)
            if not choices:
                raise media.MediaError("No hay calidades MP4 directas con audio compatibles para este video")
            self.events.put((info, choices, None))
        except Exception as exc:
            self.events.put((None, None, exc))

    def poll(self):
        if self.cancel.is_set():
            if not self.worker.is_alive():
                self.window.destroy()
                self.finished(None)
                return
        else:
            try:
                info, choices, error = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                if error:
                    self.status.set("No se pudo consultar el video. Puedes cancelar y volver a intentarlo.")
                    messagebox.showerror("Consulta de YouTube", str(error), parent=self.window)
                else:
                    self.choices = choices
                    self.status.set(str(info.get("title") or "Video de YouTube"))
                    self.name.set(media.safe_filename(info.get("title") or "video"))
                    self.quality.configure(state="readonly", values=[c.label for c in choices])
                    self.quality.current(0)
                    self.download.configure(state="normal")
        self.window.after(100, self.poll)

    def browse(self):
        folder = filedialog.askdirectory(parent=self.window, initialdir=self.folder.get())
        if folder:
            self.folder.set(folder)

    def confirm(self):
        if self.cancel.is_set() or not self.choices or self.quality.current() < 0:
            return
        try:
            target = media.destination(self.folder.get(), self.name.get())
        except (OSError, ValueError, media.MediaError) as exc:
            messagebox.showerror("Destino de descarga", str(exc), parent=self.window)
            return
        choice = self.choices[self.quality.current()]
        self.window.destroy()
        self.finished((choice, target))

    def on_cancel(self):
        self.cancel.set()
        self.download.configure(state="disabled")
        self.status.set("Cancelando consulta…")
