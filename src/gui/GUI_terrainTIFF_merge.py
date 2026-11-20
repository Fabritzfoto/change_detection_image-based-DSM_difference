"""
Terrain TIFF Raster Merger - GUI Version
Fenster-Anwendung zum Zusammenführen von georeferenzierten TIFF-Rastern

Version 2.0 – speicherschonende Verarbeitung großer Datensätze
---------------------------------------------------------------
* Das Mosaik wird NICHT mehr komplett im Arbeitsspeicher aufgebaut, sondern
  streifenweise (block-ausgerichtet) gelesen, zusammengesetzt und geschrieben.
  Der RAM-Bedarf ist dadurch unabhängig von der Gesamtgröße des Ergebnisses.
* Ausgabe als BigTIFF mit interner Kachelung (Tiles), Overviews (Pyramiden)
  und optional als Cloud Optimized GeoTIFF (COG) – für ArcGIS.
* Leere Bereiche (NoData) werden dank SPARSE_OK nicht auf die Platte geschrieben.
* Alternativ: Kachel-Mosaik (z.B. 5 km x 5 km Dateien + VRT) oder reines VRT.
"""

import sys
import os
import re
import json
import math
import time
import queue
import shutil
import ctypes
import logging
import threading
import traceback
import multiprocessing
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.sax.saxutils import escape as xml_escape

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# Abhängigkeiten prüfen
try:
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window
    from rasterio.vrt import WarpedVRT
    from rasterio.transform import Affine
    from rasterio.shutil import copy as rio_copy

    DEPS_OK = True
    DEPS_ERROR = ""
except ImportError as e:
    DEPS_OK = False
    DEPS_ERROR = str(e)

APP_VERSION = "2.0"

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
# KONSTANTEN / AUSWAHLLISTEN
# ============================================================================

# Schlüssel -> Anzeigetext (in der JSON werden die Schlüssel gespeichert)
FORMATS = {
    "COG":   "COG – Cloud Optimized GeoTIFF (empfohlen)",
    "GTiff": "GeoTIFF – BigTIFF, gekachelt",
    "VRT":   "VRT – virtuelles Mosaik (ohne Kopie)",
}
LAYOUTS = {
    "single": "Eine Gesamtdatei",
    "tiles":  "Kachel-Mosaik (mehrere Dateien + VRT)",
}
OVERLAP_MODES = {
    "first": "Erste gültige Kachel behalten",
    "last":  "Letzte gültige Kachel überschreibt",
    "max":   "Maximum der Kacheln",
    "min":   "Minimum der Kacheln",
}
COMPRESSIONS = ["DEFLATE", "LERC_DEFLATE", "LERC_ZSTD", "LERC", "ZSTD", "LZW", "None"]
RESAMPLINGS = ["nearest", "bilinear", "cubic", "cubic_spline", "lanczos", "average"]
OVR_RESAMPLINGS = ["average", "nearest", "bilinear", "cubic", "mode"]
BLOCKSIZES = ["256", "512", "1024"]
TILE_SIZES_KM = ["1", "2", "5", "10", "20", "50"]
TARGET_RES = ["Original", "0.5", "1.0", "2.0", "5.0", "10.0"]
RAM_CHOICES = ["Auto", "1", "2", "4", "8", "16", "32"]

GDAL_DTYPES = {
    "uint8": "Byte", "int8": "Int8", "uint16": "UInt16", "int16": "Int16",
    "uint32": "UInt32", "int32": "Int32", "uint64": "UInt64", "int64": "Int64",
    "float32": "Float32", "float64": "Float64",
}

CHUNK_COLS = 8192           # max. Breite eines Schreib-Chunks in Pixeln
READ_RETRIES = 3            # Wiederholungen bei Lesefehlern (Netzlaufwerk)

DEFAULT_SETTINGS = {
    "input_folder": "",
    "recursive": True,
    "output_file": "",
    "temp_folder": "",
    "format_str": "COG",
    "layout_str": "single",
    "tile_km_str": "5",
    "compression_str": "DEFLATE",
    "lerc_error_str": "0.01",
    "blocksize_str": "512",
    "overviews": True,
    "ovr_resampling_str": "average",
    "target_res_str": "Original",
    "resampling_str": "nearest",
    "overlap_str": "first",
    "nodata_str": "Auto",
    "threads_str": "Auto",
    "ram_str": "Auto",
}


# ============================================================================
# HILFSFUNKTIONEN
# ============================================================================

def memory_info():
    """Gibt (gesamt, verfügbar) physischen Arbeitsspeicher in Bytes zurück."""
    try:
        if sys.platform == "win32":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return int(st.ullTotalPhys), int(st.ullAvailPhys)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        avail = os.sysconf("SC_AVPHYS_PAGES")
        return pages * page_size, avail * page_size
    except Exception:
        return 8 * 1024 ** 3, 4 * 1024 ** 3


def fmt_bytes(n):
    """Formatiert Bytes menschenlesbar."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def fmt_duration(seconds):
    return str(timedelta(seconds=int(max(0, seconds))))


def is_nan(value):
    try:
        return value is not None and math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def crs_label(crs):
    """Kurzer, lesbarer CRS-Name."""
    try:
        epsg = crs.to_epsg()
    except Exception:
        epsg = None
    m = re.search(r'"([^"]+)"', crs.to_wkt())
    name = m.group(1) if m else crs.to_string()
    return f"{name} (EPSG:{epsg})" if epsg else name


def sidecar_files(path):
    """Mögliche Begleitdateien einer Rasterdatei."""
    return [path + ".ovr", path + ".aux.xml", path + ".msk"]


def remove_raster(path, strict=False):
    """Löscht eine Rasterdatei inkl. Begleitdateien.
    Bei strict=True wird ein verständlicher Fehler ausgelöst, falls die Hauptdatei gesperrt ist."""
    for p in [path] + sidecar_files(path):
        if not os.path.exists(p):
            continue
        try:
            os.remove(p)
        except PermissionError as e:
            if strict and p == path:
                raise PermissionError(
                    f"Die Zieldatei ist durch ein anderes Programm gesperrt "
                    f"(z. B. ArcGIS Pro, QGIS oder Windows-Vorschau) und kann nicht "
                    f"überschrieben werden:\n{path}\n\n"
                    f"Bitte schließen Sie die Datei im anderen Programm oder wählen Sie "
                    f"einen anderen Dateinamen."
                ) from e
        except OSError:
            if strict and p == path:
                raise


def check_target_writable(path):
    """Prüft vorab, ob eine existierende Zieldatei überschrieben werden kann (nicht gesperrt ist)."""
    if os.path.exists(path):
        try:
            with open(path, "r+b"):
                pass
        except PermissionError:
            return False
        except OSError:
            return False
    return True


class ProcessingCancelled(Exception):
    """Wird ausgelöst, wenn der Benutzer die Verarbeitung abbricht."""


# ============================================================================
# KONTEXT (Logging, Fortschritt, Abbruch)
# ============================================================================

class RunContext:
    """Bündelt Log-Ausgabe, Fortschrittsanzeige und Abbruchsignal."""

    def __init__(self, log_fn=None, progress_fn=None, stop_event=None):
        self._log_fn = log_fn or (lambda s: print(s, flush=True))
        self._progress_fn = progress_fn or (lambda frac, stage, detail: None)
        self.stop_event = stop_event or threading.Event()
        self.warnings = []
        self.failed_reads = []
        self._logfile = None
        self._lock = threading.Lock()

    def log(self, msg=""):
        self._log_fn(msg)
        with self._lock:
            if self._logfile:
                try:
                    self._logfile.write(msg + "\n")
                    self._logfile.flush()
                except Exception:
                    pass

    def warn(self, msg):
        self.warnings.append(msg)
        self.log("⚠ " + msg)

    def progress(self, frac, stage="", detail=""):
        """frac: 0..1 oder None (unbestimmte Dauer)."""
        self._progress_fn(frac, stage, detail)

    def check_stop(self):
        if self.stop_event.is_set():
            raise ProcessingCancelled()

    def open_logfile(self, path):
        try:
            self._logfile = open(path, "w", encoding="utf-8")
        except Exception:
            self._logfile = None

    def close_logfile(self):
        with self._lock:
            if self._logfile:
                try:
                    self._logfile.close()
                except Exception:
                    pass
                self._logfile = None


class GdalLogHandler(logging.Handler):
    """Leitet GDAL/rasterio-Warnungen ins Log (gleiche Meldungen max. 3x)."""

    def __init__(self, ctx):
        super().__init__(logging.WARNING)
        self.ctx = ctx
        self.seen = Counter()

    def emit(self, record):
        try:
            msg = record.getMessage()
            self.seen[msg] += 1
            if self.seen[msg] <= 3:
                self.ctx.log(f"  [GDAL] {msg}")
            elif self.seen[msg] == 4:
                self.ctx.log("  [GDAL] (weitere gleichlautende Meldungen werden unterdrückt)")
        except Exception:
            pass


# ============================================================================
# EINSTELLUNGEN -> VERARBEITUNGS-KONFIGURATION
# ============================================================================

def parse_settings(s):
    """Wandelt die (String-)Einstellungen der GUI in eine geprüfte Konfiguration um.
    Löst ValueError mit verständlicher Meldung aus."""
    st = dict(DEFAULT_SETTINGS)
    st.update(s or {})

    cfg = {
        "input_folder": str(st["input_folder"]).strip(),
        "recursive": bool(st["recursive"]),
        "output_file": str(st["output_file"]).strip(),
        "temp_folder": str(st["temp_folder"]).strip() or None,
        "output_format": st["format_str"] if st["format_str"] in FORMATS else "COG",
        "layout": st["layout_str"] if st["layout_str"] in LAYOUTS else "single",
        "overviews": bool(st["overviews"]),
        "overlap": st["overlap_str"] if st["overlap_str"] in OVERLAP_MODES else "first",
    }

    comp = str(st["compression_str"]).strip().upper()
    if comp in ("NONE", ""):
        cfg["compression"] = None
    elif comp in COMPRESSIONS:
        cfg["compression"] = comp
    else:  # z.B. alte Einstellung "JPEG" (für Höhendaten ungeeignet)
        cfg["compression"] = "DEFLATE"

    try:
        cfg["lerc_error"] = max(0.0, float(str(st["lerc_error_str"]).replace(",", ".")))
    except ValueError:
        raise ValueError("LERC-Toleranz muss eine Zahl sein (z.B. 0.01 für 1 cm, 0 = verlustfrei).")

    try:
        cfg["tile_km"] = float(str(st["tile_km_str"]).replace(",", "."))
        if cfg["tile_km"] <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("Kachelgröße muss eine positive Zahl (km) sein.")

    try:
        cfg["blocksize"] = int(st["blocksize_str"])
        if cfg["blocksize"] % 16 != 0 or cfg["blocksize"] < 128:
            raise ValueError
    except ValueError:
        raise ValueError("Blockgröße muss ein Vielfaches von 16 (z.B. 512) sein.")

    ovr = str(st["ovr_resampling_str"]).strip()
    cfg["ovr_resampling"] = ovr if ovr in OVR_RESAMPLINGS else "average"

    tr = str(st["target_res_str"]).strip().replace(",", ".")
    if tr.lower() in ("", "original", "auto"):
        cfg["target_res"] = None
    else:
        try:
            cfg["target_res"] = float(tr)
            if cfg["target_res"] <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Zielauflösung muss 'Original' oder eine positive Zahl (Meter) sein.")

    rs = str(st["resampling_str"]).strip()
    cfg["resampling_name"] = rs if rs in RESAMPLINGS else "nearest"
    cfg["resampling"] = Resampling[cfg["resampling_name"]]

    nd = str(st["nodata_str"]).strip().replace(",", ".")
    if nd.lower() in ("", "auto"):
        cfg["nodata"] = None
    elif nd.lower() == "nan":
        cfg["nodata"] = float("nan")
    else:
        try:
            cfg["nodata"] = float(nd)
        except ValueError:
            raise ValueError("NoData-Wert muss 'Auto', 'nan' oder eine Zahl sein.")

    n = str(st["threads_str"]).strip()
    try:
        cfg["threads"] = max(1, int(n))
    except ValueError:
        cfg["threads"] = multiprocessing.cpu_count()

    total_ram, _ = memory_info()
    r = str(st["ram_str"]).strip().replace(",", ".")
    try:
        cfg["ram_bytes"] = int(float(r) * 1024 ** 3)
    except ValueError:
        cfg["ram_bytes"] = int(min(8 * 1024 ** 3, max(1024 ** 3, total_ram * 0.25)))
    cfg["ram_bytes"] = max(256 * 1024 ** 2, cfg["ram_bytes"])

    # Dateiendung passend zum Format
    if cfg["output_file"]:
        p = Path(cfg["output_file"])
        if cfg["output_format"] == "VRT":
            p = p.with_suffix(".vrt")
        elif p.suffix.lower() not in (".tif", ".tiff"):
            p = p.with_suffix(".tif")
        cfg["output_file"] = str(p)
    return cfg


# ============================================================================
# METADATEN EINLESEN
# ============================================================================

@dataclass
class TileInfo:
    path: str
    width: int
    height: int
    count: int
    dtype: str
    nodata: object
    crs: object
    crs_wkt: str
    left: float
    bottom: float
    right: float
    top: float
    resx: float
    resy: float
    blockx: int
    blocky: int
    area_or_point: str


@dataclass
class Placement:
    """Lage einer Eingangskachel im Zielraster (in Zielpixeln)."""
    tile: TileInfo
    idx: int          # Reihenfolge (für Überlappungsregel)
    row: int
    col: int
    h: int
    w: int
    direct: bool      # True = gleiche Auflösung & pixelgenau ausgerichtet


@dataclass
class Grid:
    transform: object
    width: int
    height: int
    crs: object
    count: int
    dtype: str
    nodata: object

    @property
    def resx(self):
        return self.transform.a

    @property
    def resy(self):
        return -self.transform.e


@dataclass
class MosaicPlan:
    tiles: list
    grid: Grid
    placements: list


def find_tiff_files(folder, recursive=True, ctx=None):
    """Sucht nach TIFF-Dateien im angegebenen Ordner"""
    folder_path = Path(folder)
    files = set()
    for ext in ("*.tif", "*.tiff", "*.TIF", "*.TIFF"):
        it = folder_path.rglob(ext) if recursive else folder_path.glob(ext)
        for f in it:
            if f.is_file():
                files.add(str(f))
    return sorted(files, key=lambda p: p.lower())


def read_tile_meta(path):
    """Liest nur den Header einer TIFF-Datei (schnell, kein Pixelzugriff)."""
    try:
        with rasterio.open(path) as src:
            if src.crs is None:
                return path, None, "kein Koordinatensystem (CRS) vorhanden"
            t = src.transform
            if t.b != 0 or t.d != 0:
                return path, None, "gedrehte Raster werden nicht unterstützt"
            if t == Affine.identity():
                return path, None, "keine Georeferenzierung vorhanden"
            b = src.bounds
            bx, by = src.block_shapes[0][1], src.block_shapes[0][0]
            info = TileInfo(
                path=path, width=src.width, height=src.height, count=src.count,
                dtype=src.dtypes[0], nodata=src.nodata, crs=src.crs,
                crs_wkt=src.crs.to_wkt(), left=b.left, bottom=b.bottom,
                right=b.right, top=b.top, resx=abs(t.a), resy=abs(t.e),
                blockx=bx, blocky=by,
                area_or_point=src.tags().get("AREA_OR_POINT", "Area"),
            )
            return path, info, None
    except Exception as e:
        return path, None, str(e)


def collect_metadata(paths, threads, ctx):
    """Liest die Header aller Dateien parallel."""
    ctx.log(f"\nLese Metadaten von {len(paths)} Dateien mit {threads} Threads …")
    tiles, errors = [], []
    n = len(paths)
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(read_tile_meta, p) for p in paths]
        try:
            for i, fut in enumerate(as_completed(futures), 1):
                path, info, err = fut.result()
                if info is None:
                    errors.append((path, err))
                    ctx.log(f"⚠ Überspringe {os.path.basename(path)}: {err}")
                else:
                    tiles.append(info)
                if i % 25 == 0 or i == n:
                    ctx.progress(i / n, "Metadaten lesen", f"{i}/{n}")
                if ctx.stop_event.is_set():
                    for f in futures:
                        f.cancel()
                    raise ProcessingCancelled()
        finally:
            pass
    tiles.sort(key=lambda t: t.path.lower())
    ctx.log(f"✓ {len(tiles)} lesbare Dateien, {len(errors)} fehlerhaft")
    return tiles, errors


# ============================================================================
# ANALYSE / PLANUNG
# ============================================================================

def make_placements(tiles, transform, first_idx=None):
    """Berechnet die Lage jeder Kachel im Zielraster."""
    L, T = transform.c, transform.f
    rx, ry = transform.a, -transform.e
    out = []
    for i, t in enumerate(tiles):
        idx = first_idx[i] if first_idx is not None else i
        same_res = abs(t.resx - rx) <= 1e-6 * rx and abs(t.resy - ry) <= 1e-6 * ry
        cf = (t.left - L) / rx
        rf = (T - t.top) / ry
        if same_res and abs(cf - round(cf)) < 1e-3 and abs(rf - round(rf)) < 1e-3:
            out.append(Placement(t, idx, int(round(rf)), int(round(cf)), t.height, t.width, True))
        else:
            c0 = math.floor(cf + 1e-6)
            r0 = math.floor(rf + 1e-6)
            c1 = math.ceil((t.right - L) / rx - 1e-6)
            r1 = math.ceil((T - t.bottom) / ry - 1e-6)
            out.append(Placement(t, idx, r0, c0, max(1, r1 - r0), max(1, c1 - c0), False))
    return out


def _check_nodata_for_dtype(nodata, dtype):
    dt = np.dtype(dtype)
    if np.issubdtype(dt, np.integer):
        if is_nan(nodata) or float(nodata) != int(nodata):
            raise ValueError(f"NoData-Wert {nodata} ist für Ganzzahl-Datentyp {dtype} ungültig.")
        info = np.iinfo(dt)
        if not (info.min <= int(nodata) <= info.max):
            raise ValueError(f"NoData-Wert {nodata} liegt außerhalb des Wertebereichs von {dtype} "
                             f"({info.min} … {info.max}).")


def build_plan(tiles, cfg, ctx):
    """Prüft die Kacheln, legt das Zielraster fest und gibt einen Bericht aus."""
    ctx.log("\n" + "=" * 70)
    ctx.log("ANALYSE")
    ctx.log("=" * 70)

    # --- Koordinatensystem: häufigstes CRS als Referenz ---
    crs_count = Counter(t.crs_wkt for t in tiles)
    ref_wkt = crs_count.most_common(1)[0][0]
    ref_crs = next(t.crs for t in tiles if t.crs_wkt == ref_wkt)
    bad = [t for t in tiles if t.crs_wkt != ref_wkt]
    if bad:
        ctx.warn(f"{len(bad)} Kachel(n) mit abweichendem CRS werden übersprungen:")
        for t in bad[:10]:
            ctx.log(f"    - {os.path.basename(t.path)}: {crs_label(t.crs)}")
        if len(bad) > 10:
            ctx.log(f"    … und {len(bad) - 10} weitere")
        tiles = [t for t in tiles if t.crs_wkt == ref_wkt]

    # --- Bandanzahl ---
    count = Counter(t.count for t in tiles).most_common(1)[0][0]
    bad = [t for t in tiles if t.count != count]
    if bad:
        ctx.warn(f"{len(bad)} Kachel(n) mit abweichender Bandanzahl (≠ {count}) werden übersprungen.")
        tiles = [t for t in tiles if t.count == count]

    # --- Datentyp ---
    dtypes = sorted({t.dtype for t in tiles})
    dtype = np.result_type(*[np.dtype(d) for d in dtypes]).name
    if len(dtypes) > 1:
        ctx.warn(f"Unterschiedliche Datentypen {dtypes} – Ausgabe als {dtype}.")

    # --- NoData ---
    if cfg["nodata"] is None:
        nds = [t.nodata for t in tiles if t.nodata is not None]
        if nds:
            nodata = Counter(float(n) if not is_nan(n) else "nan" for n in nds).most_common(1)[0][0]
            nodata = float("nan") if nodata == "nan" else nodata
        else:
            nodata = -9999.0 if np.issubdtype(np.dtype(dtype), np.floating) else 0
            ctx.warn(f"Eingangsdaten haben keinen NoData-Wert – verwende {nodata}.")
    else:
        nodata = cfg["nodata"]
    _check_nodata_for_dtype(nodata, dtype)
    if np.issubdtype(np.dtype(dtype), np.integer):
        nodata = int(nodata)

    # --- Auflösung ---
    if cfg["target_res"] is None:
        resx, resy = Counter((round(t.resx, 9), round(t.resy, 9)) for t in tiles).most_common(1)[0][0]
    else:
        resx = resy = cfg["target_res"]
    other_res = sum(1 for t in tiles
                    if abs(t.resx - resx) > 1e-6 * resx or abs(t.resy - resy) > 1e-6 * resy)

    # --- Zielraster ---
    minx = min(t.left for t in tiles)
    maxx = max(t.right for t in tiles)
    miny = min(t.bottom for t in tiles)
    maxy = max(t.top for t in tiles)
    if cfg["target_res"] is not None:
        left = math.floor(minx / resx + 1e-9) * resx
        top = math.ceil(maxy / resy - 1e-9) * resy
    else:
        left, top = minx, maxy
    width = int(math.ceil((maxx - left) / resx - 1e-6))
    height = int(math.ceil((top - miny) / resy - 1e-6))
    transform = Affine(resx, 0.0, left, 0.0, -resy, top)
    grid = Grid(transform, width, height, ref_crs, count, dtype, nodata)

    placements = make_placements(tiles, transform)
    n_warp = sum(1 for p in placements if not p.direct)

    # --- Überlappungen (auf Pixelebene des Zielrasters) ---
    r0 = np.array([p.row for p in placements], dtype=np.int64)
    c0 = np.array([p.col for p in placements], dtype=np.int64)
    r1 = r0 + np.array([p.h for p in placements], dtype=np.int64)
    c1 = c0 + np.array([p.w for p in placements], dtype=np.int64)
    overlap_pairs = 0
    examples = []
    for i in range(len(placements) - 1):
        m = (r0[i + 1:] < r1[i]) & (r1[i + 1:] > r0[i]) & (c0[i + 1:] < c1[i]) & (c1[i + 1:] > c0[i])
        k = int(m.sum())
        if k:
            overlap_pairs += k
            if len(examples) < 5:
                j = i + 1 + int(np.argmax(m))
                examples.append((os.path.basename(placements[i].tile.path),
                                 os.path.basename(placements[j].tile.path)))

    # --- Isolierte Kacheln (Hinweis auf falsche Koordinaten) ---
    isolated = []
    if len(tiles) >= 3:
        cx = np.array([(t.left + t.right) / 2 for t in tiles])
        cy = np.array([(t.bottom + t.top) / 2 for t in tiles])
        nn = np.empty(len(tiles))
        for s in range(0, len(tiles), 512):
            d2 = (cx[s:s + 512, None] - cx[None, :]) ** 2 + (cy[s:s + 512, None] - cy[None, :]) ** 2
            for k in range(d2.shape[0]):
                d2[k, s + k] = np.inf
            nn[s:s + 512] = np.sqrt(d2.min(axis=1))
        tile_size = float(np.median([max(t.right - t.left, t.top - t.bottom) for t in tiles]))
        thresh = max(5 * float(np.median(nn)), 5 * tile_size)
        isolated = [(tiles[i], nn[i]) for i in np.where(nn > thresh)[0]]

    # --- Bericht ---
    itemsize = np.dtype(dtype).itemsize
    full_bytes = width * height * count * itemsize
    tile_px = sum(min(p.h, height) * min(p.w, width) for p in placements)
    coverage = tile_px / max(1, width * height) * 100
    ctx.log(f"Kacheln im Mosaik:   {len(tiles)}")
    ctx.log(f"Koordinatensystem:   {crs_label(ref_crs)}")
    ctx.log(f"Auflösung:           {resx:g} x {resy:g} m")
    ctx.log(f"Datentyp / Bänder:   {dtype} / {count}")
    ctx.log(f"NoData-Wert:         {nodata}")
    ctx.log(f"Ausdehnung:          X {left:.2f} – {left + width * resx:.2f}, "
            f"Y {top - height * resy:.2f} – {top:.2f}")
    ctx.log(f"                     ({width * resx / 1000:.2f} km x {height * resy / 1000:.2f} km)")
    ctx.log(f"Zielraster:          {width:,} x {height:,} Pixel".replace(",", "."))
    ctx.log(f"Abdeckung:           {coverage:.1f} % der Bounding-Box enthalten Kacheln")
    ctx.log(f"Unkomprimiert:       {fmt_bytes(full_bytes)} (gesamte Box) / "
            f"{fmt_bytes(tile_px * count * itemsize)} (nur Kachelflächen)")
    ctx.log(f"Hinweis: Durch Kachelung + SPARSE_OK werden leere Bereiche nicht gespeichert.")
    if other_res or n_warp:
        ctx.warn(f"{max(other_res, n_warp)} Kachel(n) mit abweichender Auflösung/Lage werden per "
                 f"Resampling '{cfg['resampling_name']}' an das Zielraster angepasst.")
    if overlap_pairs:
        ctx.warn(f"{overlap_pairs} überlappende Kachelpaare erkannt. Regel: "
                 f"{OVERLAP_MODES[cfg['overlap']]} (NoData wird dabei nie übernommen).")
        for a, b in examples:
            ctx.log(f"    - {a}  ↔  {b}")
    else:
        ctx.log("Überlappungen:       keine")
    if isolated:
        ctx.warn(f"{len(isolated)} isoliert liegende Kachel(n) gefunden – evtl. falsche Koordinaten? "
                 f"Diese vergrößern die Ausdehnung des Mosaiks:")
        for t, d in isolated[:10]:
            ctx.log(f"    - {os.path.basename(t.path)} (nächste Kachel {d / 1000:.1f} km entfernt)")
    ctx.log("=" * 70)
    return MosaicPlan(tiles, grid, placements)


# ============================================================================
# SCHREIBOPTIONEN
# ============================================================================

def _is_float(dtype):
    return np.issubdtype(np.dtype(dtype), np.floating)


def gtiff_creation_options(cfg, dtype):
    B = cfg["blocksize"]
    o = {
        "tiled": True, "blockxsize": B, "blockysize": B,
        "BIGTIFF": "YES", "SPARSE_OK": "TRUE",
        "NUM_THREADS": str(cfg["threads"]),
    }
    comp = cfg["compression"]
    if comp:
        o["compress"] = comp
        if comp in ("DEFLATE", "ZSTD", "LZW"):
            o["predictor"] = 3 if _is_float(dtype) else 2
        if comp == "DEFLATE":
            o["zlevel"] = 6
        if comp == "ZSTD":
            o["zstd_level"] = 9
        if comp.startswith("LERC"):
            o["max_z_error"] = cfg["lerc_error"]
    return o


def cog_creation_options(cfg, dtype):
    o = {
        "BLOCKSIZE": cfg["blocksize"], "BIGTIFF": "YES", "SPARSE_OK": "TRUE",
        "NUM_THREADS": str(cfg["threads"]),
        "OVERVIEWS": "AUTO" if cfg["overviews"] else "NONE",
        "OVERVIEW_RESAMPLING": cfg["ovr_resampling"],
    }
    comp = cfg["compression"]
    o["COMPRESS"] = comp or "NONE"
    if comp in ("DEFLATE", "ZSTD", "LZW"):
        o["PREDICTOR"] = "FLOATING_POINT" if _is_float(dtype) else "STANDARD"
    if comp == "DEFLATE":
        o["LEVEL"] = 6
    if comp == "ZSTD":
        o["LEVEL"] = 9
    if comp and comp.startswith("LERC"):
        o["MAX_Z_ERROR"] = cfg["lerc_error"]
        o["MAX_Z_ERROR_OVERVIEW"] = cfg["lerc_error"]
    return o


def overview_env(cfg, dtype):
    """GDAL-Konfiguration für das Berechnen der Overviews."""
    env = {
        "BIGTIFF_OVERVIEW": "YES",
        "GDAL_TIFF_OVR_BLOCKSIZE": str(cfg["blocksize"]),
        "SPARSE_OK_OVERVIEW": "ON",
        "GDAL_NUM_THREADS": str(cfg["threads"]),
    }
    comp = cfg["compression"]
    if comp:
        env["COMPRESS_OVERVIEW"] = comp
        if comp in ("DEFLATE", "ZSTD", "LZW"):
            env["PREDICTOR_OVERVIEW"] = "3" if _is_float(dtype) else "2"
        if comp == "DEFLATE":
            env["ZLEVEL_OVERVIEW"] = "6"
        if comp == "ZSTD":
            env["ZSTD_LEVEL_OVERVIEW"] = "9"
        if comp.startswith("LERC"):
            env["MAX_Z_ERROR_OVERVIEW"] = str(cfg["lerc_error"])
    return env


def overview_factors(width, height, blocksize):
    factors, f = [], 2
    while max(width, height) / f >= blocksize / 2 and f <= 4096:
        factors.append(f)
        f *= 2
    return factors


# ============================================================================
# KERN: SPEICHERSCHONENDES MOSAIKIEREN
# ============================================================================

def _valid_mask(a, nodata):
    if nodata is None:
        return np.ones(a.shape, dtype=bool)
    if is_nan(nodata):
        return ~np.isnan(a)
    return a != nodata


def _normalize(data, src_nodata, dtype, nodata):
    """Wandelt in den Zieldatentyp und setzt alle ungültigen Pixel auf den Ziel-NoData-Wert."""
    invalid = None
    if src_nodata is not None:
        if is_nan(src_nodata):
            invalid = np.isnan(data)
        elif is_nan(nodata) or float(src_nodata) != float(nodata) or data.dtype != np.dtype(dtype):
            invalid = data == src_nodata
    if np.issubdtype(data.dtype, np.floating) and not is_nan(nodata):
        nan_mask = np.isnan(data)
        if nan_mask.any():
            invalid = nan_mask if invalid is None else (invalid | nan_mask)
    out = data.astype(dtype, copy=False)
    if invalid is not None and invalid.any():
        if not out.flags.writeable:
            out = out.copy()
        out[invalid] = nodata
    return out


def _paste(dst, src, nodata, mode):
    """Setzt src in dst ein – NoData überschreibt nie gültige Werte."""
    vs = _valid_mask(src, nodata)
    if mode == "last":
        m = vs
    else:
        vd = _valid_mask(dst, nodata)
        if mode == "max":
            m = vs & (~vd | (src > dst))
        elif mode == "min":
            m = vs & (~vd | (src < dst))
        else:  # first
            m = vs & ~vd
    np.copyto(dst, src, where=m)


def _read_task(task, grid, resampling, stop_event):
    """Liest den benötigten Ausschnitt einer Kachel (mit Wiederholung bei Netzfehlern)."""
    pl, gr0, gr1, gc0, gc1 = task
    t = pl.tile
    win = Window(gc0 - pl.col, gr0 - pl.row, gc1 - gc0, gr1 - gr0)
    last_err = None
    for attempt in range(READ_RETRIES):
        if stop_event.is_set():
            return None, "abgebrochen"
        try:
            with rasterio.open(t.path) as src:
                if pl.direct:
                    data = src.read(window=win)
                    src_nodata = src.nodata
                else:
                    tf = grid.transform * Affine.translation(pl.col, pl.row)
                    with WarpedVRT(src, crs=src.crs, transform=tf, width=pl.w, height=pl.h,
                                   resampling=resampling, nodata=grid.nodata,
                                   dtype=grid.dtype) as vrt:
                        data = vrt.read(window=win)
                    src_nodata = grid.nodata
            return _normalize(data, src_nodata, grid.dtype, grid.nodata), None
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    return None, str(last_err)


class _Stats:
    """Inkrementelle Statistik (min/max/mittel/std) über alle gültigen Pixel."""

    def __init__(self, count):
        self.n = np.zeros(count, dtype=np.int64)
        self.min = np.full(count, np.inf)
        self.max = np.full(count, -np.inf)
        self.sum = np.zeros(count)
        self.sumsq = np.zeros(count)

    def update(self, arr, valid):
        for b in range(arr.shape[0]):
            v = arr[b][valid[b]]
            if v.size == 0:
                continue
            self.n[b] += v.size
            self.min[b] = min(self.min[b], float(v.min()))
            self.max[b] = max(self.max[b], float(v.max()))
            step = 1 << 20
            for s in range(0, v.size, step):
                part = v[s:s + step].astype(np.float64)
                self.sum[b] += part.sum()
                self.sumsq[b] += float(np.dot(part, part))

    def tags(self, b, total_px):
        if self.n[b] == 0:
            return {}
        mean = self.sum[b] / self.n[b]
        var = max(0.0, self.sumsq[b] / self.n[b] - mean * mean)
        return {
            "STATISTICS_MINIMUM": f"{self.min[b]:.6g}",
            "STATISTICS_MAXIMUM": f"{self.max[b]:.6g}",
            "STATISTICS_MEAN": f"{mean:.6g}",
            "STATISTICS_STDDEV": f"{math.sqrt(var):.6g}",
            "STATISTICS_VALID_PERCENT": f"{self.n[b] / max(1, total_px) * 100:.4g}",
        }


def write_mosaic(placements, grid, out_path, cfg, ctx, prog, area_or_point="Area"):
    """Schreibt das Mosaik streifenweise in ein gekacheltes BigTIFF.

    Der Speicherbedarf hängt nur von der Streifenhöhe und der Rasterbreite ab,
    nicht von der Gesamtgröße. Geschrieben werden ausschließlich vollständige,
    block-ausgerichtete Bereiche – jeder Block wird genau einmal komprimiert.
    Rückgabe: Anzahl geschriebener Chunks (0 = Ergebnis komplett leer).
    """
    B = cfg["blocksize"]
    W, H = grid.width, grid.height
    count = grid.count
    itemsize = np.dtype(grid.dtype).itemsize
    chunk_cols = max(B, (CHUNK_COLS // B) * B)
    max_chunk_blocks = chunk_cols // B

    # Streifenhöhe aus RAM-Budget: 2 Streifen Lesedaten (Vorladen) + 1 Chunk
    per_row = count * itemsize * (2 * W + chunk_cols) * 1.3
    hb = int(cfg["ram_bytes"] // per_row) // B * B
    hb = max(B, min(hb, 16 * B))

    # Lese-Aufgaben pro Streifen
    placements = sorted(placements, key=lambda p: p.idx)
    bands, total_px = [], 0
    for r0 in range(0, H, hb):
        r1 = min(H, r0 + hb)
        tasks = []
        for pl in placements:
            gr0, gr1 = max(r0, pl.row), min(r1, pl.row + pl.h)
            if gr0 >= gr1:
                continue
            gc0, gc1 = max(0, pl.col), min(W, pl.col + pl.w)
            if gc0 >= gc1:
                continue
            tasks.append((pl, gr0, gr1, gc0, gc1))
            total_px += (gr1 - gr0) * (gc1 - gc0)
        if tasks:
            bands.append((r0, r1, tasks))

    ctx.log(f"  Streifenhöhe: {hb} Zeilen | {len(bands)} Streifen mit Daten | "
            f"RAM-Budget: {fmt_bytes(cfg['ram_bytes'])}")

    profile = dict(driver="GTiff", width=W, height=H, count=count, dtype=grid.dtype,
                   crs=grid.crs, transform=grid.transform, nodata=grid.nodata)
    profile.update(gtiff_creation_options(cfg, grid.dtype))

    remove_raster(out_path, strict=True)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    stats = _Stats(count)
    done_px = 0
    chunks_written = 0
    t0 = time.time()
    pool = ThreadPoolExecutor(max_workers=cfg["threads"])
    ok = False

    def submit(band):
        return [(task, pool.submit(_read_task, task, grid, cfg["resampling"], ctx.stop_event))
                for task in band[2]]

    try:
        with rasterio.open(out_path, "w", **profile) as dst:
            pending = submit(bands[0]) if bands else None
            for bi, band in enumerate(bands):
                ctx.check_stop()
                r0, r1, tasks = band
                current = pending
                pending = submit(bands[bi + 1]) if bi + 1 < len(bands) else None

                # Daten des aktuellen Streifens einsammeln
                results = []
                for task, fut in current:
                    data, err = fut.result()
                    if data is None:
                        if ctx.stop_event.is_set():
                            raise ProcessingCancelled()
                        name = os.path.basename(task[0].tile.path)
                        ctx.warn(f"Lesefehler bei {name} (Zeilen {task[1]}–{task[2]}): {err} "
                                 f"– Bereich bleibt NoData.")
                        ctx.failed_reads.append(task[0].tile.path)
                        continue
                    results.append((task, data))

                # Block-Spalten bestimmen, die Daten enthalten -> Chunks
                ivs = sorted({(t[3] // B, -(-t[4] // B)) for t, _ in results})
                runs = []
                for a, b in ivs:
                    if runs and a <= runs[-1][1]:
                        runs[-1][1] = max(runs[-1][1], b)
                    else:
                        runs.append([a, b])
                chunks = []
                for a, b in runs:
                    for s in range(a, b, max_chunk_blocks):
                        chunks.append((s * B, min(min(s + max_chunk_blocks, b) * B, W)))

                rh = r1 - r0
                for c0, c1 in chunks:
                    arr = np.full((count, rh, c1 - c0), grid.nodata, dtype=grid.dtype)
                    for (pl, gr0, gr1, gc0, gc1), data in results:
                        ia, ib = max(gc0, c0), min(gc1, c1)
                        if ia >= ib:
                            continue
                        _paste(arr[:, gr0 - r0:gr1 - r0, ia - c0:ib - c0],
                               data[:, :, ia - gc0:ib - gc0], grid.nodata, cfg["overlap"])
                    valid = _valid_mask(arr, grid.nodata)
                    if not valid.any():
                        continue  # komplett leer -> nichts schreiben (sparse)
                    stats.update(arr, valid)
                    dst.write(arr, window=Window(c0, r0, c1 - c0, rh))
                    chunks_written += 1
                    del arr, valid

                done_px += sum((t[2] - t[1]) * (t[4] - t[3]) for t in tasks)
                del results
                frac = done_px / max(1, total_px)
                elapsed = time.time() - t0
                prog(frac, f"Streifen {bi + 1}/{len(bands)}")
                if (bi + 1) % 5 == 0 or bi + 1 == len(bands):
                    eta = elapsed / frac * (1 - frac) if frac > 0 else 0
                    ctx.log(f"  Streifen {bi + 1}/{len(bands)} ({frac * 100:5.1f} %) – "
                            f"vergangen {fmt_duration(elapsed)}, Rest ca. {fmt_duration(eta)}")

            # Statistik & Metadaten
            dst.update_tags(AREA_OR_POINT=area_or_point)
            for b in range(count):
                tags = stats.tags(b, W * H)
                if tags:
                    dst.update_tags(b + 1, **tags)
        ok = True
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        if not ok:
            remove_raster(out_path)

    if stats.n[0]:
        ctx.log(f"  Wertebereich Band 1: {stats.min[0]:.3f} … {stats.max[0]:.3f} "
                f"(gültige Pixel: {stats.n[0]:,})".replace(",", "."))
    return chunks_written


def build_overviews_inplace(path, cfg, dtype, ctx):
    """Erzeugt interne Overviews (Pyramiden) in einer bestehenden GeoTIFF-Datei."""
    with rasterio.Env(**overview_env(cfg, dtype)):
        with rasterio.open(path, "r+") as ds:
            factors = overview_factors(ds.width, ds.height, cfg["blocksize"])
            if not factors:
                return
            ctx.log(f"  Overview-Stufen: {factors} (Resampling: {cfg['ovr_resampling']})")
            ds.build_overviews(factors, Resampling[cfg["ovr_resampling"]])
            ds.update_tags(ns="rio_overview", resampling=cfg["ovr_resampling"])


def convert_to_cog(src_path, dst_path, cfg, dtype, ctx):
    """Konvertiert ein gekacheltes GeoTIFF in ein Cloud Optimized GeoTIFF."""
    remove_raster(dst_path, strict=True)
    opts = cog_creation_options(cfg, dtype)
    with rasterio.Env(GDAL_NUM_THREADS=str(cfg["threads"])):
        rio_copy(src_path, dst_path, driver="COG", **opts)


def copy_with_progress(src, dst, ctx, prog):
    """Kopiert eine (große) Datei blockweise mit Fortschritt (z.B. auf Netzlaufwerk)."""
    total = os.path.getsize(src)
    part = dst + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    done = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                ctx.check_stop()
                buf = fi.read(32 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                prog(done / max(1, total), f"{fmt_bytes(done)} / {fmt_bytes(total)}")
        remove_raster(dst)
        os.replace(part, dst)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    for sc_src, sc_dst in zip(sidecar_files(src), sidecar_files(dst)):
        if os.path.exists(sc_src):
            shutil.copyfile(sc_src, sc_dst)


def produce_raster(placements, grid, final_path, cfg, ctx, prog, area_or_point="Area", quiet=False):
    """Kompletter Ablauf für EINE Ausgabedatei: Mosaik -> Overviews/COG -> ggf. kopieren.
    prog(frac|None, stage, detail). Rückgabe: Pfad oder None (wenn komplett leer)."""
    fmt = cfg["output_format"]
    final_dir = os.path.dirname(os.path.abspath(final_path))
    stem = Path(final_path).stem
    work_dir = os.path.abspath(cfg["temp_folder"]) if cfg["temp_folder"] else final_dir
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(final_dir, exist_ok=True)
    same_dir = os.path.normcase(work_dir) == os.path.normcase(final_dir)

    if fmt == "COG":
        raw = os.path.join(work_dir, stem + "__tmp_mosaik.tif")
        result = final_path if same_dir else os.path.join(work_dir, stem + ".tif")
        steps = 3 if not same_dir else 2
    else:
        raw = final_path if same_dir else os.path.join(work_dir, stem + ".tif")
        result = raw
        steps = (2 if cfg["overviews"] else 1) + (0 if same_dir else 1)

    log = (lambda m: None) if quiet else ctx.log
    step = 1
    temp_files = []
    try:
        # Schritt 1: Mosaik schreiben
        log(f"\nSchritt {step}/{steps}: Mosaik schreiben → {raw}")
        if raw != final_path:
            temp_files.append(raw)
        n = write_mosaic(placements, grid, raw, cfg, ctx,
                         lambda f, d: prog(f, f"Schritt {step}/{steps}: Mosaik schreiben", d),
                         area_or_point)
        if n == 0:
            log("  (keine gültigen Pixel – Datei wird nicht erstellt)")
            remove_raster(raw)
            return None
        log(f"  ✓ Mosaik geschrieben ({fmt_bytes(os.path.getsize(raw))})")
        ctx.check_stop()

        # Schritt 2: Overviews bzw. COG
        if fmt == "COG":
            step += 1
            log(f"\nSchritt {step}/{steps}: Cloud Optimized GeoTIFF erstellen"
                f"{' inkl. Overviews' if cfg['overviews'] else ''} → {result}")
            log("  (dieser Schritt kann nicht unterbrochen werden und zeigt keinen Fortschritt)")
            prog(None, f"Schritt {step}/{steps}: COG erstellen", "bitte warten …")
            t = time.time()
            if result != final_path:
                temp_files.append(result)
            convert_to_cog(raw, result, cfg, grid.dtype, ctx)
            remove_raster(raw)
            temp_files.remove(raw)
            log(f"  ✓ COG erstellt in {fmt_duration(time.time() - t)} ({fmt_bytes(os.path.getsize(result))})")
        elif cfg["overviews"]:
            step += 1
            log(f"\nSchritt {step}/{steps}: Overviews (Pyramiden) berechnen")
            prog(None, f"Schritt {step}/{steps}: Overviews berechnen", "bitte warten …")
            t = time.time()
            build_overviews_inplace(raw, cfg, grid.dtype, ctx)
            log(f"  ✓ Overviews erstellt in {fmt_duration(time.time() - t)} "
                f"({fmt_bytes(os.path.getsize(raw))})")
        ctx.check_stop()

        # Schritt 3: aus Temp-Ordner an Zielort kopieren
        if result != final_path:
            step += 1
            log(f"\nSchritt {step}/{steps}: Kopiere an Zielort → {final_path}")
            copy_with_progress(result, final_path, ctx,
                               lambda f, d: prog(f, f"Schritt {step}/{steps}: Kopieren", d))
            remove_raster(result)
            temp_files.remove(result)
            log("  ✓ Kopiert")
        return final_path
    except PermissionError:
        for p in temp_files:
            remove_raster(p)
        raise
    except BaseException:
        for p in temp_files:
            remove_raster(p)
        remove_raster(final_path)
        raise


# ============================================================================
# VRT (virtuelles Mosaik)
# ============================================================================

def write_vrt(vrt_path, tiles, grid, overlap="last", resampling_name="nearest"):
    """Schreibt ein GDAL-VRT, das die Kacheln ohne Kopie zu einem Mosaik verbindet.
    Bei 'ComplexSource' + NODATA überschreiben spätere Quellen nur gültige Pixel."""
    L, T = grid.transform.c, grid.transform.f
    rx, ry = grid.resx, grid.resy
    order = list(tiles)
    if overlap == "first":
        order = order[::-1]  # zuerst gelesene Kachel muss im VRT zuletzt stehen
    vrt_dir = os.path.dirname(os.path.abspath(vrt_path))

    def nd_str(v):
        return "nan" if is_nan(v) else repr(float(v)) if isinstance(v, float) else str(v)

    lines = [f'<VRTDataset rasterXSize="{grid.width}" rasterYSize="{grid.height}">',
             f'  <SRS dataAxisToSRSAxisMapping="1,2">{xml_escape(grid.crs.to_wkt())}</SRS>',
             f'  <GeoTransform>{L!r}, {rx!r}, 0.0, {T!r}, 0.0, {-ry!r}</GeoTransform>']
    gdal_dt = GDAL_DTYPES.get(grid.dtype, "Float32")
    for b in range(1, grid.count + 1):
        lines.append(f'  <VRTRasterBand dataType="{gdal_dt}" band="{b}">')
        if grid.nodata is not None:
            lines.append(f'    <NoDataValue>{nd_str(grid.nodata)}</NoDataValue>')
        for t in order:
            try:
                rel = os.path.relpath(t.path, vrt_dir)
                use_rel = not rel.startswith("..") or len(rel) < len(t.path)
            except ValueError:
                use_rel = False
            fname = rel if use_rel else os.path.abspath(t.path)
            xoff = (t.left - L) / rx
            yoff = (T - t.top) / ry
            xs = (t.right - t.left) / rx
            ys = (t.top - t.bottom) / ry
            same = abs(t.resx - rx) <= 1e-6 * rx and abs(t.resy - ry) <= 1e-6 * ry
            res_attr = "" if same else f' resampling="{resampling_name}"'
            lines.append(f'    <ComplexSource{res_attr}>')
            lines.append(f'      <SourceFilename relativeToVRT="{1 if use_rel else 0}">'
                         f'{xml_escape(fname)}</SourceFilename>')
            lines.append(f'      <SourceBand>{b}</SourceBand>')
            lines.append(f'      <SourceProperties RasterXSize="{t.width}" RasterYSize="{t.height}" '
                         f'DataType="{GDAL_DTYPES.get(t.dtype, "Float32")}" '
                         f'BlockXSize="{t.blockx}" BlockYSize="{t.blocky}"/>')
            lines.append(f'      <SrcRect xOff="0" yOff="0" xSize="{t.width}" ySize="{t.height}"/>')
            lines.append(f'      <DstRect xOff="{xoff:.10g}" yOff="{yoff:.10g}" '
                         f'xSize="{xs:.10g}" ySize="{ys:.10g}"/>')
            if t.nodata is not None:
                lines.append(f'      <NODATA>{nd_str(t.nodata)}</NODATA>')
            lines.append('    </ComplexSource>')
        lines.append('  </VRTRasterBand>')
    lines.append('</VRTDataset>')
    os.makedirs(vrt_dir, exist_ok=True)
    with open(vrt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ============================================================================
# AUSGABE-VARIANTEN
# ============================================================================

def run_single(plan, cfg, ctx):
    final = cfg["output_file"]
    area = plan.tiles[0].area_or_point if plan.tiles else "Area"

    def prog(f, stage, detail):
        ctx.progress(f, stage, detail)

    path = produce_raster(plan.placements, plan.grid, final, cfg, ctx, prog, area)
    return [path] if path else []


def run_vrt(plan, cfg, ctx):
    final = cfg["output_file"]
    if cfg["overlap"] in ("min", "max"):
        ctx.warn("Überlappungsregel Minimum/Maximum ist im VRT nicht möglich – "
                 "es gilt 'Letzte gültige Kachel überschreibt'.")
    ctx.log(f"\nSchreibe VRT → {final}")
    write_vrt(final, plan.tiles, plan.grid, cfg["overlap"], cfg["resampling_name"])
    ctx.log("  ✓ VRT geschrieben")
    if cfg["overviews"]:
        ctx.log("\nBerechne externe Overviews (.vrt.ovr) – liest alle Kacheln einmal komplett …")
        ctx.progress(None, "Overviews für VRT berechnen", "bitte warten …")
        t = time.time()
        with rasterio.Env(**overview_env(cfg, plan.grid.dtype)):
            with rasterio.open(final, "r+") as ds:
                factors = overview_factors(ds.width, ds.height, cfg["blocksize"])
                ctx.log(f"  Overview-Stufen: {factors}")
                ds.build_overviews(factors, Resampling[cfg["ovr_resampling"]])
        ctx.log(f"  ✓ Overviews erstellt in {fmt_duration(time.time() - t)}")
    return [final]


def run_tiles(plan, cfg, ctx):
    """Kachel-Mosaik: Ausgabe als Raster aus K x K km großen Dateien + VRT-Index."""
    final = cfg["output_file"]
    stem = Path(final).stem
    out_dir = os.path.join(os.path.dirname(os.path.abspath(final)), f"{stem}_kacheln")
    vrt_path = os.path.join(os.path.dirname(os.path.abspath(final)), f"{stem}.vrt")
    K = cfg["tile_km"] * 1000.0
    g = plan.grid
    rx, ry = g.resx, g.resy
    cw = int(round(K / rx))
    ch = int(round(K / ry))
    if abs(cw * rx - K) > 1e-6 * K or abs(ch * ry - K) > 1e-6 * K:
        ctx.warn(f"Kachelgröße {K:g} m ist kein Vielfaches der Auflösung – Kacheln werden aufgerundet.")
        cw = int(math.ceil(K / rx))
        ch = int(math.ceil(K / ry))

    # Ausgabezellen bestimmen
    cells = {}
    for i, t in enumerate(plan.tiles):
        ix0 = math.floor(t.left / K + 1e-9)
        ix1 = math.ceil(t.right / K - 1e-9)
        iy0 = math.floor(t.bottom / K + 1e-9)
        iy1 = math.ceil(t.top / K - 1e-9)
        for ix in range(ix0, ix1):
            for iy in range(iy0, iy1):
                cells.setdefault((ix, iy), []).append(i)
    keys = sorted(cells, key=lambda k: (-k[1], k[0]))
    n = len(keys)
    ctx.log(f"\nKachel-Mosaik: {n} Ausgabekacheln à {cfg['tile_km']:g} x {cfg['tile_km']:g} km "
            f"({cw} x {ch} Pixel) → {out_dir}")
    os.makedirs(out_dir, exist_ok=True)

    written = []
    area = plan.tiles[0].area_or_point if plan.tiles else "Area"
    digits = 0 if float(K / 1000).is_integer() else 1
    for ci, (ix, iy) in enumerate(keys):
        ctx.check_stop()
        x0, y1 = ix * K, (iy + 1) * K
        tf = Affine(rx, 0.0, x0, 0.0, -ry, y1)
        grid = Grid(tf, cw, ch, g.crs, g.count, g.dtype, g.nodata)
        idxs = cells[(ix, iy)]
        pls = make_placements([plan.tiles[i] for i in idxs], tf, first_idx=idxs)
        name = f"{stem}_{x0 / 1000:.{digits}f}_{iy * K / 1000:.{digits}f}.tif"
        path = os.path.join(out_dir, name)

        def prog(f, stage, detail, ci=ci):
            frac = (ci + (0.8 if f is None else f)) / n
            ctx.progress(frac, "Kachel-Mosaik", f"Kachel {ci + 1}/{n} – {stage}")

        t = time.time()
        res = produce_raster(pls, grid, path, cfg, ctx, prog, area, quiet=True)
        if res:
            written.append(res)
            ctx.log(f"  [{ci + 1}/{n}] {name}  ({len(idxs)} Eingangskacheln, "
                    f"{fmt_bytes(os.path.getsize(res))}, {fmt_duration(time.time() - t)})")
        else:
            ctx.log(f"  [{ci + 1}/{n}] {name}  übersprungen (nur NoData)")

    # VRT-Index über alle Ausgabekacheln
    if written:
        infos = [read_tile_meta(p)[1] for p in written]
        infos = [i for i in infos if i is not None]
        minx = min(i.left for i in infos)
        maxy = max(i.top for i in infos)
        maxx = max(i.right for i in infos)
        miny = min(i.bottom for i in infos)
        vgrid = Grid(Affine(rx, 0.0, minx, 0.0, -ry, maxy),
                     int(round((maxx - minx) / rx)), int(round((maxy - miny) / ry)),
                     g.crs, g.count, g.dtype, g.nodata)
        write_vrt(vrt_path, infos, vgrid, "last")
        ctx.log(f"\n✓ VRT-Index über alle Kacheln: {vrt_path}")
    return written


# ============================================================================
# HAUPTVERARBEITUNG
# ============================================================================

def run_processing(cfg, ctx, analyse_only=False):
    """Hauptverarbeitungsfunktion"""
    t_start = datetime.now()
    gdal_handler = GdalLogHandler(ctx)
    rio_logger = logging.getLogger("rasterio")
    rio_logger.addHandler(gdal_handler)
    if rio_logger.level == logging.NOTSET or rio_logger.level > logging.WARNING:
        rio_logger.setLevel(logging.WARNING)

    try:
        ctx.log("=" * 70)
        ctx.log(f"  TERRAIN TIFF RASTER MERGER {APP_VERSION}" + ("  –  ANALYSE" if analyse_only else ""))
        ctx.log("=" * 70)
        ctx.log(f"Start:        {t_start.strftime('%Y-%m-%d %H:%M:%S')}")
        ctx.log(f"Input-Ordner: {cfg['input_folder']}" + (" (inkl. Unterordner)" if cfg["recursive"] else ""))
        if not analyse_only:
            ctx.log(f"Ausgabe:      {cfg['output_file']}")
            ctx.log(f"Format:       {FORMATS[cfg['output_format']]}")
            if cfg["output_format"] != "VRT":
                ctx.log(f"Struktur:     {LAYOUTS[cfg['layout']]}"
                        + (f" ({cfg['tile_km']:g} km)" if cfg["layout"] == "tiles" else ""))
                comp = cfg["compression"] or "keine"
                if cfg["compression"] and cfg["compression"].startswith("LERC"):
                    comp += f" (max. Fehler {cfg['lerc_error']:g} m" + \
                            (", verlustfrei)" if cfg["lerc_error"] == 0 else ")")
                ctx.log(f"Kompression:  {comp} | Blockgröße {cfg['blocksize']} px | "
                        f"Overviews: {'ja (' + cfg['ovr_resampling'] + ')' if cfg['overviews'] else 'nein'}")
                if cfg["temp_folder"]:
                    ctx.log(f"Temp-Ordner:  {cfg['temp_folder']}")
            ctx.log(f"Threads:      {cfg['threads']} | RAM-Budget: {fmt_bytes(cfg['ram_bytes'])}")

        # Dateien suchen
        ctx.log(f"\nSuche nach TIFF-Dateien in: {cfg['input_folder']}")
        ctx.progress(None, "Dateien suchen", "")
        files = find_tiff_files(cfg["input_folder"], cfg["recursive"])
        if not analyse_only and cfg["output_format"] != "VRT":
            # eigene Ausgaben (z.B. bei Ausgabe in den Input-Ordner) ausschließen
            out_abs = os.path.normcase(os.path.abspath(cfg["output_file"]))
            out_tiles = os.path.normcase(os.path.join(os.path.dirname(out_abs),
                                                      Path(out_abs).stem + "_kacheln"))
            files = [f for f in files if os.path.normcase(os.path.abspath(f)) != out_abs
                     and not os.path.normcase(os.path.abspath(f)).startswith(out_tiles)
                     and "__tmp_mosaik" not in f]
        ctx.log(f"✓ {len(files)} TIFF-Dateien gefunden")
        if not files:
            ctx.log("✗ Keine TIFF-Dateien gefunden!")
            return False
        ctx.check_stop()

        # Metadaten
        tiles, errors = collect_metadata(files, max(cfg["threads"], 4), ctx)
        if not tiles:
            ctx.log("✗ Keine lesbaren TIFF-Dateien zum Mergen!")
            return False
        ctx.check_stop()

        # Analyse
        plan = build_plan(tiles, cfg, ctx)
        if analyse_only:
            if cfg["output_file"]:
                try:
                    d = os.path.dirname(os.path.abspath(cfg["output_file"]))
                    if os.path.isdir(d):
                        ctx.log(f"Freier Speicher Zielordner: {fmt_bytes(shutil.disk_usage(d).free)}")
                except Exception:
                    pass
            ctx.log("\n✓ Analyse abgeschlossen (es wurden keine Dateien geschrieben).")
            return True

        # Freien Speicher prüfen
        for label, d in (("Zielordner", os.path.dirname(os.path.abspath(cfg["output_file"]))),
                         ("Temp-Ordner", cfg["temp_folder"])):
            if not d:
                continue
            try:
                os.makedirs(d, exist_ok=True)
                ctx.log(f"Freier Speicher {label}: {fmt_bytes(shutil.disk_usage(d).free)}")
            except Exception as e:
                ctx.log(f"⚠ {label} nicht prüfbar: {e}")

        # Log-Datei neben der Ausgabe
        log_path = str(Path(cfg["output_file"]).with_suffix("")) + "_log.txt"
        ctx.open_logfile(log_path)

        # Verarbeitung (GDAL-Cache bewusst begrenzt)
        cache_mb = int(max(256, min(2048, cfg["ram_bytes"] / 1024 ** 2 / 4)))
        with rasterio.Env(GDAL_CACHEMAX=cache_mb, GDAL_NUM_THREADS=str(cfg["threads"])):
            if cfg["output_format"] == "VRT":
                outputs = run_vrt(plan, cfg, ctx)
            elif cfg["layout"] == "tiles":
                outputs = run_tiles(plan, cfg, ctx)
            else:
                outputs = run_single(plan, cfg, ctx)

        # Abschluss
        t_end = datetime.now()
        ctx.log("\n" + "=" * 70)
        ctx.log("  VERARBEITUNG ABGESCHLOSSEN")
        ctx.log("=" * 70)
        if outputs:
            total = sum(os.path.getsize(p) for p in outputs if os.path.exists(p))
            ctx.log(f"Ausgabedateien: {len(outputs)} ({fmt_bytes(total)})")
            if len(outputs) == 1 and cfg["output_format"] != "VRT":
                with rasterio.open(outputs[0]) as r:
                    ctx.log(f"Datei:        {outputs[0]}")
                    ctx.log(f"Größe:        {r.width} x {r.height} Pixel, {r.count} Band/Bänder")
                    ctx.log(f"Blockgröße:   {r.block_shapes[0]} | Kompression: {r.compression}")
                    ctx.log(f"Overviews:    {r.overviews(1) or 'keine'}")
                    ctx.log(f"Bounds:       {r.bounds}")
        else:
            ctx.log("⚠ Es wurden keine Ausgabedateien erzeugt (nur NoData?).")
        if ctx.failed_reads:
            ctx.warn(f"{len(set(ctx.failed_reads))} Datei(en) konnten nicht vollständig gelesen werden:")
            for p in sorted(set(ctx.failed_reads))[:20]:
                ctx.log(f"    - {p}")
        if ctx.warnings:
            ctx.log(f"Warnungen insgesamt: {len(ctx.warnings)} (Details siehe oben)")
        ctx.log(f"Log-Datei:    {log_path}")
        ctx.log(f"Ende:         {t_end.strftime('%Y-%m-%d %H:%M:%S')}")
        ctx.log(f"Gesamtdauer:  {str(t_end - t_start).split('.')[0]}")
        ctx.progress(1.0, "Fertig", "")
        return True
    finally:
        rio_logger.removeHandler(gdal_handler)
        ctx.close_logfile()


# ============================================================================
# GUI - HILFSKLASSEN
# ============================================================================

class ToolTip:
    """Einfacher Tooltip für tkinter-Widgets."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify=tk.LEFT, background="#ffffe0",
                 relief=tk.SOLID, borderwidth=1, wraplength=440,
                 font=("Segoe UI", 9)).pack(ipadx=4, ipady=2)

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ============================================================================
# HAUPTANWENDUNG
# ============================================================================

class TerrainMergerApp:

    def __init__(self, root):
        self.root = root
        self.root.title(f"Terrain TIFF Raster Merger {APP_VERSION}")

        # Programm-Icon für Titelleiste (oben links) und Windows-Taskleiste
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "FabianBritze.TerrainTIFFRasterMerger.2.0"
                )
            except Exception:
                pass
        _icon_path = resource_path("Programm_ICON_Merge.ico")
        try:
            self.root.iconbitmap(default=_icon_path)
            self.root.iconbitmap(_icon_path)
        except Exception:
            pass  # Fallback falls Icon fehlt

        self.root.geometry("1000x900")
        self.root.minsize(900, 760)

        self.stop_event = threading.Event()
        self.proc_thread = None
        self.ui_queue = queue.Queue()
        self._stage = None
        self._stage_t0 = 0.0
        self._stage_f0 = 0.0
        self._run_t0 = 0.0

        self._build_menu()
        self._build_statusbar()  # Zuerst Statusleiste bauen
        self._build_main_ui()  # Dann Hauptinhalt

        self._load_settings_auto()
        self._update_states()
        self._poll_queue()

    # ------------------------------------------------------------------
    # MENÜ
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Einstellungen speichern …",
                           command=self._save_settings_dialog,
                           accelerator="Ctrl+S")
        m_file.add_command(label="Einstellungen laden …",
                           command=self._load_settings_dialog)
        m_file.add_command(label="Standardeinstellungen", command=self._reset_settings)
        m_file.add_separator()
        m_file.add_command(label="Beenden", command=self._on_close)
        menubar.add_cascade(label="Datei", menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Anleitung", command=self._show_anleitung)
        m_help.add_command(label="ArcGIS-Webmap: Empfehlungen", command=self._show_arcgis)
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

    def _combo(self, parent, var, values, width, row, col, tip=None):
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
        cb.grid(row=row, column=col, sticky=tk.W, padx=6, pady=3)
        if tip:
            ToolTip(cb, tip)
        return cb

    def _build_main_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))

        # Variablen
        self.v_input = tk.StringVar()
        self.v_recursive = tk.BooleanVar(value=True)
        self.v_output = tk.StringVar()
        self.v_temp = tk.StringVar()
        self.v_format = tk.StringVar(value=FORMATS["COG"])
        self.v_layout = tk.StringVar(value=LAYOUTS["single"])
        self.v_tile_km = tk.StringVar(value="5")
        self.v_compression = tk.StringVar(value="DEFLATE")
        self.v_lerc = tk.StringVar(value="0.01")
        self.v_blocksize = tk.StringVar(value="512")
        self.v_overviews = tk.BooleanVar(value=True)
        self.v_ovr_res = tk.StringVar(value="average")
        self.v_target_res = tk.StringVar(value="Original")
        self.v_resampling = tk.StringVar(value="nearest")
        self.v_overlap = tk.StringVar(value=OVERLAP_MODES["first"])
        self.v_nodata = tk.StringVar(value="Auto")
        self.v_threads = tk.StringVar(value="Auto")
        self.v_ram = tk.StringVar(value="Auto")

        # --- Ein-/Ausgabe ---
        f_io = ttk.LabelFrame(main_frame, text="Ein- und Ausgabe", padding=10)
        f_io.pack(fill=tk.X, pady=(0, 6))
        f_io.columnconfigure(1, weight=1)

        ttk.Label(f_io, text="TIFF-Ordner *:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(f_io, textvariable=self.v_input).grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        ttk.Button(f_io, text="📁", width=3,
                   command=lambda: self.v_input.set(
                       filedialog.askdirectory(title="Input-Ordner wählen") or self.v_input.get()
                   )).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))
        cb = ttk.Checkbutton(f_io, text="Unterordner einbeziehen", variable=self.v_recursive)
        cb.grid(row=1, column=1, sticky=tk.W, padx=6)

        ttk.Label(f_io, text="Ziel-Datei *:").grid(row=2, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Entry(f_io, textvariable=self.v_output).grid(row=2, column=1, sticky=tk.EW, padx=6, pady=4)
        ttk.Button(f_io, text="📁", width=3, command=self._choose_output).grid(
            row=2, column=2, sticky=tk.W, padx=(0, 6))
        self.lbl_out_hint = ttk.Label(f_io, text="", foreground="gray")
        self.lbl_out_hint.grid(row=3, column=1, columnspan=2, sticky=tk.W, padx=6)

        ttk.Label(f_io, text="Temp-Ordner (lokal):").grid(row=4, column=0, sticky=tk.W, padx=6, pady=4)
        e_tmp = ttk.Entry(f_io, textvariable=self.v_temp)
        e_tmp.grid(row=4, column=1, sticky=tk.EW, padx=6, pady=4)
        ttk.Button(f_io, text="📁", width=3,
                   command=lambda: self.v_temp.set(
                       filedialog.askdirectory(title="Lokalen Temp-Ordner wählen") or self.v_temp.get()
                   )).grid(row=4, column=2, sticky=tk.W, padx=(0, 6))
        ttk.Label(f_io, text="Optional, dringend empfohlen bei Netzlaufwerk-Ziel: Zwischenschritte "
                             "werden lokal (SSD) gerechnet, nur das Endergebnis wird kopiert.",
                  foreground="gray").grid(row=5, column=1, columnspan=2, sticky=tk.W, padx=6)
        ToolTip(e_tmp, "Leer lassen = Zwischendateien direkt im Zielordner.\n"
                       "Benötigter Platz: ca. 2x Größe des Ergebnisses.")

        # --- Optionen in zwei Spalten ---
        f_opts = ttk.Frame(main_frame)
        f_opts.pack(fill=tk.X, pady=4)
        f_opts.columnconfigure(0, weight=1, uniform="o")
        f_opts.columnconfigure(1, weight=1, uniform="o")

        # linke Spalte: Format & Kachelung
        f_fmt = ttk.LabelFrame(f_opts, text="Ausgabeformat & Kachelung", padding=10)
        f_fmt.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))

        ttk.Label(f_fmt, text="Format:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=3)
        self._combo(f_fmt, self.v_format, list(FORMATS.values()), 36, 0, 1,
                    "COG: ein GeoTIFF mit interner Kachelung und Overviews in optimaler "
                    "Reihenfolge – ArcGIS lädt nur die sichtbaren Kacheln.\n"
                    "GeoTIFF: wie COG, Overviews liegen aber am Dateiende (etwas schneller erstellt).\n"
                    "VRT: nur eine kleine XML-Datei, die auf die Originalkacheln verweist (sofort fertig).")
        ttk.Label(f_fmt, text="Struktur:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=3)
        self.cb_layout = self._combo(
            f_fmt, self.v_layout, list(LAYOUTS.values()), 36, 1, 1,
            "Eine Gesamtdatei: ein großes BigTIFF/COG.\n"
            "Kachel-Mosaik: Ergebnis wird in gleich große Dateien (z.B. 5x5 km) aufgeteilt, "
            "zusätzlich entsteht ein VRT über alle Dateien. Gut für Upload/Mosaik-Datasets.")
        ttk.Label(f_fmt, text="Kachelgröße (km):").grid(row=2, column=0, sticky=tk.W, padx=6, pady=3)
        self.cb_tile_km = ttk.Combobox(f_fmt, textvariable=self.v_tile_km, values=TILE_SIZES_KM, width=8)
        self.cb_tile_km.grid(row=2, column=1, sticky=tk.W, padx=6, pady=3)
        ttk.Label(f_fmt, text="Interne Blockgröße:").grid(row=3, column=0, sticky=tk.W, padx=6, pady=3)
        self.cb_block = self._combo(f_fmt, self.v_blocksize, BLOCKSIZES, 8, 3, 1,
                                    "Größe der internen Kacheln in Pixeln. 512 ist Standard für "
                                    "COG/ArcGIS und Webdienste.")
        self.chk_ovr = ttk.Checkbutton(f_fmt, text="Overviews (Pyramiden) erstellen",
                                       variable=self.v_overviews, command=self._update_states)
        self.chk_ovr.grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=6, pady=3)
        ToolTip(self.chk_ovr, "Verkleinerte Vorschaustufen – entscheidend für schnelle Ladezeiten "
                              "in ArcGIS beim Herauszoomen.")
        ttk.Label(f_fmt, text="Overview-Resampling:").grid(row=5, column=0, sticky=tk.W, padx=6, pady=3)
        self.cb_ovr_res = self._combo(f_fmt, self.v_ovr_res, OVR_RESAMPLINGS, 12, 5, 1,
                                      "average: Mittelwert (empfohlen für Höhendaten)\n"
                                      "nearest: schnellste Variante, keine Mischwerte")

        # rechte Spalte: Kompression & Daten
        f_data = ttk.LabelFrame(f_opts, text="Kompression & Daten", padding=10)
        f_data.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))

        ttk.Label(f_data, text="Kompression:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=3)
        self.cb_comp = self._combo(
            f_data, self.v_compression, COMPRESSIONS, 16, 0, 1,
            "DEFLATE: verlustfrei, überall lesbar (mit Float-Predictor).\n"
            "LERC_*: Esri-Format für Höhendaten, sehr klein und schnell; mit Toleranz "
            "(z.B. 0.01 m) verlustbehaftet, mit 0 verlustfrei.\n"
            "ZSTD: verlustfrei, schnell – erfordert aktuelle Software.\n"
            "LZW: verlustfrei, sehr kompatibel, größer.")
        ttk.Label(f_data, text="LERC-Toleranz (m):").grid(row=1, column=0, sticky=tk.W, padx=6, pady=3)
        self.e_lerc = ttk.Entry(f_data, textvariable=self.v_lerc, width=10)
        self.e_lerc.grid(row=1, column=1, sticky=tk.W, padx=6, pady=3)
        ToolTip(self.e_lerc, "Maximal erlaubter Höhenfehler pro Pixel in Metern.\n"
                             "0 = verlustfrei, 0.01 = 1 cm (empfohlen für Webdienste).")
        ttk.Label(f_data, text="Zielauflösung (m):").grid(row=2, column=0, sticky=tk.W, padx=6, pady=3)
        cb_res = ttk.Combobox(f_data, textvariable=self.v_target_res, values=TARGET_RES, width=10)
        cb_res.grid(row=2, column=1, sticky=tk.W, padx=6, pady=3)
        cb_res.bind("<<ComboboxSelected>>", lambda e: self._update_states())
        cb_res.bind("<KeyRelease>", lambda e: self._update_states())
        ToolTip(cb_res, "'Original' übernimmt die Auflösung der Kacheln (z.B. 0.2 m).\n"
                        "Ein größerer Wert erzeugt eine verkleinerte Fassung (z.B. 1 m = 25x weniger Pixel).")
        ttk.Label(f_data, text="Resampling:").grid(row=3, column=0, sticky=tk.W, padx=6, pady=3)
        self.cb_resampling = self._combo(f_data, self.v_resampling, RESAMPLINGS, 12, 3, 1,
                                         "Nur relevant bei geänderter Zielauflösung oder nicht "
                                         "pixelgenau ausgerichteten Kacheln.\n"
                                         "nearest: exakte Werte; average: für Verkleinerung empfohlen.")
        ttk.Label(f_data, text="Überlappungen:").grid(row=4, column=0, sticky=tk.W, padx=6, pady=3)
        self._combo(f_data, self.v_overlap, list(OVERLAP_MODES.values()), 30, 4, 1,
                    "Regel für Bereiche, in denen sich Kacheln überlappen.\n"
                    "NoData-Pixel überschreiben dabei NIE gültige Werte.")
        ttk.Label(f_data, text="NoData-Wert:").grid(row=5, column=0, sticky=tk.W, padx=6, pady=3)
        e_nd = ttk.Entry(f_data, textvariable=self.v_nodata, width=10)
        e_nd.grid(row=5, column=1, sticky=tk.W, padx=6, pady=3)
        ToolTip(e_nd, "'Auto' übernimmt den NoData-Wert der Eingangsdaten (z.B. -9999).")

        # --- Leistung ---
        f_perf = ttk.LabelFrame(main_frame, text="Leistung", padding=8)
        f_perf.pack(fill=tk.X, pady=4)
        total_ram, avail_ram = memory_info()
        ttk.Label(f_perf, text="CPU-Threads:").grid(row=0, column=0, sticky=tk.W, padx=6)
        self._combo(f_perf, self.v_threads, ["Auto"] + [str(i) for i in range(1, multiprocessing.cpu_count() + 1)],
                    8, 0, 1, "Threads für paralleles Lesen und Komprimieren.")
        ttk.Label(f_perf, text=f"(Max: {multiprocessing.cpu_count()} Kerne)",
                  foreground="gray").grid(row=0, column=2, sticky=tk.W, padx=(0, 20))
        ttk.Label(f_perf, text="RAM-Budget (GB):").grid(row=0, column=3, sticky=tk.W, padx=6)
        self._combo(f_perf, self.v_ram, RAM_CHOICES, 8, 0, 4,
                    "Wie viel Arbeitsspeicher für Pufferdaten genutzt werden darf.\n"
                    "Auto = 25 % des RAM (max. 8 GB). Mehr RAM = weniger, größere Lesevorgänge.")
        ttk.Label(f_perf, text=f"(Installiert: {fmt_bytes(total_ram)}, frei: {fmt_bytes(avail_ram)})",
                  foreground="gray").grid(row=0, column=5, sticky=tk.W)

        # --- Ausgabe ---
        f_output_log = ttk.LabelFrame(main_frame, text="Status & Log", padding=8)
        f_output_log.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        prog_frame = ttk.Frame(f_output_log)
        prog_frame.pack(fill=tk.X, pady=(0, 6))
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=1000)
        self.progress.pack(fill=tk.X, side=tk.TOP)
        self.lbl_progress = ttk.Label(prog_frame, text="", foreground="#333")
        self.lbl_progress.pack(fill=tk.X, side=tk.TOP, pady=(3, 0))

        self.output_text = scrolledtext.ScrolledText(
            f_output_log, state='disabled', font=('Consolas', 9),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='white',
            wrap=tk.WORD, height=12
        )
        self.output_text.pack(fill=tk.BOTH, expand=True)

        btn_clear = ttk.Button(f_output_log, text="Ausgabe leeren",
                               command=self._clear_output)
        btn_clear.pack(side=tk.RIGHT, pady=(5, 0))

        for v in (self.v_format, self.v_layout, self.v_compression, self.v_output):
            v.trace_add("write", lambda *a: self._update_states())

    def _choose_output(self):
        fmt = self._key(FORMATS, self.v_format.get())
        if fmt == "VRT":
            ext, types = ".vrt", [("VRT Dateien", "*.vrt"), ("Alle Dateien", "*.*")]
        else:
            ext, types = ".tif", [("TIFF Dateien", "*.tif *.tiff"), ("Alle Dateien", "*.*")]
        p = filedialog.asksaveasfilename(title="Output-Datei speichern", defaultextension=ext,
                                         filetypes=types)
        if p:
            self.v_output.set(p)

    def _clear_output(self):
        """Leert das Ausgabe-Log"""
        self.output_text.configure(state='normal')
        self.output_text.delete('1.0', tk.END)
        self.output_text.configure(state='disabled')

    @staticmethod
    def _key(mapping, label):
        for k, v in mapping.items():
            if v == label or k == label:
                return k
        return next(iter(mapping))

    def _update_states(self):
        """Aktiviert/deaktiviert abhängige Bedienelemente."""
        if not hasattr(self, "cb_layout"):
            return
        fmt = self._key(FORMATS, self.v_format.get())
        layout = self._key(LAYOUTS, self.v_layout.get())
        comp = self.v_compression.get()
        is_vrt = fmt == "VRT"
        self.cb_layout.configure(state="disabled" if is_vrt else "readonly")
        self.cb_tile_km.configure(state="normal" if (layout == "tiles" and not is_vrt) else "disabled")
        self.cb_comp.configure(state="readonly")
        self.e_lerc.configure(state="normal" if comp.startswith("LERC") else "disabled")
        self.cb_ovr_res.configure(state="readonly" if self.v_overviews.get() else "disabled")
        res_orig = self.v_target_res.get().strip().lower() in ("", "original")
        self.cb_resampling.configure(state="readonly")

        out = self.v_output.get().strip()
        stem = Path(out).stem if out else "<name>"
        if is_vrt:
            hint = "Es entsteht nur eine .vrt-Datei (Verweis auf die Originalkacheln). " \
                   "Kompression gilt dann nur für die Overviews (.ovr)."
        elif layout == "tiles":
            hint = f"Kacheln werden in den Unterordner '{stem}_kacheln' geschrieben, " \
                   f"dazu der Index '{stem}.vrt'."
        else:
            hint = "Zusammengeführte Datei (BigTIFF, intern gekachelt)."
        if not res_orig:
            hint += "  Achtung: Zielauflösung ≠ Original."
        self.lbl_out_hint.configure(text=hint)

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
                                    command=lambda: self._start(False))
        self.btn_start.pack(side=tk.RIGHT, padx=4)

        self.btn_analyse = ttk.Button(btn_frame, text="🔍  Daten analysieren",
                                      command=lambda: self._start(True))
        self.btn_analyse.pack(side=tk.RIGHT, padx=4)
        ToolTip(self.btn_analyse, "Liest nur die Metadaten und zeigt Ausdehnung, Größe, "
                                  "Überlappungen und Auffälligkeiten – ohne etwas zu schreiben.")

        self.lbl_status = ttk.Label(btn_frame, text="Bereit")
        self.lbl_status.pack(side=tk.LEFT, padx=(2, 0))

    # ------------------------------------------------------------------
    # EINSTELLUNGEN
    # ------------------------------------------------------------------

    def _collect_settings(self):
        """Sammelt alle Einstellungen (String-Form für JSON)"""
        return {
            "input_folder": self.v_input.get().strip(),
            "recursive": bool(self.v_recursive.get()),
            "output_file": self.v_output.get().strip(),
            "temp_folder": self.v_temp.get().strip(),
            "format_str": self._key(FORMATS, self.v_format.get()),
            "layout_str": self._key(LAYOUTS, self.v_layout.get()),
            "tile_km_str": self.v_tile_km.get().strip(),
            "compression_str": self.v_compression.get(),
            "lerc_error_str": self.v_lerc.get().strip(),
            "blocksize_str": self.v_blocksize.get(),
            "overviews": bool(self.v_overviews.get()),
            "ovr_resampling_str": self.v_ovr_res.get(),
            "target_res_str": self.v_target_res.get().strip(),
            "resampling_str": self.v_resampling.get(),
            "overlap_str": self._key(OVERLAP_MODES, self.v_overlap.get()),
            "nodata_str": self.v_nodata.get().strip(),
            "threads_str": self.v_threads.get(),
            "ram_str": self.v_ram.get(),
        }

    def _apply_settings(self, cfg):
        """Lädt Einstellungen in die GUI (kompatibel mit Version 1.0)"""
        s = dict(DEFAULT_SETTINGS)
        s.update(cfg or {})
        self.v_input.set(s["input_folder"])
        self.v_recursive.set(bool(s["recursive"]))
        self.v_output.set(s["output_file"])
        self.v_temp.set(s["temp_folder"])
        self.v_format.set(FORMATS.get(s["format_str"], FORMATS["COG"]))
        self.v_layout.set(LAYOUTS.get(s["layout_str"], LAYOUTS["single"]))
        self.v_tile_km.set(s["tile_km_str"])
        comp = s["compression_str"] if s["compression_str"] in COMPRESSIONS else "DEFLATE"
        self.v_compression.set(comp)
        self.v_lerc.set(s["lerc_error_str"])
        self.v_blocksize.set(s["blocksize_str"] if s["blocksize_str"] in BLOCKSIZES else "512")
        self.v_overviews.set(bool(s["overviews"]))
        self.v_ovr_res.set(s["ovr_resampling_str"] if s["ovr_resampling_str"] in OVR_RESAMPLINGS else "average")
        self.v_target_res.set(s["target_res_str"])
        self.v_resampling.set(s["resampling_str"] if s["resampling_str"] in RESAMPLINGS else "nearest")
        self.v_overlap.set(OVERLAP_MODES.get(s["overlap_str"], OVERLAP_MODES["first"]))
        self.v_nodata.set(s["nodata_str"])
        self.v_threads.set(str(s["threads_str"]))
        self.v_ram.set(str(s["ram_str"]))
        self._update_states()

    def _reset_settings(self):
        keep = {"input_folder": self.v_input.get(), "output_file": self.v_output.get(),
                "temp_folder": self.v_temp.get()}
        s = dict(DEFAULT_SETTINGS)
        s.update(keep)
        self._apply_settings(s)

    def _save_settings_auto(self):
        """Speichert Einstellungen automatisch"""
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._collect_settings(), f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_settings_auto(self):
        """Lädt Einstellungen automatisch"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    self._apply_settings(json.load(f))
            except Exception:
                pass

    def _save_settings_dialog(self):
        """Speichert Einstellungen mit Dialog"""
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
                json.dump(self._collect_settings(), f, indent=2, ensure_ascii=False)
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
                self._apply_settings(json.load(f))
        except Exception as e:
            messagebox.showerror("Fehler", f"Laden fehlgeschlagen:\n{e}")

    # ------------------------------------------------------------------
    # VERARBEITUNG
    # ------------------------------------------------------------------

    def _validate(self, cfg, analyse_only):
        """Validiert die Eingaben"""
        if not cfg['input_folder']:
            messagebox.showerror("Fehler", "Bitte Input-Ordner angeben!")
            return False
        if not os.path.isdir(cfg['input_folder']):
            messagebox.showerror("Fehler", "Input-Ordner existiert nicht!")
            return False
        if analyse_only:
            return True
        if not cfg['output_file']:
            messagebox.showerror("Fehler", "Bitte Output-Datei angeben!")
            return False
        if cfg["temp_folder"] and not os.path.isdir(cfg["temp_folder"]):
            try:
                os.makedirs(cfg["temp_folder"], exist_ok=True)
            except Exception as e:
                messagebox.showerror("Fehler", f"Temp-Ordner kann nicht angelegt werden:\n{e}")
                return False
        out = cfg["output_file"]
        if cfg["output_format"] != "VRT" and cfg["layout"] == "tiles":
            tdir = os.path.join(os.path.dirname(os.path.abspath(out)), Path(out).stem + "_kacheln")
            if os.path.isdir(tdir) and os.listdir(tdir):
                if not messagebox.askyesno("Überschreiben?",
                                           f"Der Kachel-Ordner existiert bereits:\n{tdir}\n\n"
                                           "Gleichnamige Kacheln werden überschrieben. Fortfahren?"):
                    return False
        elif os.path.exists(out):
            if not check_target_writable(out):
                messagebox.showerror(
                    "Datei gesperrt (Zugriff verweigert)",
                    f"Die Zieldatei existiert bereits und ist aktuell durch ein anderes Programm "
                    f"geöffnet/gesperrt (z. B. ArcGIS Pro, QGIS oder Windows Explorer):\n\n{out}\n\n"
                    f"Bitte schließen Sie die Datei im anderen Programm oder wählen Sie einen "
                    f"anderen Ziel-Dateinamen."
                )
                return False
            if not messagebox.askyesno("Überschreiben?",
                                       f"Die Datei existiert bereits:\n{out}\n\nÜberschreiben?"):
                return False
        if cfg["output_format"] == "VRT" and cfg["temp_folder"]:
            pass  # Temp-Ordner wird für VRT nicht benötigt
        return True

    def _start(self, analyse_only=False):
        """Startet die Verarbeitung"""
        if self.proc_thread and self.proc_thread.is_alive():
            messagebox.showwarning("Hinweis", "Verarbeitung läuft bereits!")
            return

        if not DEPS_OK:
            messagebox.showerror("Fehlende Bibliotheken",
                                 f"Folgende Pakete konnten nicht geladen werden:\n\n{DEPS_ERROR}\n\n"
                                 "Bitte per 'pip install rasterio numpy' nachinstallieren.")
            return

        try:
            cfg = parse_settings(self._collect_settings())
        except ValueError as e:
            messagebox.showerror("Ungültige Einstellung", str(e))
            return
        if not self._validate(cfg, analyse_only):
            return
        if cfg["output_file"] and cfg["output_file"] != self.v_output.get().strip():
            self.v_output.set(cfg["output_file"])  # Dateiendung wurde angepasst

        self._save_settings_auto()
        self.stop_event.clear()
        self._stage = None
        self._run_t0 = time.time()

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_analyse.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.lbl_status.configure(text="⏳  Analyse läuft …" if analyse_only else "⏳  Verarbeitung läuft …")
        self.progress.configure(mode="determinate", value=0)

        self.proc_thread = threading.Thread(
            target=self._thread_worker, args=(cfg, analyse_only), daemon=True)
        self.proc_thread.start()

    def _thread_worker(self, cfg, analyse_only):
        """Worker-Thread für die Verarbeitung"""
        ctx = RunContext(
            log_fn=lambda s: self.ui_queue.put(("log", s + "\n")),
            progress_fn=lambda f, st, d: self.ui_queue.put(("progress", f, st, d)),
            stop_event=self.stop_event,
        )
        result = "error"
        try:
            ok = run_processing(cfg, ctx, analyse_only)
            result = "ok" if ok else "error"
        except ProcessingCancelled:
            ctx.log("\n⚠ Verarbeitung abgebrochen. Unvollständige Dateien wurden gelöscht.")
            result = "cancel"
        except MemoryError:
            ctx.log("\n✗ Zu wenig Arbeitsspeicher! Bitte das RAM-Budget verringern.")
        except Exception as e:
            ctx.log(f"\n✗ Unerwarteter Fehler: {e}")
            ctx.log(traceback.format_exc())
        finally:
            self.ui_queue.put(("done", result))

    def _poll_queue(self):
        """Überträgt Meldungen des Worker-Threads in die GUI (thread-sicher)."""
        texts = []
        try:
            for _ in range(2000):
                msg = self.ui_queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    texts.append(msg[1])
                elif kind == "progress":
                    self._on_progress(*msg[1:])
                elif kind == "done":
                    if texts:
                        self._append_text("".join(texts))
                        texts = []
                    self._on_done(msg[1])
        except queue.Empty:
            pass
        if texts:
            self._append_text("".join(texts))
        self.root.after(100, self._poll_queue)

    def _append_text(self, text):
        self.output_text.configure(state='normal')
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)
        self.output_text.configure(state='disabled')

    def _on_progress(self, frac, stage, detail):
        now = time.time()
        if stage != self._stage:
            self._stage = stage
            self._stage_t0 = now
            self._stage_f0 = frac or 0.0
        if frac is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
            text = f"{stage}  {detail}"
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=int(frac * 1000))
            text = f"{stage}  –  {frac * 100:.1f} %  {detail}"
            df = frac - self._stage_f0
            if df > 0.005 and frac < 1.0:
                eta = (now - self._stage_t0) / df * (1.0 - frac)
                text += f"   |   Restzeit ca. {fmt_duration(eta)}"
        text += f"   |   gesamt {fmt_duration(now - self._run_t0)}"
        self.lbl_progress.configure(text=text)

    def _on_done(self, result):
        """Wird nach Abschluss der Verarbeitung aufgerufen"""
        self.progress.stop()
        self.progress.configure(mode="determinate", value=1000 if result == "ok" else 0)
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_analyse.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        status = {"ok": "✓  Fertig", "cancel": "⚠  Abgebrochen", "error": "✗  Fehler – siehe Log"}
        self.lbl_status.configure(text=status.get(result, "Bereit"))

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
            "1. EIN- UND AUSGABE\n"
            "   • Ordner mit TIFF-Kacheln wählen (optional inkl. Unterordner)\n"
            "   • Ziel-Datei angeben (.tif bzw. .vrt)\n"
            "   • Temp-Ordner: lokaler, schneller Ordner (SSD). Sehr empfohlen,\n"
            "     wenn das Ziel ein Netzlaufwerk ist.\n"
            "\n"
            "2. DATEN ANALYSIEREN (empfohlen vor dem Start)\n"
            "   • Zeigt Ausdehnung, Pixelanzahl, erwartete Größe,\n"
            "     Überlappungen und isolierte Kacheln – ohne etwas zu schreiben.\n"
            "\n"
            "3. AUSGABEFORMAT\n"
            "   • COG: ein Cloud Optimized GeoTIFF (empfohlen für ArcGIS)\n"
            "   • GeoTIFF: gekacheltes BigTIFF mit internen Overviews\n"
            "   • VRT: virtuelles Mosaik, verweist auf die Originaldateien\n"
            "   • Kachel-Mosaik: Ergebnis aufgeteilt in z.B. 5x5 km Dateien + VRT\n"
            "\n"
            "4. KOMPRESSION\n"
            "   • DEFLATE: verlustfrei, maximal kompatibel (Standard)\n"
            "   • LERC_DEFLATE: kleinste Dateien, Toleranz z.B. 0.01 m\n"
            "\n"
            "5. VERARBEITUNG\n"
            "   • Die Daten werden streifenweise verarbeitet – der RAM-Bedarf\n"
            "     ist unabhängig von der Gesamtgröße (RAM-Budget einstellbar).\n"
            "   • 'Stopp' bricht ab; unvollständige Dateien werden gelöscht.\n"
            "   • Eine Log-Datei wird neben der Ausgabe gespeichert.\n"
            "\n"
            "6. EINSTELLUNGEN\n"
            "   • Werden automatisch beim Start und Schließen gespeichert\n"
            "   • Manuelles Speichern/Laden über Datei-Menü möglich\n"
        )

    def _show_arcgis(self):
        messagebox.showinfo(
            "ArcGIS-Webmap: Empfehlungen",
            "Schnelle Ladezeiten in ArcGIS – empfohlene Einstellungen:\n"
            "\n"
            "• Format: COG, Blockgröße 512, Overviews 'average'\n"
            "  → ArcGIS liest nur die gerade sichtbaren 512er-Kacheln der\n"
            "     passenden Zoomstufe statt der ganzen Datei.\n"
            "• Kompression: LERC_DEFLATE mit 0.01 m (Esri-Format für\n"
            "  Höhendaten) oder DEFLATE (verlustfrei).\n"
            "\n"
            "Veröffentlichung:\n"
            "• ArcGIS Enterprise: COG (oder Kachel-Mosaik) in ein Mosaik-\n"
            "  Dataset laden und als Image Service veröffentlichen. Darstellung\n"
            "  per Rasterfunktion (z.B. Schummerung/Farbverlauf).\n"
            "• ArcGIS Online: 'Gekachelten Bild-Layer erstellen' (Tiled Imagery\n"
            "  Layer, Typ Elevation) – mehrere Dateien des Kachel-Mosaiks\n"
            "  können gemeinsam hochgeladen werden.\n"
            "• 3D-Webszene: Höhen-Layer per 'Elevation Tile Package\n"
            "  erstellen' (LERC) in ArcGIS Pro.\n"
            "• Reine 2D-Webkarte: Höhenwerte sind kein Bild – für maximale\n"
            "  Geschwindigkeit eine Schummerung/Farbdarstellung als\n"
            "  Kachel-Layer (Tile Layer) im Kachelschema der Basemap\n"
            "  (WGS84 Web Mercator) cachen.\n"
            "\n"
            "Tipp: Für Übersichtsmaßstäbe zusätzlich eine 1-m-Fassung\n"
            "(Zielauflösung 1.0) erzeugen – 25x weniger Pixel."
        )

    def _show_info(self):
        messagebox.showinfo(
            "Info",
            "Terrain TIFF Raster Merger\n"
            "\n"
            "Dieses Tool führt georeferenzierte TIFF-Raster zu einem\n"
            "großen zusammenhängenden Raster zusammen.\n"
            "\n"
            "Entwickelt für:\n"
            "• DGM-Kacheln (Digitale Geländemodelle)\n"
            "• DOM-Kacheln (Digitale Oberflächenmodelle)\n"
            "• Beliebige georeferenzierte TIFF-Raster\n"
            "\n"
            "Ausgabe:\n"
            "• Cloud Optimized GeoTIFF, BigTIFF, Kachel-Mosaik oder VRT\n"
            "• Interne Kachelung + Overviews für schnelle Darstellung\n"
            "• Kompression: DEFLATE, LERC, ZSTD, LZW\n"
            "• Statistiken (Min/Max/Mittel/Std) werden mitgeschrieben\n"
            "\n"
            "Funktionen:\n"
            "• Speicherschonende Streifenverarbeitung (beliebig große Mosaike)\n"
            "• Leere Bereiche werden nicht gespeichert (SPARSE_OK)\n"
            "• Automatische CRS-, Auflösungs- und Überlappungsprüfung\n"
            "• Erkennung isolierter Kacheln (falsche Koordinaten)\n"
            "• Paralleles Lesen und Komprimieren\n"
            "• Wiederholung bei Lesefehlern (Netzlaufwerke)\n"
            "\n"
            "Technische Details:\n"
            "• Verwendet rasterio/GDAL für Raster-Operationen\n"
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
            f"Version {APP_VERSION}\n"
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
