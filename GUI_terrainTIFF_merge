#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Terrain TIFF Raster Merger - GUI Version
Fenster-Anwendung zum Zusammenführen von georeferenzierten TIFF-Rastern
"""

import sys
import os
import json
import queue
import threading
import multiprocessing
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# Abhängigkeiten prüfen
try:
    import rasterio
    from rasterio.merge import merge
    from rasterio.enums import Resampling

    DEPS_OK = True
    DEPS_ERROR = ""
except ImportError as e:
    DEPS_OK = False
    DEPS_ERROR = str(e)

# ============================================================================
# RESSOURCEN-PFAD (Wichtig für das Icon in der EXE)
# ============================================================================

def resource_path(relative_path):
    """ Holt den absoluten Pfad zur Ressource, passend für Dev und PyInstaller """
    try:
        # PyInstaller erstellt einen temporären Ordner _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ============================================================================
# ANWENDUNGSPFAD
# ============================================================================

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(APP_DIR, "Raster_Merge_Einstellungen.json")


# ============================================================================
# VERARBEITUNGSFUNKTIONEN
# ============================================================================

def find_tiff_files(folder, extensions):
    """Sucht rekursiv nach TIFF-Dateien im angegebenen Ordner"""
    tiff_files = []
    folder_path = Path(folder)

    print(f"Suche nach TIFF-Dateien in: {folder}")

    for ext in extensions:
        files = list(folder_path.rglob(ext))
        tiff_files.extend([str(f) for f in files])

    print(f"✓ {len(tiff_files)} TIFF-Dateien gefunden")
    return sorted(tiff_files)


def validate_tiff(filepath):
    """Validiert eine TIFF-Datei"""
    try:
        with rasterio.open(filepath) as src:
            info = {
                'crs': src.crs,
                'width': src.width,
                'height': src.height,
                'bands': src.count,
                'dtype': src.dtypes[0],
                'bounds': src.bounds
            }
            return (filepath, True, info)
    except Exception as e:
        return (filepath, False, str(e))


def validate_files_parallel(filepaths, num_threads):
    """Validiert alle TIFF-Dateien parallel"""
    print(f"\nValidiere {len(filepaths)} Dateien mit {num_threads} Threads...")

    valid_files = []
    reference_crs = None

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(validate_tiff, fp): fp for fp in filepaths}

        for i, future in enumerate(as_completed(futures), 1):
            filepath, is_valid, info = future.result()

            if not is_valid:
                print(f"⚠ Überspringe {os.path.basename(filepath)}: {info}")
                continue

            if reference_crs is None:
                reference_crs = info['crs']
            elif info['crs'] != reference_crs:
                print(f"⚠ CRS-Konflikt in {os.path.basename(filepath)}")
                print(f"  Erwartet: {reference_crs}, Gefunden: {info['crs']}")
                continue

            valid_files.append(filepath)

            if i % 10 == 0 or i == len(filepaths):
                print(f"  Fortschritt: {i}/{len(filepaths)}")

    print(f"✓ {len(valid_files)} valide Dateien")
    return valid_files


def merge_rasters(input_files, output_file, compression, resampling):
    """Führt die Raster zusammen zu einem großen TIFF"""
    print(f"\nStarte Merge von {len(input_files)} Rastern...")

    src_files_to_mosaic = []

    try:
        for fp in input_files:
            src = rasterio.open(fp)
            src_files_to_mosaic.append(src)

        print("✓ Alle Dateien geöffnet")
        print("Führe Merge durch (das kann einige Zeit dauern)...")

        mosaic, out_trans = merge(
            src_files_to_mosaic,
            resampling=resampling,
            nodata=None
        )

        print("✓ Merge abgeschlossen")

        out_meta = src_files_to_mosaic[0].meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "compress": compression
        })

        print(f"Schreibe Output nach: {output_file}")
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        with rasterio.open(output_file, "w", **out_meta) as dest:
            dest.write(mosaic)

        print("✓ Output-Datei erfolgreich erstellt!")

        with rasterio.open(output_file) as result:
            print("\n" + "=" * 60)
            print("OUTPUT-STATISTIKEN")
            print("=" * 60)
            print(f"Größe: {result.width} x {result.height} Pixel")
            print(f"Bänder: {result.count}")
            print(f"CRS: {result.crs}")
            print(f"Bounds: {result.bounds}")
            print(f"Dateigröße: {os.path.getsize(output_file) / (1024 * 1024):.2f} MB")
            print("=" * 60)

    finally:
        for src in src_files_to_mosaic:
            src.close()


def run_processing(config, stop_event=None):
    """Hauptverarbeitungsfunktion"""
    t_start = datetime.now()
    print("=" * 80)
    print("  TERRAIN TIFF RASTER MERGER")
    print("=" * 80)
    print(f"Start: {t_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input-Ordner: {config['input_folder']}")
    print(f"Output-Datei: {config['output_file']}")
    print(f"Kompression: {config['compression']}")
    print(f"Resampling: {config['resampling']}")
    print(f"CPU-Threads: {config['num_threads']}")
    print()

    # Dateien suchen
    tiff_files = find_tiff_files(config['input_folder'], ["*.tif", "*.tiff"])

    if not tiff_files:
        print("✗ Keine TIFF-Dateien gefunden!")
        return

    if stop_event and stop_event.is_set():
        print("\n⚠ Verarbeitung abgebrochen.")
        return

    # Dateien validieren
    valid_files = validate_files_parallel(tiff_files, config['num_threads'])

    if not valid_files:
        print("✗ Keine validen TIFF-Dateien zum Mergen!")
        return

    if stop_event and stop_event.is_set():
        print("\n⚠ Verarbeitung abgebrochen.")
        return

    # Merge durchführen
    merge_rasters(
        valid_files,
        config['output_file'],
        config['compression'],
        config['resampling']
    )

    t_end = datetime.now()
    print("\n" + "=" * 80)
    print("  VERARBEITUNG ABGESCHLOSSEN")
    print("=" * 80)
    print(f"Ende: {t_end.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Gesamtdauer: {str(t_end - t_start).split('.')[0]}")


# ============================================================================
# GUI - HILFSKLASSE
# ============================================================================

class TextRedirector:
    """Leitet stdout in eine tkinter Text-Widget um (thread-sicher via Queue)"""

    def __init__(self, text_widget):
        self.text_widget = text_widget
        self.queue = queue.Queue()
        self.active = True

    def write(self, s):
        if self.active:
            self.queue.put(s)

    def flush(self):
        pass

    def start_polling(self):
        self._poll()

    def _poll(self):
        if not self.active:
            return
        try:
            while True:
                text = self.queue.get_nowait()
                self.text_widget.configure(state='normal')
                self.text_widget.insert(tk.END, text)
                self.text_widget.see(tk.END)
                self.text_widget.configure(state='disabled')
        except queue.Empty:
            pass
        self.text_widget.after(80, self._poll)

    def stop(self):
        self.active = False
        try:
            while True:
                text = self.queue.get_nowait()
                self.text_widget.configure(state='normal')
                self.text_widget.insert(tk.END, text)
                self.text_widget.see(tk.END)
                self.text_widget.configure(state='disabled')
        except queue.Empty:
            pass


# ============================================================================
# HAUPTANWENDUNG
# ============================================================================

class TerrainMergerApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Terrain TIFF Raster Merger")

        # Programm-Icon (FIXED: Nutzt nun resource_path)
        _icon_path = resource_path("Programm_ICON_Merge.ico")
        try:
            self.root.iconbitmap(_icon_path)
        except Exception:
            pass  # Fallback falls Icon fehlt

        self.root.geometry("900x800")
        self.root.minsize(750, 750)

        self.stop_event = threading.Event()
        self.proc_thread = None
        self.redirector = None

        self._build_menu()
        self._build_statusbar()  # Zuerst Statusleiste bauen
        self._build_main_ui()  # Dann Hauptinhalt

        self._load_settings_auto()

    # ------------------------------------------------------------------
    # MENÜ (unverändert)
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Einstellungen speichern …",
                           command=self._save_settings_dialog,
                           accelerator="Ctrl+S")
        m_file.add_command(label="Einstellungen laden …",
                           command=self._load_settings_dialog)
        m_file.add_separator()
        m_file.add_command(label="Beenden", command=self._on_close)
        menubar.add_cascade(label="Datei", menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Anleitung", command=self._show_anleitung)
        m_help.add_command(label="Info", command=self._show_info)
        m_help.add_separator()
        m_help.add_command(label="Kontakt", command=self._show_kontakt)
        m_help.add_command(label="Über", command=self._show_about)
        menubar.add_cascade(label="Hilfe", menu=m_help)

        self.root.config(menu=menubar)
        self.root.bind('<Control-s>', lambda e: self._save_settings_dialog())

    # ------------------------------------------------------------------
    # HAUPTOBERFLÄCHE
    # ------------------------------------------------------------------

    def _build_main_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Variablen
        self.v_input = tk.StringVar()
        self.v_output = tk.StringVar()
        self.v_compression = tk.StringVar(value="LZW")
        self.v_resampling = tk.StringVar(value="nearest")
        self.v_threads = tk.StringVar(value="Auto")

        # --- Input-Ordner ---
        f_input = ttk.LabelFrame(main_frame, text="Input-Ordner", padding=12)
        f_input.pack(fill=tk.X, pady=(0, 8))
        f_input.columnconfigure(1, weight=1)

        ttk.Label(f_input, text="TIFF-Ordner *:").grid(
            row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_input, textvariable=self.v_input).grid(
            row=0, column=1, sticky=tk.EW, padx=6, pady=5)
        ttk.Button(f_input, text="📁", width=3,
                   command=lambda: self.v_input.set(
                       filedialog.askdirectory(title="Input-Ordner wählen") or self.v_input.get()
                   )).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))

        ttk.Label(f_input,
                  text="Alle TIFF-Dateien in diesem Ordner (inkl. Unterordner) werden zusammengeführt",
                  foreground="gray").grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=6)

        # --- Output-Datei ---
        f_output = ttk.LabelFrame(main_frame, text="Output-Datei", padding=12)
        f_output.pack(fill=tk.X, pady=8)
        f_output.columnconfigure(1, weight=1)

        ttk.Label(f_output, text="Ziel-Datei *:").grid(
            row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_output, textvariable=self.v_output).grid(
            row=0, column=1, sticky=tk.EW, padx=6, pady=5)
        ttk.Button(f_output, text="📁", width=3,
                   command=lambda: self.v_output.set(
                       filedialog.asksaveasfilename(
                           title="Output-Datei speichern",
                           defaultextension=".tif",
                           filetypes=[("TIFF Dateien", "*.tif *.tiff"), ("Alle Dateien", "*.*")]
                       ) or self.v_output.get()
                   )).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))

        ttk.Label(f_output,
                  text="Zusammengeführte TIFF-Datei",
                  foreground="gray").grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=6)

        # --- Optionen ---
        f_options = ttk.LabelFrame(main_frame, text="Optionen", padding=12)
        f_options.pack(fill=tk.X, pady=8)
        f_options.columnconfigure(1, weight=0)

        ttk.Label(f_options, text="Kompression:").grid(
            row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Combobox(f_options, textvariable=self.v_compression,
                     values=["None", "LZW", "DEFLATE", "JPEG"],
                     state="readonly", width=12).grid(
            row=0, column=1, sticky=tk.W, padx=6)

        ttk.Label(f_options, text="Resampling:").grid(
            row=1, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Combobox(f_options, textvariable=self.v_resampling,
                     values=["nearest", "bilinear", "cubic", "cubic_spline", "lanczos", "average"],
                     state="readonly", width=12).grid(
            row=1, column=1, sticky=tk.W, padx=6)

        ttk.Label(f_options, text="CPU-Threads:").grid(
            row=2, column=0, sticky=tk.W, padx=6, pady=5)
        thread_frame = ttk.Frame(f_options)
        thread_frame.grid(row=2, column=1, sticky=tk.W, padx=6)
        ttk.Combobox(thread_frame, textvariable=self.v_threads,
                     values=["Auto"] + list(range(1, multiprocessing.cpu_count() + 1)),
                     state="readonly", width=10).pack(side=tk.LEFT)
        ttk.Label(thread_frame,
                  text=f"  (Max: {multiprocessing.cpu_count()} Kerne)",
                  foreground="gray").pack(side=tk.LEFT, padx=6)

        # --- Ausgabe ---
        f_output_log = ttk.LabelFrame(main_frame, text="Status & Log", padding=10)
        f_output_log.pack(fill=tk.BOTH, expand=True, pady=8)

        self.output_text = scrolledtext.ScrolledText(
            f_output_log, state='disabled', font=('Consolas', 9),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='white',
            wrap=tk.WORD, height=15
        )
        self.output_text.pack(fill=tk.BOTH, expand=True)

        btn_clear = ttk.Button(f_output_log, text="Ausgabe leeren",
                               command=self._clear_output)
        btn_clear.pack(side=tk.RIGHT, pady=(5, 0))

    def _clear_output(self):
        """Leert das Ausgabe-Log"""
        self.output_text.configure(state='normal')
        self.output_text.delete('1.0', tk.END)
        self.output_text.configure(state='disabled')

    # ------------------------------------------------------------------
    # STATUSLEISTE
    # ------------------------------------------------------------------

    def _build_statusbar(self):
        outer = ttk.Frame(self.root, relief=tk.SUNKEN)
        outer.pack(fill=tk.X, side=tk.BOTTOM, padx=0, pady=0, anchor=tk.S)

        # Buttons
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill=tk.X, padx=8, pady=6, side=tk.BOTTOM)

        self.btn_stop = ttk.Button(btn_frame, text="■  Stopp",
                                   command=self._stop,
                                   state=tk.DISABLED, width=12)
        self.btn_stop.pack(side=tk.RIGHT, padx=(4, 0))

        self.btn_start = ttk.Button(btn_frame, text="▶  Verarbeitung starten",
                                    command=self._start)
        self.btn_start.pack(side=tk.RIGHT, padx=4)

        self.lbl_status = ttk.Label(btn_frame, text="Bereit")
        self.lbl_status.pack(side=tk.LEFT, padx=(2, 0))

    # ------------------------------------------------------------------
    # EINSTELLUNGEN
    # ------------------------------------------------------------------

    def _collect_config(self):
        """Sammelt alle Einstellungen"""
        n = self.v_threads.get().strip()
        if n.lower() == 'auto':
            nproc = multiprocessing.cpu_count()
        else:
            try:
                nproc = int(n)
            except ValueError:
                nproc = multiprocessing.cpu_count()

        compression = None if self.v_compression.get() == "None" else self.v_compression.get()
        resampling = getattr(Resampling, self.v_resampling.get())

        return {
            'input_folder': self.v_input.get().strip(),
            'output_file': self.v_output.get().strip(),
            'compression': compression,
            'resampling': resampling,
            'num_threads': nproc,
            # Für JSON-Speicherung (String-Versionen)
            'compression_str': self.v_compression.get(),
            'resampling_str': self.v_resampling.get(),
            'threads_str': self.v_threads.get()
        }

    def _apply_config(self, cfg):
        """Lädt Einstellungen in die GUI"""
        self.v_input.set(cfg.get('input_folder', ''))
        self.v_output.set(cfg.get('output_file', ''))
        self.v_compression.set(cfg.get('compression_str', 'LZW'))
        self.v_resampling.set(cfg.get('resampling_str', 'nearest'))
        self.v_threads.set(cfg.get('threads_str', 'Auto'))

    def _save_settings_auto(self):
        """Speichert Einstellungen automatisch"""
        cfg = self._collect_config()
        save_cfg = {
            'input_folder': cfg['input_folder'],
            'output_file': cfg['output_file'],
            'compression_str': cfg['compression_str'],
            'resampling_str': cfg['resampling_str'],
            'threads_str': cfg['threads_str']
        }
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(save_cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_settings_auto(self):
        """Lädt Einstellungen automatisch"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    self._apply_config(json.load(f))
            except Exception:
                pass

    def _save_settings_dialog(self):
        """Speichert Einstellungen mit Dialog"""
        cfg = self._collect_config()
        save_cfg = {
            'input_folder': cfg['input_folder'],
            'output_file': cfg['output_file'],
            'compression_str': cfg['compression_str'],
            'resampling_str': cfg['resampling_str'],
            'threads_str': cfg['threads_str']
        }
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON-Dateien", "*.json"), ("Alle Dateien", "*.*")],
            initialfile="TerrainTIff_Merger_Einstellungen.json",
            title="Einstellungen speichern"
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(save_cfg, f, indent=2, ensure_ascii=False)
            messagebox.showinfo("Gespeichert", f"Einstellungen gespeichert:\n{path}")
        except Exception as e:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen:\n{e}")

    def _load_settings_dialog(self):
        """Lädt Einstellungen mit Dialog"""
        path = filedialog.askopenfilename(
            filetypes=[("JSON-Dateien", "*.json"), ("Alle Dateien", "*.*")],
            title="Einstellungen laden"
        )
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self._apply_config(json.load(f))
        except Exception as e:
            messagebox.showerror("Fehler", f"Laden fehlgeschlagen:\n{e}")

    # ------------------------------------------------------------------
    # VERARBEITUNG
    # ------------------------------------------------------------------

    def _validate(self, cfg):
        """Validiert die Eingaben"""
        if not cfg['input_folder']:
            messagebox.showerror("Fehler", "Bitte Input-Ordner angeben!")
            return False
        if not os.path.exists(cfg['input_folder']):
            messagebox.showerror("Fehler", "Input-Ordner existiert nicht!")
            return False
        if not cfg['output_file']:
            messagebox.showerror("Fehler", "Bitte Output-Datei angeben!")
            return False
        return True

    def _start(self):
        """Startet die Verarbeitung"""
        if self.proc_thread and self.proc_thread.is_alive():
            messagebox.showwarning("Hinweis", "Verarbeitung läuft bereits!")
            return

        if not DEPS_OK:
            messagebox.showerror("Fehlende Bibliotheken",
                                 f"Folgende Pakete konnten nicht geladen werden:\n\n{DEPS_ERROR}\n\n"
                                 "Bitte per 'pip install rasterio' nachinstallieren.")
            return

        cfg = self._collect_config()
        if not self._validate(cfg):
            return

        self.stop_event.clear()

        # Stdout umleiten
        self.redirector = TextRedirector(self.output_text)
        sys.stdout = self.redirector
        self.redirector.start_polling()

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.lbl_status.configure(text="⏳  Verarbeitung läuft …")

        self.proc_thread = threading.Thread(
            target=self._thread_worker, args=(cfg,), daemon=True)
        self.proc_thread.start()

    def _thread_worker(self, cfg):
        """Worker-Thread für die Verarbeitung"""
        try:
            run_processing(cfg, self.stop_event)
        except Exception as e:
            print(f"\n✗ Unerwarteter Fehler: {e}")
            import traceback
            traceback.print_exc()
        finally:
            sys.stdout = sys.__stdout__
            self.root.after(0, self._on_done)

    def _on_done(self):
        """Wird nach Abschluss der Verarbeitung aufgerufen"""
        if self.redirector:
            self.redirector.stop()
            self.redirector = None

        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.lbl_status.configure(text="Bereit")

    def _stop(self):
        """Stoppt die Verarbeitung"""
        if self.proc_thread and self.proc_thread.is_alive():
            self.stop_event.set()
            self.lbl_status.configure(text="⚠  Abbruch angefordert …")

    # ------------------------------------------------------------------
    # HILFE-MENÜ
    # ------------------------------------------------------------------

    def _show_anleitung(self):
        messagebox.showinfo(
            "Anleitung",
            "Terrain TIFF Raster Merger – Kurzanleitung:\n"
            "\n"
            "1. INPUT-ORDNER\n"
            "   • Ordner mit TIFF-Rasterbildern wählen\n"
            "   • Programm durchsucht automatisch alle Unterordner\n"
            "   • Alle TIFFs werden zusammengeführt\n"
            "\n"
            "2. OUTPUT-DATEI\n"
            "   • Ziel-Datei für das zusammengeführte Raster angeben\n"
            "   • Format: .tif oder .tiff\n"
            "\n"
            "3. OPTIONEN\n"
            "   • Kompression: LZW empfohlen für verlustfreie Kompression\n"
            "   • Resampling: nearest für exakte Werte, bilinear für geglättete Übergänge\n"
            "   • CPU-Threads: 'Auto' nutzt alle verfügbaren Prozessorkerne\n"
            "\n"
            "4. VERARBEITUNG\n"
            "   • 'Verarbeitung starten' – Fortschritt wird im Log angezeigt\n"
            "   • 'Stopp' bricht die Verarbeitung ab\n"
            "\n"
            "5. EINSTELLUNGEN\n"
            "   • Werden automatisch beim Schließen gespeichert\n"
            "   • Manuelles Speichern/Laden über Datei-Menü möglich\n"
            "\n"
        )

    def _show_info(self):
        messagebox.showinfo(
            "Info",
            "Terrain TIFF Raster Merger\n"
            "\n"
            "Dieses Tool führt georeferenzierte TIFF-Raster zu einem\n"
            "großen zusammenhängenden Raster zusammen.\n"
            "\n"
            "\n"
            "Entwickelt für:\n"
            "• DGM-Kacheln (Digitale Geländemodelle)\n"
            "• DOM-Kacheln (Digitale Oberflächenmodelle)\n"
            "• Beliebige georeferenzierte TIFF-Raster\n"
            "\n"
            "Ausgabe:\n"
            "• Einzelnes georeferenziertes GeoTIFF\n"
            "• Wahlweise komprimiert (LZW, DEFLATE, JPEG)\n"
            "• Mit Statistiken (Größe, CRS, Bounds)\n"
            "\n"
            "Funktionen:\n"
            "• Rekursive Suche nach TIFF-Dateien\n"
            "• Automatische CRS-Validierung\n"
            "• Parallele Verarbeitung mit Multithreading\n"
            "• Verschiedene Kompressionsverfahren\n"
            "• Flexible Resampling-Methoden\n"
            "\n"
            "Anforderungen:\n"
            "• Alle Raster müssen das gleiche CRS haben\n"
            "• Georeferenzierung muss vorhanden sein\n"
            "• Ausreichend Arbeitsspeicher für große Raster\n"
            "\n"
            "Technische Details:\n"
            "• Verwendet rasterio für Raster-Operationen\n"
            "• Multithreading für Dateivalidierung\n"
            f"• Einstellungen werden automatisch gespeichert:\n{SETTINGS_FILE}\n"
        )

    def _on_close(self):
        """Wird beim Schließen des Fensters aufgerufen"""
        if self.proc_thread and self.proc_thread.is_alive():
            if not messagebox.askyesno("Beenden",
                                       "Eine Verarbeitung läuft noch.\n"
                                       "Wirklich beenden?"):
                return
            self.stop_event.set()
        self._save_settings_auto()
        self.root.destroy()

    def _show_kontakt(self):
        messagebox.showinfo(
            "Kontakt",
            "Kontakt, Support & Weiterentwicklung\n\n"
            "Ansprechpartner:\n"
            "  Name:    Fabian Britze\n"
            "  E-Mail:   fabian.britze@gmail.com\n\n"
            "Bei Fragen zur Bedienung oder Fehlermeldungen "
            "bitte vollständiges Protokoll mitsenden."
        )

    def _show_about(self):
        messagebox.showinfo(
            "Über",
            "Terrain TIFF Raster Merger\n"
            "Version 1.0\n"
            "\n"
            "Werkzeug zum Zusammenführen von georeferenzierten\n"
            "TIFF-Rastern zu einem großen Gesamt-Raster.\n\n"
            "Die Entwicklung erfolgte im Rahmen einer Bachelorarbeit.\n\n"
            "Icon: Flaticon.com"
            "\n"
        )


# ============================================================================
# EINSTIEGSPUNKT
# ============================================================================

def main():
    """Hauptfunktion"""
    multiprocessing.freeze_support()

    root = tk.Tk()

    # Erscheinungsbild
    style = ttk.Style()
    for theme in ('vista', 'winnative', 'aqua', 'clam', 'alt', 'default'):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue

    app = TerrainMergerApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
