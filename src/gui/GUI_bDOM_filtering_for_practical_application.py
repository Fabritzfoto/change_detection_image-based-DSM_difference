#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LICENSE:
The program calculates the difference between two image-based digital surface models and 
filters out the resulting errors. The script is divided into a building filter and a 
forest filter, which filter different elevation ranges with different parameters.
Copyright (©) 2026  Fabian Britze
E-Mail: fabian.britze@gmail.com

<https://doi.org/10.5281/zenodo.19392546>

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License version 3 as published by
the Free Software Foundation. 
>> see <https://www.gnu.org/licenses/> for more Information.


Deutsch:
LIZENZ:
Das Programm berechnet die Differenz aus 2 bildbasierten digitalen Oberflächenmodellen und 
filtert die dabei auftauchenden Fehler. Das Skript teilt sich dabei in eine Gebäude und 
eine Wald Filterung, die unterschiedliche Höhenbereiche mit verschiedenen Parametern filtern. 
Copyright (C) 2026  Fabian Britze
E-Mail: fabian.britze@gmail.com

<https://doi.org/10.5281/zenodo.19392546>

Dieses Programm wird in der Hoffnung verbreitet, dass es nützlich sein wird,
aber OHNE JEGLICHE GEWÄHRLEISTUNG.

Dieses Programm ist freie Software: Sie können es weitergeben und/oder verändern
unter den Bedingungen der GNU General Public License Version 3, wie sie von der 
Free Software Foundation veröffentlicht wurde.
>> Weitere Informationen finden Sie unter <https://www.gnu.org/licenses/>.

------------------------------------------------------------------------------------------------------

Terrain-Differenz-Berechnung mit tDOM Filterung- GUI Version
Fenster-Anwendung zur Konfiguration und Ausführung der Höhendifferenz-Berechnung
"""

import sys
import os
import re
import glob
import queue
import threading
import datetime
import json
import atexit
from contextlib import contextmanager
from multiprocessing import cpu_count, Pool, freeze_support

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# Abhängigkeiten prüfen
try:
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
    from rasterio.windows import from_bounds
    from scipy import ndimage
    from scipy.ndimage import label
    DEPS_OK = True
    DEPS_ERROR = ""
except ImportError as e:
    DEPS_OK = False
    DEPS_ERROR = str(e)

# ============================================================================
# RESSOURCEN-PFAD (Wichtig für PyInstaller .exe)
# ============================================================================

def resource_path(relative_path):
    """ Ermöglicht den Zugriff auf Ressourcen (Icons etc.) sowohl im Skript als auch in der .exe """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ============================================================================
# ANWENDUNGSPFAD (funktioniert auch als .exe)
# ============================================================================

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(APP_DIR, "tDOM_Filterung_Einstellungen.json")


# ============================================================================
# STANDARD-KLASSENBEREICHE
# ============================================================================

DEFAULT_CLASS_RANGES_GEBAEUDE = [
    (-14, -12), (-13, -11), (-12, -10), (-11, -9), (-10, -8),
    (-9, -7),   (-8, -6),   (-7, -5),   (-6, -4),  (-5, -3),
    (-4, -2),   (2, 4),     (3, 5),     (4, 6),    (5, 7),
    (6, 8),     (7, 9),     (8, 10),    (9, 11),   (10, 12),
    (11, 13),   (12, 14)
]

DEFAULT_CLASS_RANGES_WALD = [
    (-30, -26), (-28, -24), (-26, -22), (-24, -20),
    (-22, -18), (-20, -16), (-18, -14)
]

# Standard-Filterwerte (für den Reset-Button)
DEFAULT_MIN_PIXELS_GEB  = 1000
DEFAULT_MIN_PIXELS_WALD = 5000
DEFAULT_CONNECTIVITY    = 8
DEFAULT_NPROC           = "auto"


# ============================================================================
# SICHERES SCHREIBEN  –  verhindert korrupte Dateien & ArcGIS-Konflikte
# ============================================================================

@contextmanager
def _safe_raster_write(output_path, **kwargs):
    """
    Schreibt zunächst in eine temporäre Neben-Datei (.writing.tmp) und
    benennt sie erst nach erfolgreichem Abschluss atomar in den Zielpfad um.

    Vorteile:
    • Ist der Zielpfad in ArcGIS geöffnet/gesperrt, schlägt erst der
      abschließende Rename fehl – nicht der gesamte Schreibvorgang.
    • Bei einem Absturz oder Abbruch bleibt keine halbfertige Ausgabedatei
      zurück, die spätere Läufe korrumpieren könnte.
    """
    dir_name = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(dir_name, exist_ok=True)
    tmp_path = output_path + ".writing.tmp"
    try:
        with rasterio.open(tmp_path, 'w', **kwargs) as dst:
            yield dst
        # Erfolg: bestehende Datei entfernen, dann umbenennen
        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(tmp_path, output_path)
    except BaseException:
        # Fehler oder Abbruch: temporäre Datei aufräumen
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


# ============================================================================
# POOL-VERWALTUNG  –  verhindert Hintergrundprozesse nach Programmende
# ============================================================================

_pool_lock    = threading.Lock()
_active_pool  = None   # aktuell laufender multiprocessing.Pool


def _register_pool(pool):
    global _active_pool
    with _pool_lock:
        _active_pool = pool


def _unregister_pool():
    global _active_pool
    with _pool_lock:
        _active_pool = None


def terminate_active_pool():
    """Bricht den laufenden Pool sofort ab (Stopp-Button & Fenster-Schließen)."""
    global _active_pool
    with _pool_lock:
        if _active_pool is not None:
            try:
                _active_pool.terminate()
                _active_pool.join()
            except Exception:
                pass
            _active_pool = None


# Sicherheitsnetz: Pool beim Interpreter-Exit automatisch beenden
atexit.register(terminate_active_pool)


# ============================================================================
# VERARBEITUNGSFUNKTIONEN
# ============================================================================

def extract_year_from_date(date_str):
    year_match = re.search(r'(\d{4})', str(date_str))
    if year_match:
        return year_match.group(1)[-2:]
    return "XX"


def setup_output_folders(base_folder, calc_method):
    folders = {
        '01_tDOM':               os.path.join(base_folder, '01_tDOM'),
        '02_tDOM_klass_gebaeude': os.path.join(base_folder, '02_tDOM_klass_gebaeude'),
        '03_tDOM_klass_wald':    os.path.join(base_folder, '03_tDOM_klass_wald'),
        '04_mask_gebaeude':       os.path.join(base_folder, '04_mask_gebaeude'),
        '05_mask_wald':           os.path.join(base_folder, '05_mask_wald'),
        '06_tDOM_final':         os.path.join(base_folder, '06_tDOM_final'),
        'temp':                base_folder
    }
    if calc_method == 1:
        folders['h0_alt'] = os.path.join(base_folder, 'h0_DOM_alt')
        folders['h0_neu'] = os.path.join(base_folder, 'h0_DOM_neu')
    for folder in folders.values():
        os.makedirs(folder, exist_ok=True)
    return folders


def extract_tile_key(filename):
    match = re.search(r'(\d{3})000-(\d{4})000', filename)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


def find_matching_files(dgm_folder, bdom_alt_folder, bdom_neu_folder, calc_method):
    print("Suche nach zusammengehörigen Dateien...")
    dgm_files = {}
    bdom_alt_files = {}
    bdom_neu_files = {}
    large_dgm_file = None

    if calc_method == 1:
        if not os.path.exists(dgm_folder):
            print(f"  ⚠ WARNUNG: DGM-Ordner existiert nicht: {dgm_folder}")
        else:
            dgm_tifs = glob.glob(os.path.join(dgm_folder, "*.tif"))
            print(f"  → DGM-Ordner: {len(dgm_tifs)} TIF-Dateien gefunden")
            for filepath in dgm_tifs:
                key = extract_tile_key(os.path.basename(filepath))
                if key:
                    dgm_files[key] = filepath
            if not dgm_files and dgm_tifs:
                print(f"  → Keine Einzelkacheln gefunden, suche großes DGM-Raster...")
                largest_file = max(dgm_tifs, key=os.path.getsize)
                large_dgm_file = largest_file
                size_mb = os.path.getsize(largest_file) / (1024 ** 2)
                print(f"  → Großes DGM: {os.path.basename(largest_file)} ({size_mb:.1f} MB)")
            else:
                print(f"  → DGM: {len(dgm_files)} Einzelkacheln")

    for filepath in glob.glob(os.path.join(bdom_alt_folder, "*.tif")):
        key = extract_tile_key(os.path.basename(filepath))
        if key:
            bdom_alt_files[key] = filepath
    for filepath in glob.glob(os.path.join(bdom_neu_folder, "*.tif")):
        key = extract_tile_key(os.path.basename(filepath))
        if key:
            bdom_neu_files[key] = filepath

    print(f"  → bDOM Alt: {len(bdom_alt_files)} Dateien")
    print(f"  → bDOM Neu: {len(bdom_neu_files)} Dateien")

    if calc_method == 1:
        if large_dgm_file:
            all_keys = set(bdom_alt_files.keys()) & set(bdom_neu_files.keys())
        else:
            all_keys = (set(dgm_files.keys()) & set(bdom_alt_files.keys()) &
                        set(bdom_neu_files.keys()))
    else:
        all_keys = set(bdom_alt_files.keys()) & set(bdom_neu_files.keys())

    matching_sets = []
    for key in sorted(all_keys):
        tile_set = {'key': key, 'bdom_alt': bdom_alt_files[key], 'bdom_neu': bdom_neu_files[key]}
        if calc_method == 1:
            if large_dgm_file:
                tile_set['dgm'] = large_dgm_file
                tile_set['use_large_dgm'] = True
            else:
                tile_set['dgm'] = dgm_files[key]
                tile_set['use_large_dgm'] = False
        matching_sets.append(tile_set)

    print(f"  ✓ {len(matching_sets)} vollständige Kachel-Sets gefunden\n")
    return matching_sets


def extract_tile_from_large_dgm(large_dgm_path, tile_key, output_path):
    parts = tile_key.split('-')
    x_min, y_min = int(parts[0]) * 1000, int(parts[1]) * 1000
    x_max, y_max = x_min + 1000, y_min + 1000
    print(f"        Extrahiere: X={x_min}-{x_max}m, Y={y_min}-{y_max}m")
    with rasterio.open(large_dgm_path) as src:
        window = from_bounds(x_min, y_min, x_max, y_max, src.transform)
        data = src.read(1, window=window)
        print(f"        Größe: {data.shape[1]}x{data.shape[0]} Pixel")
        window_transform = src.window_transform(window)
        meta = src.meta.copy()
        meta.update({'height': data.shape[0], 'width': data.shape[1], 'transform': window_transform})
        with _safe_raster_write(output_path, **meta) as dst:
            dst.write(data, 1)
    return output_path


def crop_dgm_to_1000x1000(input_path, output_path):
    with rasterio.open(input_path) as src:
        data = src.read(1)[1:, :-1]
        transform = src.transform
        new_transform = rasterio.Affine(
            transform.a, transform.b, transform.c,
            transform.d, transform.e, transform.f - transform.e
        )
        meta = src.meta.copy()
        meta.update({'height': 1000, 'width': 1000, 'transform': new_transform})
        with _safe_raster_write(output_path, **meta) as dst:
            dst.write(data, 1)
    return output_path


def resample_to_02m(input_path, output_path, target_resolution=0.2):
    with rasterio.open(input_path) as src:
        old_transform = src.transform
        scale_factor = src.res[0] / target_resolution
        new_width = int(src.width * scale_factor)
        new_height = int(src.height * scale_factor)
        new_transform = rasterio.Affine(
            target_resolution, old_transform.b, old_transform.c,
            old_transform.d, -target_resolution, old_transform.f
        )
        meta = src.meta.copy()
        meta.update({'height': new_height, 'width': new_width, 'transform': new_transform})
        resampled_data = np.empty((new_height, new_width), dtype=src.dtypes[0])
        reproject(
            source=rasterio.band(src, 1), destination=resampled_data,
            src_transform=old_transform, src_crs=src.crs,
            dst_transform=new_transform, dst_crs=src.crs,
            resampling=Resampling.bilinear
        )
        with _safe_raster_write(output_path, **meta) as dst:
            dst.write(resampled_data, 1)
    return output_path


def subtract_rasters(minuend_path, subtrahend_path, output_path):
    with rasterio.open(minuend_path) as src1:
        with rasterio.open(subtrahend_path) as src2:
            if src1.shape != src2.shape:
                raise ValueError(f"Dimensionen stimmen nicht überein: {src1.shape} vs {src2.shape}")
            data1 = src1.read(1).astype(np.float32)
            data2 = src2.read(1).astype(np.float32)
            result = data1 - data2
            meta = src1.meta.copy()
            meta.update({'dtype': 'float32'})
            with _safe_raster_write(output_path, **meta) as dst:
                dst.write(result, 1)
    return output_path


def klassifiziere_hoehen_multiband(input_raster, output_raster, class_ranges):
    with rasterio.open(input_raster) as src:
        hoehen = src.read(1)
        meta = src.meta.copy()
        original_nodata = src.nodata
        num_bands = len(class_ranges)
        classified = np.zeros((num_bands, hoehen.shape[0], hoehen.shape[1]), dtype=np.uint8)
        for band_idx, (min_val, max_val) in enumerate(class_ranges):
            mask = (hoehen >= min_val) & (hoehen <= max_val)
            classified[band_idx][mask] = 1
            if original_nodata is not None:
                classified[band_idx][hoehen == original_nodata] = 0
        meta.update({'dtype': 'uint8', 'count': num_bands, 'nodata': 0,
                     'driver': 'GTiff', 'compress': 'lzw'})
        with _safe_raster_write(output_raster, **meta) as dst:
            for band_idx in range(num_bands):
                dst.write(classified[band_idx], band_idx + 1)
    stats = {f"Band {i+1} ({mn}-{mx}m)": int(np.sum(classified[i] == 1))
             for i, (mn, mx) in enumerate(class_ranges)}
    return stats


def process_single_band(args):
    """Top-level-Funktion für multiprocessing (muss auf Modulebene liegen)"""
    band_idx, data, min_pixels, connectivity = args
    valid_mask = data == 1
    if np.sum(valid_mask) == 0:
        return band_idx, data.copy(), 0, 0, 0
    structure = ndimage.generate_binary_structure(2, 1 if connectivity == 4 else 2)
    labeled_array, num_features = label(valid_mask, structure=structure)
    region_sizes = ndimage.sum(valid_mask, labeled_array, range(1, num_features + 1))
    small_regions = np.where(region_sizes < min_pixels)[0] + 1
    filtered_band = data.copy()
    removed_regions = removed_pixels = 0
    if len(small_regions) > 0:
        removed_pixels = int(np.sum(region_sizes[small_regions - 1]))
        removed_regions = len(small_regions)
        for region_id in small_regions:
            filtered_band[labeled_array == region_id] = 0
    return band_idx, filtered_band, num_features, removed_regions, removed_pixels


def filter_small_regions_multiband_parallel(input_raster, output_raster, min_pixels=500,
                                             connectivity=8, num_processes=None):
    with rasterio.open(input_raster) as src:
        num_bands = src.count
        profile = src.profile.copy()
        all_bands_data = [src.read(b + 1) for b in range(num_bands)]

    args_list = [(b, all_bands_data[b], min_pixels, connectivity) for b in range(num_bands)]
    if num_processes is None:
        num_processes = min(cpu_count(), num_bands)

    # Pool registrieren, damit er bei Stopp/Schließen sofort beendet werden kann
    pool = Pool(processes=num_processes)
    _register_pool(pool)
    try:
        results = pool.map(process_single_band, args_list)
    finally:
        pool.close()
        pool.join()
        _unregister_pool()

    filtered_data = np.zeros((num_bands, all_bands_data[0].shape[0],
                               all_bands_data[0].shape[1]), dtype=np.uint8)
    total_found = total_removed = total_removed_px = 0
    for band_idx, filtered_band, num_features, removed_regions, removed_pixels in results:
        filtered_data[band_idx] = filtered_band
        total_found += num_features
        total_removed += removed_regions
        total_removed_px += removed_pixels

    with _safe_raster_write(output_raster, **profile) as dst:
        for b in range(num_bands):
            dst.write(filtered_data[b], b + 1)
    return total_found, total_removed, total_removed_px


def extract_with_combined_masks(original_raster, mask_gebaeude, mask_wald, output_raster):
    with rasterio.open(original_raster) as src_orig:
        original_data = src_orig.read(1)
        profile = src_orig.profile.copy()
        original_nodata = src_orig.nodata

    combined_mask = np.zeros(original_data.shape, dtype=bool)
    with rasterio.open(mask_gebaeude) as src:
        for b in range(src.count):
            combined_mask |= (src.read(b + 1) == 1)
    with rasterio.open(mask_wald) as src:
        for b in range(src.count):
            combined_mask |= (src.read(b + 1) == 1)

    nodata_val = original_nodata if original_nodata is not None else np.nan
    output_data = np.full(original_data.shape, nodata_val, dtype=np.float32)
    output_data[combined_mask] = original_data[combined_mask]

    extracted_values = original_data[combined_mask]
    num_extracted = int(np.sum(combined_mask))
    stats = {
        'num_extracted': num_extracted,
        'percentage': (num_extracted / combined_mask.size) * 100,
        'area_m2': num_extracted * 0.04,
        'min': float(np.min(extracted_values)) if len(extracted_values) > 0 else 0,
        'max': float(np.max(extracted_values)) if len(extracted_values) > 0 else 0,
        'mean': float(np.mean(extracted_values)) if len(extracted_values) > 0 else 0
    }

    if original_nodata is not None:
        profile['nodata'] = original_nodata
    profile['dtype'] = 'float32'
    with _safe_raster_write(output_raster, **profile) as dst:
        dst.write(output_data, 1)
    return stats


def check_complete_tileset_exists(key, output_folders, year_alt, year_neu, calc_method):
    files = [
        os.path.join(output_folders['01_tDOM'], f"tDOM_{year_alt}_{year_neu}_{key}.tif"),
        os.path.join(output_folders['02_tDOM_klass_gebaeude'], f"tDOM_klass_geb_{year_alt}_{year_neu}_{key}.tif"),
        os.path.join(output_folders['03_tDOM_klass_wald'], f"tDOM_klass_wald_{year_alt}_{year_neu}_{key}.tif"),
        os.path.join(output_folders['04_mask_gebaeude'], f"mask_geb_{year_alt}_{year_neu}_{key}.tif"),
        os.path.join(output_folders['05_mask_wald'], f"mask_wald_{year_alt}_{year_neu}_{key}.tif"),
        os.path.join(output_folders['06_tDOM_final'], f"tDOM_final_{year_alt}_{year_neu}_33{key}.tif"),
    ]
    if calc_method == 1:
        files += [
            os.path.join(output_folders['h0_alt'], f"h0_DOM_{year_alt}_{key}.tif"),
            os.path.join(output_folders['h0_neu'], f"h0_DOM_{year_neu}_{key}.tif"),
        ]
    return all(os.path.exists(f) for f in files)


def process_tile(tile_set, output_folders, config, stop_event=None):
    """Verarbeitet ein einzelnes Kachel-Set"""
    key          = tile_set['key']
    year_alt     = config['year_alt']
    year_neu     = config['year_neu']
    calc_method  = config['calc_method']
    min_pix_geb  = config['min_pixels_geb']
    min_pix_wald = config['min_pixels_wald']
    connectivity = config['connectivity']
    num_proc     = config['num_processes']
    ranges_geb   = config['class_ranges_geb']
    ranges_wald  = config['class_ranges_wald']
    file_policy  = config['file_conflict_policy']
    tile_policy  = config['tile_conflict_policy']

    print(f"\n{'=' * 70}")
    print(f"Verarbeite Kachel: {key}")
    print(f"{'=' * 70}")
    t_start = datetime.datetime.now()
    print(f"Start: {t_start.strftime('%Y-%m-%d %H:%M:%S')}")

    if check_complete_tileset_exists(key, output_folders, year_alt, year_neu, calc_method):
        if tile_policy == 'use_all':
            print(f"  → Vollständiges Set vorhanden – übersprungen")
            return True
        else:
            print(f"  → Vollständiges Set vorhanden – wird neu berechnet")

    def should_write(path):
        if not os.path.exists(path):
            return True
        if file_policy == 'skip_all':
            print(f"        → Existiert, übersprungen: {os.path.basename(path)}")
            return False
        return True

    def check_stop():
        if stop_event and stop_event.is_set():
            print("  ⚠ Abbruch durch Benutzer")
            return True
        return False

    out_hdiff = os.path.join(output_folders['01_tDOM'], f"tDOM_{year_alt}_{year_neu}_{key}.tif")
    out_klass_geb = os.path.join(output_folders['02_tDOM_klass_gebaeude'], f"tDOM_klass_geb_{year_alt}_{year_neu}_{key}.tif")
    out_klass_wald = os.path.join(output_folders['03_tDOM_klass_wald'], f"tDOM_klass_wald_{year_alt}_{year_neu}_{key}.tif")
    out_mask_geb = os.path.join(output_folders['04_mask_gebaeude'], f"mask_geb_{year_alt}_{year_neu}_{key}.tif")
    out_mask_wald = os.path.join(output_folders['05_mask_wald'], f"mask_wald_{year_alt}_{year_neu}_{key}.tif")
    out_final = os.path.join(output_folders['06_tDOM_final'], f"tDOM_final_{year_alt}_{year_neu}_33{key}.tif")

    steps     = 9 if calc_method == 1 else 6
    temp_files = []

    try:
        if check_stop(): return False

        if calc_method == 1:
            temp_dgm_tile = os.path.join(output_folders['temp'], f"DGM_{key}_tile.tif")
            temp_dgm_1000 = os.path.join(output_folders['temp'], f"DGM_{key}_1000.tif")
            temp_dgm_20cm = os.path.join(output_folders['temp'], f"DGM_{key}_20cm.tif")
            out_h0_alt    = os.path.join(output_folders['h0_alt'], f"h0_DOM_{year_alt}_{key}.tif")
            out_h0_neu    = os.path.join(output_folders['h0_neu'], f"h0_DOM_{year_neu}_{key}.tif")

            if tile_set.get('use_large_dgm', False):
                print(f"  [1/9] Extrahiere DGM-Kachel aus großem Raster...")
                extract_tile_from_large_dgm(tile_set['dgm'], key, temp_dgm_tile)
                temp_files.append(temp_dgm_tile)
                print(f"  [2/9] Croppe DGM auf 1000x1000...")
                crop_dgm_to_1000x1000(temp_dgm_tile, temp_dgm_1000)
            else:
                print(f"  [1/9] Croppe DGM auf 1000x1000...")
                crop_dgm_to_1000x1000(tile_set['dgm'], temp_dgm_1000)
            temp_files.append(temp_dgm_1000)

            print(f"  [3/9] Resample DGM auf 0.2m...")
            resample_to_02m(temp_dgm_1000, temp_dgm_20cm)
            temp_files.append(temp_dgm_20cm)

            print(f"  [4/9] Berechne h0_DOM_alt (DOM_alt - DGM)...")
            if should_write(out_h0_alt):
                subtract_rasters(tile_set['bdom_alt'], temp_dgm_20cm, out_h0_alt)

            print(f"  [5/9] Berechne h0_DOM_neu (DOM_neu - DGM)...")
            if should_write(out_h0_neu):
                subtract_rasters(tile_set['bdom_neu'], temp_dgm_20cm, out_h0_neu)

            print(f"  [6/9] Berechne tDOM (h0_neu - h0_alt)...")
            if should_write(out_hdiff):
                subtract_rasters(out_h0_neu, out_h0_alt, out_hdiff)
            step_offset = 6
        else:
            print(f"  [1/6] Berechne tDOM (DOM_neu - DOM_alt)...")
            if should_write(out_hdiff):
                subtract_rasters(tile_set['bdom_neu'], tile_set['bdom_alt'], out_hdiff)
            step_offset = 1

        if check_stop(): return False

        print(f"  [{step_offset+1}/{steps}] Klassifiziere Gebäude ({len(ranges_geb)} Klassen)...")
        if should_write(out_klass_geb):
            stats = klassifiziere_hoehen_multiband(out_hdiff, out_klass_geb, ranges_geb)
            print(f"        → {sum(stats.values()):,} Pixel klassifiziert")

        print(f"  [{step_offset+2}/{steps}] Klassifiziere Wald ({len(ranges_wald)} Klassen)...")
        if should_write(out_klass_wald):
            stats = klassifiziere_hoehen_multiband(out_hdiff, out_klass_wald, ranges_wald)
            print(f"        → {sum(stats.values()):,} Pixel klassifiziert")

        if check_stop(): return False

        print(f"  [{step_offset+3}/{steps}] Filtere Gebäude (min. {min_pix_geb} Pixel, {num_proc} Kerne)...")
        if should_write(out_mask_geb):
            found, removed, _ = filter_small_regions_multiband_parallel(
                out_klass_geb, out_mask_geb, min_pix_geb, connectivity, num_proc)
            print(f"        → Gefunden: {found} | Entfernt: {removed} | Verbleibend: {found - removed}")

        print(f"  [{step_offset+4}/{steps}] Filtere Wald (min. {min_pix_wald} Pixel, {num_proc} Kerne)...")
        if should_write(out_mask_wald):
            found, removed, _ = filter_small_regions_multiband_parallel(
                out_klass_wald, out_mask_wald, min_pix_wald, connectivity, num_proc)
            print(f"        → Gefunden: {found} | Entfernt: {removed} | Verbleibend: {found - removed}")

        print(f"  [{step_offset+5}/{steps}] Extrahiere alle Cluster aus tDOM...")
        if should_write(out_final):
            stats = extract_with_combined_masks(out_hdiff, out_mask_geb, out_mask_wald, out_final)
            print(f"        → {stats['num_extracted']:,} Pixel ({stats['percentage']:.2f}%, {stats['area_m2']:.2f} m²)")

        for f in temp_files:
            if os.path.exists(f):
                os.remove(f)

        t_end = datetime.datetime.now()
        print(f"\n  ✓ Kachel {key} erfolgreich verarbeitet")
        print(f"Ende: {t_end.strftime('%Y-%m-%d %H:%M:%S')} | Dauer: {str(t_end - t_start).split('.')[0]}")
        return True

    except Exception as e:
        t_end = datetime.datetime.now()
        print(f"\n  ✗ FEHLER bei Kachel {key}: {e}")
        print(f"Fehlerzeitpunkt: {t_end.strftime('%Y-%m-%d %H:%M:%S')} | Dauer: {str(t_end - t_start).split('.')[0]}")
        for f in temp_files:
            try:
                if os.path.exists(f): os.remove(f)
            except: pass
        return False


def run_processing(config, stop_event=None, progress_callback=None):
    """Hauptschleife – wird im Hintergrund-Thread aufgerufen"""
    t_global = datetime.datetime.now()
    print("=" * 80)
    print("  TERRAIN-DIFFERENZ-BERECHNUNG")
    print("=" * 80)
    print(f"Start: {t_global.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Methode: {config['calc_method']} ({'(DOM_n-DGM)-(DOM_a-DGM)' if config['calc_method'] == 1 else 'DOM_n - DOM_a'})")
    print(f"Zeitraum: {config['date_alt']} → {config['date_neu']}  (Kürzel: {config['year_alt']} / {config['year_neu']})")
    print(f"Gebäude: {len(config['class_ranges_geb'])} Klassen | min. {config['min_pixels_geb']} Pixel")
    print(f"Wald:    {len(config['class_ranges_wald'])} Klassen | min. {config['min_pixels_wald']} Pixel")
    print(f"CPU-Kerne: {config['num_processes']} | Konnektivität: {config['connectivity']}")
    print()

    output_folders = setup_output_folders(config['output_base'], config['calc_method'])
    matching_sets  = find_matching_files(
        config.get('folder_dgm', ''),
        config['folder_bdom_alt'],
        config['folder_bdom_neu'],
        config['calc_method']
    )

    if not matching_sets:
        print("✗ Keine vollständigen Kachel-Sets gefunden! Bitte Ordnerpfade prüfen.")
        return

    total = len(matching_sets)
    print(f"Verarbeite {total} Kacheln ...\n")
    if progress_callback:
        progress_callback(0, total)
    successful = failed = 0

    for i, tile_set in enumerate(matching_sets, 1):
        if stop_event and stop_event.is_set():
            print("\n⚠ Verarbeitung abgebrochen.")
            if progress_callback:
                progress_callback(i - 1, total, aborted=True)
            break
        print(f"\nKachel {i} / {total}")
        ok = process_tile(tile_set, output_folders, config, stop_event)
        if ok:
            successful += 1
        else:
            failed += 1
        if progress_callback:
            progress_callback(i, total, aborted=False)

    t_end = datetime.datetime.now()
    print("\n" + "=" * 80)
    print("  VERARBEITUNG ABGESCHLOSSEN")
    print("=" * 80)
    print(f"Erfolgreich: {successful} / {len(matching_sets)}")
    if failed:
        print(f"Fehlgeschlagen: {failed}")
    print(f"Ende: {t_end.strftime('%Y-%m-%d %H:%M:%S')} | Gesamtdauer: {str(t_end - t_global).split('.')[0]}")


# ============================================================================
# GUI – HILFSKLASSEN
# ============================================================================

class TextRedirector:
    """Leitet stdout in eine tkinter Text-Widget um (thread-sicher via Queue)."""

    def __init__(self, text_widget):
        self.text_widget = text_widget
        self.queue = queue.Queue()
        self.log_buffer = []
        self.active = True

    def write(self, s):
        if self.active:
            self.log_buffer.append(s)
            self.queue.put(s)

    def get_log_text(self):
        return "".join(self.log_buffer)

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


class RangeEditorDialog(tk.Toplevel):
    """Dialog zum Hinzufügen/Bearbeiten eines Wertebereichs"""

    def __init__(self, parent, title, min_val=None, max_val=None):
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Mindestwert (m):").grid(row=0, column=0, sticky=tk.W, pady=6, padx=5)
        self.var_min = tk.StringVar(value=str(int(min_val)) if min_val is not None else "")
        ttk.Entry(frame, textvariable=self.var_min, width=12).grid(row=0, column=1, padx=5)

        ttk.Label(frame, text="Maximalwert (m):").grid(row=1, column=0, sticky=tk.W, pady=6, padx=5)
        self.var_max = tk.StringVar(value=str(int(max_val)) if max_val is not None else "")
        ttk.Entry(frame, textvariable=self.var_max, width=12).grid(row=1, column=1, padx=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text="  OK  ", command=self._ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Abbrechen", command=self.destroy).pack(side=tk.LEFT, padx=6)

        self.bind('<Return>', lambda e: self._ok())
        self.bind('<Escape>', lambda e: self.destroy())
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _ok(self):
        try:
            mn = float(self.var_min.get())
            mx = float(self.var_max.get())
        except ValueError:
            messagebox.showerror("Fehler", "Bitte gültige Zahlen eingeben!", parent=self)
            return
        if mn >= mx:
            messagebox.showerror("Fehler", "Mindestwert muss kleiner als Maximalwert sein!", parent=self)
            return
        self.result = (mn, mx)
        self.destroy()


class ClassRangesFrame(ttk.LabelFrame):
    """Interaktiver Editor für Klassenbereiche (Min/Max-Tabelle)"""

    def __init__(self, parent, title, initial_ranges=None, **kwargs):
        super().__init__(parent, text=title, **kwargs)
        self.ranges = list(initial_ranges) if initial_ranges else []
        self._build()

    def _build(self):
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        cols = ('band', 'min', 'max', 'breite')
        self.tree = ttk.Treeview(tree_frame, columns=cols, show='headings', height=12,
                                  selectmode='browse')
        self.tree.heading('band',   text='Band')
        self.tree.heading('min',    text='Min (m)')
        self.tree.heading('max',    text='Max (m)')
        self.tree.heading('breite', text='Breite (m)')
        self.tree.column('band',   width=70,  anchor=tk.CENTER)
        self.tree.column('min',    width=90,  anchor=tk.CENTER)
        self.tree.column('max',    width=90,  anchor=tk.CENTER)
        self.tree.column('breite', width=90,  anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind('<Double-1>', lambda e: self._edit())

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="+ Hinzufügen",   command=self._add      ).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="✎ Bearbeiten",   command=self._edit     ).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="✕ Entfernen",    command=self._remove   ).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="↑ Hoch",         command=self._move_up  ).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="↓ Runter",       command=self._move_down).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="⟳ Standard",     command=lambda: self._reset()).pack(side=tk.RIGHT, padx=3)

        self._refresh()

    def _refresh(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, (mn, mx) in enumerate(self.ranges, 1):
            mn_disp = int(mn) if mn == int(mn) else mn
            mx_disp = int(mx) if mx == int(mx) else mx
            self.tree.insert('', tk.END, values=(f"Band {i}", mn_disp, mx_disp, mx - mn))

    def _selected_idx(self):
        sel = self.tree.selection()
        return self.tree.index(sel[0]) if sel else None

    def _add(self):
        dlg = RangeEditorDialog(self.winfo_toplevel(), "Bereich hinzufügen")
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            self.ranges.append(dlg.result)
            self._refresh()

    def _edit(self):
        idx = self._selected_idx()
        if idx is None:
            messagebox.showwarning("Hinweis", "Bitte zuerst einen Eintrag auswählen.",
                                   parent=self.winfo_toplevel())
            return
        mn, mx = self.ranges[idx]
        dlg = RangeEditorDialog(self.winfo_toplevel(), "Bereich bearbeiten", mn, mx)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            self.ranges[idx] = dlg.result
            self._refresh()

    def _remove(self):
        idx = self._selected_idx()
        if idx is not None:
            self.ranges.pop(idx)
            self._refresh()

    def _move_up(self):
        idx = self._selected_idx()
        if idx and idx > 0:
            self.ranges[idx], self.ranges[idx-1] = self.ranges[idx-1], self.ranges[idx]
            self._refresh()
            self.tree.selection_set(self.tree.get_children()[idx-1])

    def _move_down(self):
        idx = self._selected_idx()
        if idx is not None and idx < len(self.ranges) - 1:
            self.ranges[idx], self.ranges[idx+1] = self.ranges[idx+1], self.ranges[idx]
            self._refresh()
            self.tree.selection_set(self.tree.get_children()[idx+1])

    def _reset(self):
        pass  # wird von außen gesetzt

    def get_ranges(self):
        return [(int(mn) if mn == int(mn) else mn, int(mx) if mx == int(mx) else mx)
                for mn, mx in self.ranges]


# ============================================================================
# HAUPTANWENDUNG
# ============================================================================

class TerrainDiffApp:

    def __init__(self, root):
        self.root = root
        self.root.title("bDOM Veränderungsdetektion")

        try:
            icon_file = resource_path("Programm_ICON_bDOM_Filterung.ico")
            self.root.iconbitmap(icon_file)
        except Exception:
            pass

        self.root.geometry("950x800")
        self.root.minsize(565, 720)

        self.stop_event = threading.Event()
        self.proc_thread = None
        self.redirector = None

        self._build_menu()
        self._build_notebook()
        self._build_statusbar()

        self._load_settings_auto()

    # ------------------------------------------------------------------
    # MENÜ
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Einstellungen speichern …", command=self._save_settings_dialog,
                           accelerator="Ctrl+S")
        m_file.add_command(label="Einstellungen laden …",    command=self._load_settings_dialog)
        m_file.add_separator()
        m_file.add_command(label="Beenden", command=self._on_close)
        menubar.add_cascade(label="Datei", menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Anleitung",  command=self._show_anleitung)
        m_help.add_command(label="Info",       command=self._show_info)
        m_help.add_separator()
        m_help.add_command(label="Kontakt", command=self._show_kontakt)
        menubar.add_cascade(label="Hilfe", menu=m_help)

        m_ueber = tk.Menu(menubar, tearoff=0)
        m_ueber.add_command(label="Über …",     command=self._show_about)
        m_ueber.add_command(label="Bachelorarbeit herunterladen", command=self._download_bachelorarbeit)
        m_ueber.add_separator()
        m_ueber.add_command(label="Lizenz",     command=self._show_lizenz)
        menubar.add_cascade(label="Über/Lizenz", menu=m_ueber)

        self.root.config(menu=menubar)
        self.root.bind('<Control-s>', lambda e: self._save_settings_dialog())

    # ------------------------------------------------------------------
    # TABS
    # ------------------------------------------------------------------

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 0))

        self._build_tab_ordner()
        self._build_tab_parameter()
        self._build_tab_gebaeude()
        self._build_tab_wald()
        self._build_tab_ausgabe()

    # --- Tab 1: Ordner & Methode ---

    def _build_tab_ordner(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  📁  Dateneingabe  ")

        self.v_dgm      = tk.StringVar()
        self.v_bdom_alt = tk.StringVar()
        self.v_bdom_neu = tk.StringVar()
        self.v_output   = tk.StringVar()
        self.v_date_alt = tk.StringVar(value="2020")
        self.v_date_neu = tk.StringVar(value="2023")

        # --- bDOM Alt ---
        f_alt = ttk.LabelFrame(tab, text="bDOM Alt", padding=12)
        f_alt.pack(fill=tk.X, padx=12, pady=(12, 6))
        f_alt.columnconfigure(1, weight=1)

        ttk.Label(f_alt, text="Ordner *:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_alt, textvariable=self.v_bdom_alt).grid(
            row=0, column=1, sticky=tk.EW, padx=6, pady=5)
        ttk.Button(f_alt, text="📁", width=3,
                   command=lambda: self.v_bdom_alt.set(
                       filedialog.askdirectory(title="bDOM Alt – Ordner wählen") or self.v_bdom_alt.get()
                   )).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))

        ttk.Label(f_alt, text="Datum *:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_alt, textvariable=self.v_date_alt, width=20).grid(
            row=1, column=1, sticky=tk.W, padx=6, pady=5)
        ttk.Label(f_alt, text="z. B. 2020 oder 20.04.2020  (Jahr wird als Dateinamen-Kürzel verwendet)",
                  foreground="gray").grid(row=2, column=1, columnspan=2, sticky=tk.W, padx=6, pady=(0, 4))

        # --- bDOM Neu ---
        f_neu = ttk.LabelFrame(tab, text="bDOM Neu", padding=12)
        f_neu.pack(fill=tk.X, padx=12, pady=6)
        f_neu.columnconfigure(1, weight=1)

        ttk.Label(f_neu, text="Ordner *:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_neu, textvariable=self.v_bdom_neu).grid(
            row=0, column=1, sticky=tk.EW, padx=6, pady=5)
        ttk.Button(f_neu, text="📁", width=3,
                   command=lambda: self.v_bdom_neu.set(
                       filedialog.askdirectory(title="bDOM Neu – Ordner wählen") or self.v_bdom_neu.get()
                   )).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))

        ttk.Label(f_neu, text="Datum *:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_neu, textvariable=self.v_date_neu, width=20).grid(
            row=1, column=1, sticky=tk.W, padx=6, pady=5)
        ttk.Label(f_neu, text="z. B. 2023 oder 21.04.2023  (Jahr wird als Dateinamen-Kürzel verwendet)",
                  foreground="gray").grid(row=2, column=1, columnspan=2, sticky=tk.W, padx=6, pady=(0, 4))

        # --- Ausgabe ---
        f_out = ttk.LabelFrame(tab, text="Ausgabe", padding=12)
        f_out.pack(fill=tk.X, padx=12, pady=6)
        f_out.columnconfigure(1, weight=1)

        ttk.Label(f_out, text="Ausgabe-Ordner *:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Entry(f_out, textvariable=self.v_output, width=52).grid(
            row=0, column=1, sticky=tk.EW, padx=6, pady=5)
        ttk.Button(f_out, text="📁", width=3,
                   command=lambda: self.v_output.set(
                       filedialog.askdirectory(title="Ausgabe-Ordner wählen") or self.v_output.get()
                   )).grid(row=0, column=2, padx=3)

        f_hint = ttk.LabelFrame(tab, text="Hinweise", padding=10)
        f_hint.pack(fill=tk.X, padx=12, pady=6)
        ttk.Label(f_hint, text="* = Pflichtfeld", foreground="gray").pack(anchor=tk.W)
        ttk.Label(f_hint, text="Erwartetes Dateischmema: 1 Km² bDOM Kacheln mit 0,20 m räumlicher Auflösung\n"
                               "Erwartetes Dateinamenschema:  bDOM_<NNN>000-<MMMM>000.tif\n"
                               "Beispiel:  BDOM_389000-5753000.tif", foreground="gray").pack(anchor=tk.W)

    def _on_method_change(self):
        state = 'normal' if self.v_method.get() == 1 else 'disabled'
        self.entry_dgm.configure(state=state)
        self.btn_dgm.configure(state=state)

    # --- Tab 2: Parameter ---

    def _build_tab_parameter(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  ⚙  Einstellungen  ")

        # Berechnungsmethode
        f_meth = ttk.LabelFrame(tab, text="Berechnungsmethode", padding=12)
        f_meth.pack(fill=tk.X, padx=12, pady=(12, 6))
        f_meth.columnconfigure(1, weight=1)

        self.v_method = tk.IntVar(value=2)
        ttk.Radiobutton(f_meth, text="Methode 1: (DOM_neu − DGM) − (DOM_alt − DGM)",
                        variable=self.v_method, value=1,
                        command=self._on_method_change).grid(row=0, column=0, sticky=tk.W, pady=3, padx=6)
        ttk.Radiobutton(f_meth, text="Methode 2: DOM_neu − DOM_alt  (direkt, kein DGM erforderlich)",
                        variable=self.v_method, value=2,
                        command=self._on_method_change).grid(row=1, column=0, sticky=tk.W, pady=3, padx=6)

        # DGM-Ordner
        f_dgm = ttk.LabelFrame(tab, text="DGM-Eingabe  (nur Methode 1)", padding=12)
        f_dgm.pack(fill=tk.X, padx=12, pady=(0, 6))
        f_dgm.columnconfigure(1, weight=1)

        ttk.Label(f_dgm, text="DGM-Ordner:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=5)
        self.entry_dgm = ttk.Entry(f_dgm, textvariable=self.v_dgm, width=44, state='disabled')
        self.entry_dgm.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=5)
        self.btn_dgm = ttk.Button(f_dgm, text="📁", width=3, state='disabled',
                                   command=lambda: self.v_dgm.set(
                                       filedialog.askdirectory(title="DGM-Ordner wählen") or self.v_dgm.get()
                                   ))
        self.btn_dgm.grid(row=0, column=2, padx=3)
        ttk.Label(f_dgm, text="Erwartet einzelne KM²-Kacheln",
                  foreground="gray").grid(row=0, column=3, sticky=tk.W, padx=8)

        # ── Connected-Components-Filterung ──────────────────────────────────
        f_filt = ttk.LabelFrame(tab, text="Connected-Components-Filterung", padding=12)
        f_filt.pack(fill=tk.X, padx=12, pady=6)
        f_filt.columnconfigure(1, weight=0)

        self.v_min_geb  = tk.StringVar(value=str(DEFAULT_MIN_PIXELS_GEB))
        self.v_min_wald = tk.StringVar(value=str(DEFAULT_MIN_PIXELS_WALD))
        self.v_conn     = tk.IntVar(value=DEFAULT_CONNECTIVITY)
        self.v_nproc    = tk.StringVar(value=DEFAULT_NPROC)

        ttk.Label(f_filt, text="Min. Pixel Gebäude:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Spinbox(f_filt, textvariable=self.v_min_geb,  from_=1, to=999999, width=10
                    ).grid(row=0, column=1, sticky=tk.W, padx=6)

        ttk.Label(f_filt, text="Min. Pixel Wald:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Spinbox(f_filt, textvariable=self.v_min_wald, from_=1, to=999999, width=10
                    ).grid(row=1, column=1, sticky=tk.W, padx=6)

        ttk.Label(f_filt, text="Konnektivität:").grid(row=2, column=0, sticky=tk.W, padx=6, pady=5)
        conn_f = ttk.Frame(f_filt)
        conn_f.grid(row=2, column=1, sticky=tk.W, padx=6)
        ttk.Radiobutton(conn_f, text="4-fach", variable=self.v_conn, value=4).pack(side=tk.LEFT)
        ttk.Radiobutton(conn_f, text="8-fach", variable=self.v_conn, value=8).pack(side=tk.LEFT, padx=12)

        ttk.Label(f_filt, text="CPU-Kerne:").grid(row=3, column=0, sticky=tk.W, padx=6, pady=5)
        proc_f = ttk.Frame(f_filt)
        proc_f.grid(row=3, column=1, sticky=tk.W, padx=6)
        ttk.Entry(proc_f, textvariable=self.v_nproc, width=8).pack(side=tk.LEFT)
        ttk.Label(proc_f, text=f"  ('auto' = alle {cpu_count()} verfügbaren)",
                  foreground="gray").pack(side=tk.LEFT)

        # ── Reset-Button: unten links, identisches Design wie Klassen-Tabs ──
        ttk.Button(f_filt, text="⟳ Standard",
                   command=self._reset_filter_params
                   ).grid(row=4, column=0, columnspan=3, sticky=tk.W, padx=6, pady=(8, 2))

        # Konflikt-Strategie
        f_kon = ttk.LabelFrame(tab, text="Konflikt-Strategie (bei bereits vorhandenen Dateien)", padding=12)
        f_kon.pack(fill=tk.X, padx=12, pady=6)

        self.v_file_policy = tk.StringVar(value="overwrite_all")
        self.v_tile_policy = tk.StringVar(value="recalculate_all")

        ttk.Label(f_kon, text="Einzel-Datei:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Radiobutton(f_kon, text="Überschreiben",  variable=self.v_file_policy,
                        value="overwrite_all").grid(row=0, column=1, sticky=tk.W, padx=6)
        ttk.Radiobutton(f_kon, text="Überspringen",   variable=self.v_file_policy,
                        value="skip_all").grid(row=0, column=2, sticky=tk.W, padx=6)

        ttk.Label(f_kon, text="Vollständige Kachel:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=5)
        ttk.Radiobutton(f_kon, text="Neu berechnen",  variable=self.v_tile_policy,
                        value="recalculate_all").grid(row=1, column=1, sticky=tk.W, padx=6)
        ttk.Radiobutton(f_kon, text="Überspringen",   variable=self.v_tile_policy,
                        value="use_all").grid(row=1, column=2, sticky=tk.W, padx=6)

        # Protokolldatei
        f_log = ttk.LabelFrame(tab, text="Protokolldatei", padding=12)
        f_log.pack(fill=tk.X, padx=12, pady=6)

        self.v_write_log = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_log,
                        text="Protokolldatei nach Verarbeitung automatisch speichern",
                        variable=self.v_write_log).grid(row=0, column=0, columnspan=3,
                                                        sticky=tk.W, padx=6, pady=4)
        ttk.Label(f_log,
                  text="Datei wird im Ausgabe-Ordner gespeichert:  Berechnungsprotokoll_JJJJMMTT_HHMMSS.txt",
                  foreground="gray").grid(row=1, column=0, columnspan=3,
                                          sticky=tk.W, padx=6, pady=(0, 4))

    def _reset_filter_params(self):
        """Setzt die Filterparameter auf die vordefinierten Standardwerte zurück."""
        if messagebox.askyesno("Zurücksetzen",
                               "Filterparameter auf Standardwerte zurücksetzen?\n\n"
                               f"  Min. Pixel Gebäude : {DEFAULT_MIN_PIXELS_GEB}\n"
                               f"  Min. Pixel Wald    : {DEFAULT_MIN_PIXELS_WALD}\n"
                               f"  Konnektivität      : {DEFAULT_CONNECTIVITY}-fach\n"
                               f"  CPU-Kerne          : {DEFAULT_NPROC}"):
            self.v_min_geb.set(str(DEFAULT_MIN_PIXELS_GEB))
            self.v_min_wald.set(str(DEFAULT_MIN_PIXELS_WALD))
            self.v_conn.set(DEFAULT_CONNECTIVITY)
            self.v_nproc.set(DEFAULT_NPROC)

    # --- Tab 3: Gebäude-Klassen ---

    def _build_tab_gebaeude(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  🏢  Gebäude-Klassen  ")
        self.ed_geb = ClassRangesFrame(
            tab,
            "Gebäude-Klassenbereiche  –  Höhendifferenz in Metern",
            initial_ranges=DEFAULT_CLASS_RANGES_GEBAEUDE,
            padding=10
        )
        self.ed_geb._reset = lambda: self._reset_ranges(
            self.ed_geb, DEFAULT_CLASS_RANGES_GEBAEUDE)
        self.ed_geb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # --- Tab 4: Wald-Klassen ---

    def _build_tab_wald(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  🌲  Wald-Klassen  ")
        self.ed_wald = ClassRangesFrame(
            tab,
            "Wald-Klassenbereiche  –  Höhendifferenz in Metern",
            initial_ranges=DEFAULT_CLASS_RANGES_WALD,
            padding=10
        )
        self.ed_wald._reset = lambda: self._reset_ranges(
            self.ed_wald, DEFAULT_CLASS_RANGES_WALD)
        self.ed_wald.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def _reset_ranges(self, editor, defaults):
        if messagebox.askyesno("Zurücksetzen",
                               "Klassenbereiche auf Standardwerte zurücksetzen?"):
            editor.ranges = list(defaults)
            editor._refresh()

    # --- Tab 5: Ausgabe ---

    def _build_tab_ausgabe(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  📋  Ausgabe  ")

        self.output_text = scrolledtext.ScrolledText(
            tab, state='disabled', font=('Consolas', 9),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='white',
            wrap=tk.WORD
        )
        self.output_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))

        btn_f = ttk.Frame(tab)
        btn_f.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Button(btn_f, text="Ausgabe leeren", command=self._clear_output).pack(side=tk.RIGHT)

    def _clear_output(self):
        self.output_text.configure(state='normal')
        self.output_text.delete('1.0', tk.END)
        self.output_text.configure(state='disabled')

    # ------------------------------------------------------------------
    # STATUSLEISTE
    # ------------------------------------------------------------------

    def _build_statusbar(self):
        outer = ttk.Frame(self.root, relief=tk.SUNKEN)
        outer.pack(fill=tk.X, side=tk.BOTTOM, padx=0, pady=0)

        bar_frame = ttk.Frame(outer)
        bar_frame.pack(fill=tk.X, padx=8, pady=(6, 2))

        self.lbl_kachel = ttk.Label(bar_frame, text="", width=22, anchor=tk.W)
        self.lbl_kachel.pack(side=tk.LEFT)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progressbar  = ttk.Progressbar(
            bar_frame, variable=self.progress_var,
            maximum=100, mode='determinate', length=400
        )
        self.progressbar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))

        self.lbl_pct = ttk.Label(bar_frame, text="", width=7, anchor=tk.E)
        self.lbl_pct.pack(side=tk.LEFT)

        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 6))

        self.btn_stop  = ttk.Button(btn_frame, text="■  Stopp", command=self._stop,
                                    state=tk.DISABLED, width=12)
        self.btn_stop.pack(side=tk.RIGHT, padx=(4, 0))

        self.btn_start = ttk.Button(btn_frame, text="▶  Verarbeitung starten",
                                    command=self._start)
        self.btn_start.pack(side=tk.RIGHT, padx=4)

        self.lbl_status = ttk.Label(btn_frame, text="Bereit")
        self.lbl_status.pack(side=tk.LEFT, padx=(2, 0))

    def _update_progress(self, done, total, aborted=False):
        if total == 0:
            return
        pct = (done / total) * 100
        self.progress_var.set(pct)
        self.lbl_pct.configure(text=f"{pct:.0f} %")
        if aborted:
            self.lbl_kachel.configure(text=f"⚠  {done} / {total}  abgebrochen")
        else:
            self.lbl_kachel.configure(text=f"{done} / {total}")

    # ------------------------------------------------------------------
    # EINSTELLUNGEN LESEN / SCHREIBEN
    # ------------------------------------------------------------------

    def _collect_config(self):
        try:
            min_geb  = int(self.v_min_geb.get())
            min_wald = int(self.v_min_wald.get())
        except ValueError:
            messagebox.showerror("Eingabefehler",
                                 "Mindest-Pixel-Werte müssen ganze Zahlen sein.")
            return None

        n = self.v_nproc.get().strip().lower()
        if n in ('', 'auto'):
            nproc = cpu_count()
        else:
            try:
                nproc = int(n)
            except ValueError:
                messagebox.showerror("Eingabefehler",
                                     "CPU-Kerne muss eine ganze Zahl oder 'auto' sein.")
                return None

        date_alt = self.v_date_alt.get().strip()
        date_neu = self.v_date_neu.get().strip()

        return {
            'folder_dgm':      self.v_dgm.get().strip(),
            'folder_bdom_alt': self.v_bdom_alt.get().strip(),
            'folder_bdom_neu': self.v_bdom_neu.get().strip(),
            'output_base':     self.v_output.get().strip(),
            'calc_method':     self.v_method.get(),
            'date_alt':        date_alt,
            'date_neu':        date_neu,
            'year_alt':        extract_year_from_date(date_alt),
            'year_neu':        extract_year_from_date(date_neu),
            'min_pixels_geb':  min_geb,
            'min_pixels_wald': min_wald,
            'connectivity':    self.v_conn.get(),
            'num_processes':   nproc,
            'class_ranges_geb':  self.ed_geb.get_ranges(),
            'class_ranges_wald': self.ed_wald.get_ranges(),
            'file_conflict_policy': self.v_file_policy.get(),
            'tile_conflict_policy': self.v_tile_policy.get(),
            'write_log': self.v_write_log.get(),
        }

    def _apply_config(self, cfg):
        self.v_dgm.set(cfg.get('folder_dgm', ''))
        self.v_bdom_alt.set(cfg.get('folder_bdom_alt', ''))
        self.v_bdom_neu.set(cfg.get('folder_bdom_neu', ''))
        self.v_output.set(cfg.get('output_base', ''))
        self.v_method.set(cfg.get('calc_method', 2))
        self._on_method_change()
        self.v_date_alt.set(cfg.get('date_alt', '2020'))
        self.v_date_neu.set(cfg.get('date_neu', '2023'))
        self.v_min_geb.set(str(cfg.get('min_pixels_geb', DEFAULT_MIN_PIXELS_GEB)))
        self.v_min_wald.set(str(cfg.get('min_pixels_wald', DEFAULT_MIN_PIXELS_WALD)))
        self.v_conn.set(cfg.get('connectivity', DEFAULT_CONNECTIVITY))
        np_raw = cfg.get('num_processes', cpu_count())
        self.v_nproc.set('auto' if np_raw == cpu_count() else str(np_raw))
        self.v_file_policy.set(cfg.get('file_conflict_policy', 'overwrite_all'))
        self.v_tile_policy.set(cfg.get('tile_conflict_policy', 'recalculate_all'))
        self.v_write_log.set(cfg.get('write_log', True))

        if 'class_ranges_geb' in cfg:
            self.ed_geb.ranges = [tuple(r) for r in cfg['class_ranges_geb']]
            self.ed_geb._refresh()
        if 'class_ranges_wald' in cfg:
            self.ed_wald.ranges = [tuple(r) for r in cfg['class_ranges_wald']]
            self.ed_wald._refresh()

    def _save_settings_auto(self):
        cfg = self._collect_config()
        if cfg is None:
            return
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_settings_auto(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    self._apply_config(json.load(f))
            except Exception:
                pass

    def _save_settings_dialog(self):
        cfg = self._collect_config()
        if cfg is None:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON-Dateien", "*.json"), ("Alle Dateien", "*.*")],
            initialfile="tDOM_Filterung_Einstellungen.json",
            title="Einstellungen speichern"
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            messagebox.showinfo("Gespeichert", f"Einstellungen gespeichert:\n{path}")
        except Exception as e:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen:\n{e}")

    def _load_settings_dialog(self):
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
    # VERARBEITUNG STARTEN / STOPPEN
    # ------------------------------------------------------------------

    def _validate(self, cfg):
        if not cfg['folder_bdom_alt']:
            messagebox.showerror("Fehler", "bDOM-Alt-Ordner muss angegeben werden!"); return False
        if not cfg['folder_bdom_neu']:
            messagebox.showerror("Fehler", "bDOM-Neu-Ordner muss angegeben werden!"); return False
        if not cfg['output_base']:
            messagebox.showerror("Fehler", "Ausgabe-Ordner muss angegeben werden!"); return False
        if cfg['calc_method'] == 1 and not cfg['folder_dgm']:
            messagebox.showerror("Fehler", "Methode 1 benötigt einen DGM-Ordner!"); return False
        if not cfg['class_ranges_geb']:
            messagebox.showerror("Fehler", "Mindestens ein Gebäude-Klassenbereich erforderlich!"); return False
        if not cfg['class_ranges_wald']:
            messagebox.showerror("Fehler", "Mindestens ein Wald-Klassenbereich erforderlich!"); return False

        pfad_alt = os.path.normcase(os.path.realpath(cfg['folder_bdom_alt']))
        pfad_neu = os.path.normcase(os.path.realpath(cfg['folder_bdom_neu']))
        if pfad_alt == pfad_neu:
            messagebox.showerror(
                "Fehler – gleiche Eingabeordner",
                "bDOM Alt und bDOM Neu zeigen auf denselben Ordner!\n\n"
                f"{cfg['folder_bdom_alt']}\n\n"
                "Bitte zwei verschiedene Ordner mit Aufnahmen aus\n"
                "unterschiedlichen Zeitpunkten wählen.\n"
                "Die Differenzbildung wäre sonst 0 für alle Pixel."
            )
            return False

        return True

    def _start(self):
        if self.proc_thread and self.proc_thread.is_alive():
            messagebox.showwarning("Hinweis", "Verarbeitung läuft bereits!")
            return

        if not DEPS_OK:
            messagebox.showerror("Fehlende Bibliotheken",
                                 f"Folgende Pakete konnten nicht geladen werden:\n\n{DEPS_ERROR}\n\n"
                                 "Bitte per 'pip install' nachinstallieren.")
            return

        cfg = self._collect_config()
        if cfg is None:
            return
        if not self._validate(cfg):
            return

        self.stop_event.clear()
        self._proc_start_time = datetime.datetime.now()
        self._proc_cfg = cfg

        self.redirector = TextRedirector(self.output_text)
        sys.stdout = self.redirector
        self.redirector.start_polling()

        self.nb.select(4)

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.lbl_status.configure(text="⏳  Verarbeitung läuft …")

        self.progress_var.set(0.0)
        self.lbl_kachel.configure(text="…")
        self.lbl_pct.configure(text="0 %")

        self.proc_thread = threading.Thread(
            target=self._thread_worker, args=(cfg,), daemon=True)
        self.proc_thread.start()

    def _thread_worker(self, cfg):
        def progress_callback(done, total, aborted=False):
            self.root.after(0, self._update_progress, done, total, aborted)
        try:
            run_processing(cfg, self.stop_event, progress_callback)
        except Exception as e:
            print(f"\n✗ Unerwarteter Fehler: {e}")
            import traceback
            traceback.print_exc()
        finally:
            sys.stdout = sys.__stdout__
            self.root.after(0, self._on_done)

    def _on_done(self):
        log_text = None
        if self.redirector:
            log_text = self.redirector.get_log_text()
            self.redirector.stop()
            self.redirector = None

        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.lbl_status.configure(text="Bereit")
        if self.progress_var.get() == 0.0:
            self.lbl_kachel.configure(text="")
            self.lbl_pct.configure(text="")

        if log_text and getattr(self, "_proc_cfg", None) and self._proc_cfg.get("write_log", True):
            self._write_log_file(log_text)

    def _write_log_file(self, log_text):
        output_base = self._proc_cfg.get("output_base", "").strip()
        if not output_base:
            return
        try:
            os.makedirs(output_base, exist_ok=True)
            ts = getattr(self, "_proc_start_time", datetime.datetime.now())
            filename = f"Berechnungsprotokoll_{ts.strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = os.path.join(output_base, filename)

            trennlinie = "=" * 60
            header = (
                "Terrain-Differenz-Berechnung - Protokoll\n" +
                trennlinie + "\n" +
                f"Prozessstart  : {ts.strftime('%d.%m.%Y %H:%M:%S')}\n" +
                f"Ausgabe-Ordner: {output_base}\n" +
                trennlinie + "\n\n"
            )

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(log_text)

            sep = "=" * 60
            msg = f"\n{sep}\nProtokolldatei gespeichert:\n{filepath}\n{sep}\n"
            self.output_text.configure(state="normal")
            self.output_text.insert(tk.END, msg)
            self.output_text.see(tk.END)
            self.output_text.configure(state="disabled")

        except Exception as e:
            messagebox.showwarning("Protokoll",
                                   f"Protokolldatei konnte nicht gespeichert werden:\n{e}")

    def _stop(self):
        if self.proc_thread and self.proc_thread.is_alive():
            self.stop_event.set()
            terminate_active_pool()   # laufenden Pool sofort abbrechen
            self.lbl_status.configure(text="⚠  Abbruch angefordert …")

    # ------------------------------------------------------------------
    # SONSTIGES
    # ------------------------------------------------------------------

    def _show_anleitung(self):
        messagebox.showinfo(
            "Anleitung",
            "bDOM Veränderungsdetektion – Kurzanleitung:\n"
            "\n"
            "1. DATENEINGABE\n"
            "   • bDOM Alt / Neu: Ordner wählen\n"
            "      ↳ Programm erwartet 1KM Kacheln mit räumlicher \n"
            "         Auflösung von 0,2m\n"
            "   • Datumsfelder ausfüllen (Jahr wird als Dateikürzel genutzt)\n"
            "   • Ausgabe-Ordner festlegen\n"
            "      ↳ Programm erstellt Ausgabeordnerstruktur selbst \n\n"
            "2. Einstellung\n"
            "   • Methode 1: nutzt DGM zur Normalisierung\n"
            "      ↳ Programm erwartet 1KM Kacheln mit räumlicher \n"
            "         Auflösung von 1m\n"
            "      ↳ Erzeugt pro Kachel zusätzlich 2 auf die Erdoberfläche\n"
            "         reduzierte h0 Raster\n"
            "   • Methode 2: direkte DOM-Differenz (kein DGM nötig)\n"
            "      ↳ Standard Szenario\n"
            "      ↳ geringere Rechendauer\n"
            "   • Mindest-Pixel: Cluster kleiner als dieser Schwellwert\n"
            "     werden gefiltert\n\n"
            "3. KLASSEN (Gebäude / Wald)\n"
            "   • Wertebereiche der Höhendifferenz definieren\n"
            "   • Doppelklick zum Bearbeiten eines Bandes\n\n"
            "4. STARTEN\n"
            "   • 'Verarbeitung starten' – Fortschritt im Tab Ausgabe\n"
            "   • 'Stopp' bricht nach der laufenden Kachel ab\n\n"
            f"Einstellungen werden beim Schließen automatisch gespeichert:\n{SETTINGS_FILE}"
        )

    def _show_info(self):
        messagebox.showinfo(
            "Info",
            "bDOM Veränderungsdetektion\n"
            "Version 1.0\n\n"
            "Dieses Werkzeug berechnet kachelweise\n"
            "Höhendifferenzen zwischen Aufnahmezeitpunkten\n"
            "aus bDOM-Rasterdaten (0,2 m Auflösung).\n\n"
            "Ausgabe-Produkte je Kachel:\n"
            "  01_tDOM                                    – Höhendifferenz\n"
            "  02/03_tDOM_klass_geb/wald   – klassifizierte Bänder\n"
            "  04/05_tDOM_mask_geb/wald   – gefilterte Masken\n"
            "  06_tDOM_final                           – gefilterte Veränderungen\n\n"
            "Verarbeitung nutzt parallele CPU-Kerne\n"
            "für die Connected-Components-Filterung.\n\n"
            f"Einstellungen werden automatisch gespeichert:\n{SETTINGS_FILE}\n\n"
            "Icon: Flaticon.com"
        )

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
            "Die angewendete Methode wurde im Rahmen einer Bachelorarbeit entwickelt. "
            "Ziel war es, ein Hilfsmittel zu schaffen welches die Aktualisierung der "
            "tatsächlichen Nutzung im Liegenschaftskataster beschleunigt.\n"
            "Die Standardparameter sind daher auf diese Anwendung zugeschnitten.\n\n"
            "Daten zur Bachelorarbeit:\n\n"
            "Titel:\n"
            "Detektion von Veränderungen auf Basis des digitalen Oberflächenmodells, "
            "des Feldblockkatasters und der Forstgrundkarte mit dem Ziel einer beschleunigten "
            "Aktualisierung der tatsächlichen Nutzung im Liegenschaftskataster \n\n"
            "Autor:\n"
            "Fabian Britze\n\n"
            "Datum:\n"
            "02.03.2026\n\n"
            "Institut:\n"
            "Hochschule Anhalt Dessau - FB3 \n"
            "In Kooperation mit dem Landkreis Spree Neiße - FB 62"
        )

    def _show_lizenz(self):
        messagebox.showinfo(
            "Lizenz",
            "Dieses Programm wird in der Hoffnung verbreitet, dass es in unterschiedlichsten Anwendungen nützlich sein wird, aber OHNE JEGLICHE GEWÄHRLEISTUNG.\n\n"
            "Copyright (C) 2026  Fabian Britze\n"
            "E-Mail: fabian.britze@gmail.com\n\n"
            "Lizenz: \n"
            "GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007\n\n"
            "DOI: \n"
            "https://doi.org/10.5281/zenodo.19392546\n\n"
            "Repository: \n"
            "https://github.com/Fabritzfoto/change_detection_image-based-DSM_difference"
        )

    def _on_close(self):
        if self.proc_thread and self.proc_thread.is_alive():
            if not messagebox.askyesno("Beenden",
                                       "Eine Verarbeitung läuft noch.\n"
                                       "Wirklich beenden?"):
                return
            self.stop_event.set()
            terminate_active_pool()   # Hintergrundprozesse sofort beenden
        self._save_settings_auto()
        self.root.destroy()

    def _download_bachelorarbeit(self):
        """Speichert die eingebettete Bachelorarbeit als PDF"""
        import shutil

        pdf_src = resource_path("Bachelorarbeit_Fabian_Britze_mitNachtrag.pdf")

        if not os.path.exists(pdf_src):
            messagebox.showerror(
                "Datei nicht gefunden",
                "Die PDF-Datei konnte nicht gefunden werden.\n"
                f"Erwartet unter:\n{pdf_src}"
            )
            return

        ziel = filedialog.asksaveasfilename(
            title="Bachelorarbeit speichern",
            defaultextension=".pdf",
            filetypes=[("PDF-Dateien", "*.pdf")],
            initialfile="Bachelorarbeit_Fabian_Britze_mitNachtrag.pdf"
        )

        if not ziel:
            return  # Nutzer hat abgebrochen

        try:
            shutil.copy2(pdf_src, ziel)
            messagebox.showinfo(
                "Gespeichert",
                f"PDF erfolgreich gespeichert:\n{ziel}"
            )
        except Exception as e:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen:\n{e}")


# ============================================================================
# EINSTIEGSPUNKT
# ============================================================================

def main():
    freeze_support()

    root = tk.Tk()

    style = ttk.Style()
    for theme in ('vista', 'winnative', 'aqua', 'clam', 'alt', 'default'):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue

    app = TerrainDiffApp(root)
    if not DEPS_OK:
        messagebox.showerror("Fehlende Abhängigkeiten",
                             f"Kritische Bibliotheken fehlen:\n{DEPS_ERROR}\n\n"
                             "Bitte 'pip install numpy rasterio scipy' ausführen.")

    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
