"""
LICENSE:
The program calculates the difference between two image-based digital surface models and 
filters out the resulting errors. The script is divided into a building filter and a 
forest filter, which filter different elevation ranges with different parameters.
Copyright (©) 2026  Fabian Britze
E-Mail: fabian.britze@gmail.com

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

Dieses Programm ist freie Software: Sie können es weitergeben und/oder verändern
unter den Bedingungen der GNU General Public License Version 3, wie sie von der 
Free Software Foundation veröffentlicht wurde.
>> Weitere Informationen finden Sie unter <https://www.gnu.org/licenses/>.

-----------------------------------------------------------------------------------------------------------------------------------------

Terrain-Differenz-Berechnung für bDOM und DGM Daten (Batch-Verarbeitung)
Mit separaten Gebäude- und Wald-Klassen und parallelisierter Connected Components Filterung
"""

import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.windows import from_bounds
import numpy as np
import os
import re
import glob
from pathlib import Path
from scipy import ndimage
from scipy.ndimage import label
from multiprocessing import Pool, cpu_count
from functools import partial
import datetime

# ============================================================================
# HARDCODE EINSTELLUNGEN - HIER ANPASSEN
# ============================================================================

# Input Ordner
FOLDER_DGM = r"C:\Users\...\Roh Daten\DGM_2011_04_21"
FOLDER_BDOM_ALT = r"C:\Users\...\Roh Daten\bDOM_2020_04_11"
FOLDER_BDOM_NEU = r"C:\Users\...\Roh Daten\bDOM_2023_04_21"

# Output Basis-Ordner (Unterordner werden automatisch erstellt)
OUTPUT_BASE_FOLDER = r"C:\Users\...\temp_auto_test"

# Berechnungsmethode
CALCULATION_METHOD = 2  # 1 = (DOM_n - DGM) - (DOM_a - DGM), 2 = DOM_n - DOM_a

# Datumsangaben für Dateinamen (Format: DD.MM.YYYY oder nur YYYY)
DATE_DOM_ALT = "2020"
DATE_DOM_NEU = "2023"

# Parameter für Connected Components Filterung
MIN_PIXELS_GEBAEUDE = 1000  # Für Gebäude-Klassen
MIN_PIXELS_WALD = 5000       # Für Wald-Klassen
CONNECTIVITY = 8

# Parallelisierung (Anzahl CPU-Kerne, None = alle verfügbaren)
NUM_PROCESSES = None  # None = automatisch (cpu_count()), oder z.B. 4

# ============================================================================
# KLASSENEINTEILUNG - HIER ANPASSEN
# ============================================================================

# Gebäude-Klassen: 22 Bands mit 2m Fensterbreite, 1m Versatz
# Format: (min_wert, max_wert) in Metern
CLASS_RANGES_GEBAEUDE = [
    (-14, -12),  # Band 1
    (-13, -11),  # Band 2
    (-12, -10),  # Band 3
    (-11, -9),   # Band 4
    (-10, -8),   # Band 5
    (-9, -7),    # Band 6
    (-8, -6),    # Band 7
    (-7, -5),    # Band 8
    (-6, -4),    # Band 9
    (-5, -3),    # Band 10
    (-4, -2),    # Band 11
    (2, 4),      # Band 12
    (3, 5),      # Band 13
    (4, 6),      # Band 14
    (5, 7),      # Band 15
    (6, 8),      # Band 16
    (7, 9),      # Band 17
    (8, 10),     # Band 18
    (9, 11),     # Band 19
    (10, 12),    # Band 20
    (11, 13),    # Band 21
    (12, 14)     # Band 22
]

# Wald-Klassen: 7 Bands mit 4m Fensterbreite, 2m Versatz
CLASS_RANGES_WALD = [
    (-30, -26),  # Band 1
    (-28, -24),  # Band 2
    (-26, -22),  # Band 3
    (-24, -20),  # Band 4
    (-22, -18),  # Band 5
    (-20, -16),  # Band 6
    (-18, -14)   # Band 7
]

# ============================================================================
# GLOBALE VARIABLEN
# ============================================================================

file_conflict_policy = None
tile_conflict_policy = None


def extract_year_from_date(date_str):
    """Extrahiert Jahr aus Datumsstring"""
    year_match = re.search(r'(\d{4})', date_str)
    if year_match:
        return year_match.group(1)[-2:]
    return "XX"


YEAR_ALT = extract_year_from_date(DATE_DOM_ALT)
YEAR_NEU = extract_year_from_date(DATE_DOM_NEU)


# ============================================================================
# DATEIMANAGEMENT
# ============================================================================

def setup_output_folders(base_folder, calc_method):
    """Erstellt Ausgabeordner-Struktur"""
    folders = {
        'hdiff': os.path.join(base_folder, 'hdiff'),
        'hdiff_klass_gebaeude': os.path.join(base_folder, 'hdiff_klass_gebaeude'),
        'hdiff_klass_wald': os.path.join(base_folder, 'hdiff_klass_wald'),
        'mask_gebaeude': os.path.join(base_folder, 'hdiff_mask_gebaeude'),
        'mask_wald': os.path.join(base_folder, 'hdiff_mask_wald'),
        'hdiff_final': os.path.join(base_folder, 'hdiff_final'),
        'temp': base_folder
    }

    if calc_method == 1:
        folders['h0_alt'] = os.path.join(base_folder, 'h0_DOM_alt')
        folders['h0_neu'] = os.path.join(base_folder, 'h0_DOM_neu')

    for folder in folders.values():
        os.makedirs(folder, exist_ok=True)

    return folders


def check_file_exists_and_ask(filepath, step_name):
    """Prüft ob Datei existiert und fragt Benutzer"""
    global file_conflict_policy

    if not os.path.exists(filepath):
        return True

    if file_conflict_policy == 'overwrite_all':
        print(f"        → Datei existiert, wird überschrieben (globale Einstellung)")
        return True
    elif file_conflict_policy == 'skip_all':
        print(f"        → Datei existiert, wird übersprungen (globale Einstellung)")
        return False

    filename = os.path.basename(filepath)
    print(f"\n        ⚠ KONFLIKT: {filename}")
    print(f"        1-Überschreiben | 2-Überspringen | 3-Alle überschreiben | 4-Alle überspringen")

    while True:
        choice = input(f"        Wahl [1-4]: ").strip()
        if choice == '1':
            return True
        elif choice == '2':
            return False
        elif choice == '3':
            file_conflict_policy = 'overwrite_all'
            return True
        elif choice == '4':
            file_conflict_policy = 'skip_all'
            return False


def check_complete_tileset_exists(key, output_folders, calc_method):
    """Prüft ob vollständiges Tileset existiert"""
    files_to_check = [
        os.path.join(output_folders['hdiff'], f"hdiff_{YEAR_ALT}_{YEAR_NEU}_{key}.tif"),
        os.path.join(output_folders['hdiff_klass_gebaeude'], f"hdiff_klass_geb_{YEAR_ALT}_{YEAR_NEU}_{key}.tif"),
        os.path.join(output_folders['hdiff_klass_wald'], f"hdiff_klass_wald_{YEAR_ALT}_{YEAR_NEU}_{key}.tif"),
        os.path.join(output_folders['mask_gebaeude'], f"mask_geb_{YEAR_ALT}_{YEAR_NEU}_{key}.tif"),
        os.path.join(output_folders['mask_wald'], f"mask_wald_{YEAR_ALT}_{YEAR_NEU}_{key}.tif"),
        os.path.join(output_folders['hdiff_final'], f"hdiff_{YEAR_ALT}_{YEAR_NEU}_33{key}.tif")
    ]

    if calc_method == 1:
        files_to_check.extend([
            os.path.join(output_folders['h0_alt'], f"h0_DOM_{YEAR_ALT}_{key}.tif"),
            os.path.join(output_folders['h0_neu'], f"h0_DOM_{YEAR_NEU}_{key}.tif")
        ])

    return all(os.path.exists(f) for f in files_to_check)


def ask_tileset_action(key):
    """Fragt Benutzer was mit existierendem Tileset zu tun ist"""
    global tile_conflict_policy

    if tile_conflict_policy == 'recalculate_all':
        print(f"  → Vollständiges Set existiert, wird neu berechnet")
        return True
    elif tile_conflict_policy == 'use_all':
        print(f"  → Vollständiges Set existiert, wird übersprungen")
        return False

    print(f"\n  ⚠ VOLLSTÄNDIGES DATA SET für Kachel: {key}")
    print(f"  1-Neu berechnen | 2-Überspringen | 3-Alle neu | 4-Alle überspringen")

    while True:
        choice = input(f"  Wahl [1-4]: ").strip()
        if choice == '1':
            return True
        elif choice == '2':
            return False
        elif choice == '3':
            tile_conflict_policy = 'recalculate_all'
            return True
        elif choice == '4':
            tile_conflict_policy = 'use_all'
            return False


# ============================================================================
# GEOMETRISCHE VERARBEITUNG
# ============================================================================

def extract_tile_key(filename):
    """Extrahiert Kachel-Schlüssel"""
    match = re.search(r'(\d{3})000-(\d{4})000', filename)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


def find_matching_files(dgm_folder, bdom_alt_folder, bdom_neu_folder, calc_method):
    """Findet zusammengehörige Datei-Sets"""
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
                print(f"  → Keine DGM-Kacheln gefunden, suche nach großem DGM-Raster...")
                largest_file = max(dgm_tifs, key=os.path.getsize)
                large_dgm_file = largest_file
                print(f"  → Großes DGM-Raster gefunden: {os.path.basename(largest_file)}")
                print(f"     Größe: {os.path.getsize(largest_file) / (1024**2):.1f} MB")
            else:
                print(f"  → DGM: {len(dgm_files)} Einzelkacheln gefunden")

    for filepath in glob.glob(os.path.join(bdom_alt_folder, "*.tif")):
        key = extract_tile_key(os.path.basename(filepath))
        if key:
            bdom_alt_files[key] = filepath

    for filepath in glob.glob(os.path.join(bdom_neu_folder, "*.tif")):
        key = extract_tile_key(os.path.basename(filepath))
        if key:
            bdom_neu_files[key] = filepath

    print(f"  → BDOM_ALT: {len(bdom_alt_files)} Dateien")
    print(f"  → BDOM_NEU: {len(bdom_neu_files)} Dateien")

    if calc_method == 1:
        if large_dgm_file:
            all_keys = set(bdom_alt_files.keys()) & set(bdom_neu_files.keys())
            print(f"  → Verwende großes DGM für alle {len(all_keys)} BDOM-Kacheln")
        else:
            all_keys = set(dgm_files.keys()) & set(bdom_alt_files.keys()) & set(bdom_neu_files.keys())
    else:
        all_keys = set(bdom_alt_files.keys()) & set(bdom_neu_files.keys())

    matching_sets = []
    for key in sorted(all_keys):
        tile_set = {
            'key': key,
            'bdom_alt': bdom_alt_files[key],
            'bdom_neu': bdom_neu_files[key]
        }
        if calc_method == 1:
            if large_dgm_file:
                tile_set['dgm'] = large_dgm_file
                tile_set['use_large_dgm'] = True
            else:
                tile_set['dgm'] = dgm_files[key]
                tile_set['use_large_dgm'] = False
        matching_sets.append(tile_set)

    print(f"  ✓ {len(matching_sets)} vollständige Kachel-Sets gefunden")
    return matching_sets


def extract_tile_from_large_dgm(large_dgm_path, tile_key, output_path):
    """Extrahiert eine 1km-Kachel aus großem DGM"""
    parts = tile_key.split('-')
    if len(parts) != 2:
        raise ValueError(f"Ungültiger Kachel-Schlüssel: {tile_key}")

    x_min = int(parts[0]) * 1000
    y_min = int(parts[1]) * 1000
    x_max = x_min + 1000
    y_max = y_min + 1000

    print(f"        Extrahiere: X={x_min}-{x_max}m, Y={y_min}-{y_max}m")

    with rasterio.open(large_dgm_path) as src:
        window = from_bounds(x_min, y_min, x_max, y_max, src.transform)
        data = src.read(1, window=window)
        print(f"        Extrahierte Größe: {data.shape[1]}x{data.shape[0]} Pixel")

        window_transform = src.window_transform(window)
        meta = src.meta.copy()
        meta.update({
            'height': data.shape[0],
            'width': data.shape[1],
            'transform': window_transform
        })

        with rasterio.open(output_path, 'w', **meta) as dst:
            dst.write(data, 1)

    return output_path


def crop_dgm_to_1000x1000(input_path, output_path):
    """Schneidet DGM auf 1000x1000"""
    with rasterio.open(input_path) as src:
        data = src.read(1)[1:, :-1]
        transform = src.transform
        new_transform = rasterio.Affine(
            transform.a, transform.b, transform.c,
            transform.d, transform.e, transform.f - transform.e
        )
        meta = src.meta.copy()
        meta.update({'height': 1000, 'width': 1000, 'transform': new_transform})
        with rasterio.open(output_path, 'w', **meta) as dst:
            dst.write(data, 1)
    return output_path


def resample_to_02m(input_path, output_path, target_resolution=0.2):
    """Resampelt DGM auf 0.2m"""
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
            source=rasterio.band(src, 1),
            destination=resampled_data,
            src_transform=old_transform,
            src_crs=src.crs,
            dst_transform=new_transform,
            dst_crs=src.crs,
            resampling=Resampling.bilinear
        )
        with rasterio.open(output_path, 'w', **meta) as dst:
            dst.write(resampled_data, 1)
    return output_path


def subtract_rasters(minuend_path, subtrahend_path, output_path):
    """Pixelweise Subtraktion"""
    with rasterio.open(minuend_path) as src1:
        with rasterio.open(subtrahend_path) as src2:
            if src1.shape != src2.shape:
                raise ValueError(f"Dimensionen stimmen nicht überein")
            data1 = src1.read(1).astype(np.float32)
            data2 = src2.read(1).astype(np.float32)
            result = data1 - data2
            meta = src1.meta.copy()
            meta.update({'dtype': 'float32'})
            with rasterio.open(output_path, 'w', **meta) as dst:
                dst.write(result, 1)
    return output_path


# ============================================================================
# KLASSIFIZIERUNG
# ============================================================================

def klassifiziere_hoehen_multiband(input_raster, output_raster, class_ranges):
    """Klassifiziert Höhenwerte in Multi-Band Raster"""
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

        meta.update({
            'dtype': 'uint8',
            'count': num_bands,
            'nodata': 0,
            'driver': 'GTiff',
            'compress': 'lzw'
        })

        with rasterio.open(output_raster, 'w', **meta) as dst:
            for band_idx in range(num_bands):
                dst.write(classified[band_idx], band_idx + 1)

        stats = {}
        for band_idx, (min_val, max_val) in enumerate(class_ranges):
            pixel_count = np.sum(classified[band_idx] == 1)
            stats[f"Band {band_idx + 1} ({min_val} bis {max_val}m)"] = int(pixel_count)

    return stats


# ============================================================================
# PARALLELISIERTE CONNECTED COMPONENTS FILTERUNG
# ============================================================================

def process_single_band(args):
    """Verarbeitet ein einzelnes Band (für Parallelisierung)"""
    band_idx, data, min_pixels, connectivity = args

    valid_mask = data == 1
    if np.sum(valid_mask) == 0:
        return band_idx, data.copy(), 0, 0, 0

    structure = ndimage.generate_binary_structure(2, 1 if connectivity == 4 else 2)
    labeled_array, num_features = label(valid_mask, structure=structure)
    region_sizes = ndimage.sum(valid_mask, labeled_array, range(1, num_features + 1))
    small_regions = np.where(region_sizes < min_pixels)[0] + 1

    filtered_band = data.copy()
    removed_regions = 0
    removed_pixels = 0

    if len(small_regions) > 0:
        removed_pixels = int(np.sum(region_sizes[small_regions - 1]))
        removed_regions = len(small_regions)
        for region_id in small_regions:
            mask = labeled_array == region_id
            filtered_band[mask] = 0

    return band_idx, filtered_band, num_features, removed_regions, removed_pixels


def filter_small_regions_multiband_parallel(input_raster, output_raster, min_pixels=500, connectivity=8,
                                            num_processes=None):
    """Filtert kleine Regionen parallel über alle Bands"""
    with rasterio.open(input_raster) as src:
        num_bands = src.count
        profile = src.profile.copy()
        all_bands_data = [src.read(band_idx + 1) for band_idx in range(num_bands)]

    args_list = [(band_idx, all_bands_data[band_idx], min_pixels, connectivity)
                 for band_idx in range(num_bands)]

    if num_processes is None:
        num_processes = min(cpu_count(), num_bands)

    with Pool(processes=num_processes) as pool:
        results = pool.map(process_single_band, args_list)

    filtered_data = np.zeros((num_bands, all_bands_data[0].shape[0], all_bands_data[0].shape[1]), dtype=np.uint8)
    total_found_regions = 0
    total_removed_regions = 0
    total_removed_pixels = 0

    for band_idx, filtered_band, num_features, removed_regions, removed_pixels in results:
        filtered_data[band_idx] = filtered_band
        total_found_regions += num_features
        total_removed_regions += removed_regions
        total_removed_pixels += removed_pixels

    with rasterio.open(output_raster, 'w', **profile) as dst:
        for band_idx in range(num_bands):
            dst.write(filtered_data[band_idx], band_idx + 1)

    return total_found_regions, total_removed_regions, total_removed_pixels


# ============================================================================
# MASKEN-EXTRAKTION
# ============================================================================

def extract_with_combined_masks(original_raster, mask_gebaeude, mask_wald, output_raster):
    """Extrahiert Pixel basierend auf kombinierten Masken"""
    with rasterio.open(original_raster) as src_orig:
        original_data = src_orig.read(1)
        profile = src_orig.profile.copy()
        original_nodata = src_orig.nodata

    combined_mask = np.zeros((original_data.shape[0], original_data.shape[1]), dtype=bool)

    with rasterio.open(mask_gebaeude) as src_geb:
        for band_idx in range(src_geb.count):
            band_data = src_geb.read(band_idx + 1)
            combined_mask |= (band_data == 1)

    with rasterio.open(mask_wald) as src_wald:
        for band_idx in range(src_wald.count):
            band_data = src_wald.read(band_idx + 1)
            combined_mask |= (band_data == 1)

    num_extracted = np.sum(combined_mask)
    percentage = (num_extracted / combined_mask.size) * 100

    output_data = np.full_like(original_data, original_nodata if original_nodata is not None else np.nan)
    output_data[combined_mask] = original_data[combined_mask]

    extracted_values = original_data[combined_mask]
    stats = {
        'num_extracted': int(num_extracted),
        'percentage': percentage,
        'area_m2': num_extracted * 0.2 * 0.2,
        'min': float(np.min(extracted_values)) if len(extracted_values) > 0 else 0,
        'max': float(np.max(extracted_values)) if len(extracted_values) > 0 else 0,
        'mean': float(np.mean(extracted_values)) if len(extracted_values) > 0 else 0
    }

    if original_nodata is not None:
        profile['nodata'] = original_nodata

    with rasterio.open(output_raster, 'w', **profile) as dst:
        dst.write(output_data, 1)

    return stats


# ============================================================================
# HAUPTVERARBEITUNG
# ============================================================================

def process_tile(tile_set, output_folders, calc_method, min_pixels_geb, min_pixels_wald, connectivity, num_processes):
    """Verarbeitet ein einzelnes Kachel-Set"""
    key = tile_set['key']
    print(f"\n{'=' * 70}")
    print(f"Verarbeite Kachel: {key}")
    print(f"{'=' * 70}")

    time_start = datetime.datetime.now()
    time = time_start.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Prozessstart: {time}")

    if check_complete_tileset_exists(key, output_folders, calc_method):
        if not ask_tileset_action(key):
            print(f"  ✓ Kachel {key} übersprungen")
            return True

    # Dateipfade
    temp_files = []
    output_hdiff = os.path.join(output_folders['hdiff'], f"hdiff_{YEAR_ALT}_{YEAR_NEU}_{key}.tif")
    output_klass_geb = os.path.join(output_folders['hdiff_klass_gebaeude'],
                                    f"hdiff_klass_geb_{YEAR_ALT}_{YEAR_NEU}_{key}.tif")
    output_klass_wald = os.path.join(output_folders['hdiff_klass_wald'],
                                     f"hdiff_klass_wald_{YEAR_ALT}_{YEAR_NEU}_{key}.tif")
    output_mask_geb = os.path.join(output_folders['mask_gebaeude'], f"mask_geb_{YEAR_ALT}_{YEAR_NEU}_{key}.tif")
    output_mask_wald = os.path.join(output_folders['mask_wald'], f"mask_wald_{YEAR_ALT}_{YEAR_NEU}_{key}.tif")
    output_final = os.path.join(output_folders['hdiff_final'], f"hdiff_{YEAR_ALT}_{YEAR_NEU}_33{key}.tif")

    recalculate_from_step = 0
    if calc_method == 1:
        steps = 9
    else:
        steps = 6

    try:
        if calc_method == 1:
            # Methode 1 mit DGM
            temp_dgm_tile = os.path.join(output_folders['temp'], f"DGM_{key}_tile.tif")
            temp_dgm_1000 = os.path.join(output_folders['temp'], f"DGM_{key}_1000.tif")
            temp_dgm_20cm = os.path.join(output_folders['temp'], f"DGM_{key}_20cm.tif")
            output_h0_alt = os.path.join(output_folders['h0_alt'], f"h0_DOM_{YEAR_ALT}_{key}.tif")
            output_h0_neu = os.path.join(output_folders['h0_neu'], f"h0_DOM_{YEAR_NEU}_{key}.tif")

            if tile_set.get('use_large_dgm', False):
                print("  [1/9] Extrahiere DGM-Kachel aus großem Raster...")
                extract_tile_from_large_dgm(tile_set['dgm'], key, temp_dgm_tile)
                temp_files.append(temp_dgm_tile)
                dgm_to_crop = temp_dgm_tile
                print("  [2/9] Croppe DGM auf 1000x1000...")
                crop_dgm_to_1000x1000(dgm_to_crop, temp_dgm_1000)
                temp_files.append(temp_dgm_1000)
            else:
                print("  [1/9] Croppe DGM auf 1000x1000...")
                crop_dgm_to_1000x1000(tile_set['dgm'], temp_dgm_1000)
                temp_files.append(temp_dgm_1000)

            print("  [3/9] Resample DGM auf 0.2m...")
            resample_to_02m(temp_dgm_1000, temp_dgm_20cm)
            temp_files.append(temp_dgm_20cm)

            print("  [4/9] Berechne h0_DOM_alt...")
            if check_file_exists_and_ask(output_h0_alt, "h0_DOM_alt"):
                subtract_rasters(tile_set['bdom_alt'], temp_dgm_20cm, output_h0_alt)
                if recalculate_from_step == 0:
                    recalculate_from_step = 6

            print("  [5/9] Berechne h0_DOM_neu...")
            if check_file_exists_and_ask(output_h0_neu, "h0_DOM_neu"):
                subtract_rasters(tile_set['bdom_neu'], temp_dgm_20cm, output_h0_neu)
                if recalculate_from_step == 0:
                    recalculate_from_step = 6

            print("  [6/9] Berechne hdiff...")
            force_recalc = recalculate_from_step > 0 and recalculate_from_step <= 6
            if force_recalc or check_file_exists_and_ask(output_hdiff, "hdiff"):
                subtract_rasters(output_h0_neu, output_h0_alt, output_hdiff)
                if recalculate_from_step == 0:
                    recalculate_from_step = 7
            step_offset = 6
        else:
            # Methode 2 direkt
            print("  [1/6] Berechne hdiff (DOM_neu - DOM_alt)...")
            if check_file_exists_and_ask(output_hdiff, "hdiff"):
                subtract_rasters(tile_set['bdom_neu'], tile_set['bdom_alt'], output_hdiff)
                if recalculate_from_step == 0:
                    recalculate_from_step = 2
            step_offset = 1

        # Klassifizierung Gebäude - NUTZT JETZT CLASS_RANGES_GEBAEUDE
        print(f"  [{step_offset + 1}/{steps}] Klassifiziere Gebäude-Klassen ({len(CLASS_RANGES_GEBAEUDE)} Bands)...")
        force_recalc = recalculate_from_step > 0 and recalculate_from_step <= step_offset + 1
        if force_recalc or check_file_exists_and_ask(output_klass_geb, "klass_gebaeude"):
            stats_geb = klassifiziere_hoehen_multiband(output_hdiff, output_klass_geb, CLASS_RANGES_GEBAEUDE)
            print(f"        Gebäude: {sum(stats_geb.values()):,} Pixel klassifiziert")
            if recalculate_from_step == 0:
                recalculate_from_step = step_offset + 2

        # Klassifizierung Wald - NUTZT JETZT CLASS_RANGES_WALD
        print(f"  [{step_offset + 2}/{steps}] Klassifiziere Wald-Klassen ({len(CLASS_RANGES_WALD)} Bands)...")
        force_recalc = recalculate_from_step > 0 and recalculate_from_step <= step_offset + 2
        if force_recalc or check_file_exists_and_ask(output_klass_wald, "klass_wald"):
            stats_wald = klassifiziere_hoehen_multiband(output_hdiff, output_klass_wald, CLASS_RANGES_WALD)
            print(f"        Wald: {sum(stats_wald.values()):,} Pixel klassifiziert")
            if recalculate_from_step == 0:
                recalculate_from_step = step_offset + 3

        # Filterung Gebäude (parallel)
        print(f"  [{step_offset + 3}/{steps}] Filtere Gebäude (min. {min_pixels_geb} Pixel, {num_processes} Kerne)...")
        force_recalc = recalculate_from_step > 0 and recalculate_from_step <= step_offset + 3
        if force_recalc or check_file_exists_and_ask(output_mask_geb, "mask_gebaeude"):
            total_found, removed_regions, removed_pixels = filter_small_regions_multiband_parallel(
                output_klass_geb, output_mask_geb, min_pixels_geb, connectivity, num_processes
            )
            print(f"        Gefunden: {total_found} | Entfernt: {removed_regions} | Ergebnis: {total_found - removed_regions} Regionen")
            if recalculate_from_step == 0:
                recalculate_from_step = step_offset + 4

        # Filterung Wald (parallel)
        print(f"  [{step_offset + 4}/{steps}] Filtere Wald (min. {min_pixels_wald} Pixel, {num_processes} Kerne)...")
        force_recalc = recalculate_from_step > 0 and recalculate_from_step <= step_offset + 4
        if force_recalc or check_file_exists_and_ask(output_mask_wald, "mask_wald"):
            total_found, removed_regions, removed_pixels = filter_small_regions_multiband_parallel(
                output_klass_wald, output_mask_wald, min_pixels_wald, connectivity, num_processes
            )
            print(f"        Gefunden: {total_found} | Entfernt: {removed_regions} | Ergebnis: {total_found - removed_regions} Regionen")
            if recalculate_from_step == 0:
                recalculate_from_step = step_offset + 5

        # Finale Extraktion
        print(f"  [{step_offset + 5}/{steps}] Extrahiere finale Höhendifferenzen...")
        force_recalc = recalculate_from_step > 0 and recalculate_from_step <= step_offset + 5
        if force_recalc or check_file_exists_and_ask(output_final, "hdiff_final"):
            stats = extract_with_combined_masks(output_hdiff, output_mask_geb, output_mask_wald, output_final)
            print(f"        Extrahiert: {stats['num_extracted']:,} Pixel ({stats['percentage']:.2f}%, {stats['area_m2']:.2f} m²)")

        # Cleanup
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        print(f"  ✓ Kachel {key} erfolgreich verarbeitet")

        time_ende = datetime.datetime.now()
        time = time_ende.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\nProzessende: {time}")
        differenz = time_ende - time_start
        print("Prozesszeit: " + str(differenz).split('.')[0])

        return True

    except Exception as e:
        print(f"  ✗ FEHLER bei Kachel {key}: {str(e)}")

        time_ende = datetime.datetime.now()
        time = time_ende.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\nFehlerzeitpunkt: {time}")
        differenz = time_ende - time_start
        print("Prozesszeit: " + str(differenz).split('.')[0])

        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except:
                pass
        return False


def main():
    global file_conflict_policy, tile_conflict_policy

    num_proc = NUM_PROCESSES if NUM_PROCESSES else cpu_count()

    print("=" * 80)
    print("TERRAIN DIFFERENZ BERECHNUNG - PARALLELISIERT")
    print("=" * 80)
    global_start = datetime.datetime.now()
    time = global_start.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Programmstart: {time}")
    print(f"Berechnungsmethode: {CALCULATION_METHOD}")
    if CALCULATION_METHOD == 1:
        print("  → (DOM_neu - DGM) - (DOM_alt - DGM)")
    else:
        print("  → DOM_neu - DOM_alt (direkt)")
    print(f"Datumsangaben: {DATE_DOM_ALT} → {DATE_DOM_NEU}")
    print(f"Dateinamen-Präfix: {YEAR_ALT}_{YEAR_NEU}")
    print(f"Gebäude: min. {MIN_PIXELS_GEBAEUDE} Pixel | {len(CLASS_RANGES_GEBAEUDE)} Klassen")
    print(f"Wald: min. {MIN_PIXELS_WALD} Pixel | {len(CLASS_RANGES_WALD)} Klassen")
    print(f"Parallelisierung: {num_proc} CPU-Kerne für Connected Components")

    output_folders = setup_output_folders(OUTPUT_BASE_FOLDER, CALCULATION_METHOD)
    print(f"\nOutput-Basis-Ordner: {OUTPUT_BASE_FOLDER}")
    print(f"  → Unterordner: hdiff, hdiff_klass_gebaeude, hdiff_klass_wald, hdiff_mask_gebaeude, hdiff_mask_wald, hdiff_final")

    matching_sets = find_matching_files(FOLDER_DGM, FOLDER_BDOM_ALT, FOLDER_BDOM_NEU, CALCULATION_METHOD)

    if not matching_sets:
        print("\n✗ Keine vollständigen Kachel-Sets gefunden!")
        return

    print(f"\nVerarbeite {len(matching_sets)} Kacheln...\n")

    successful = 0
    failed = 0

    for i, tile_set in enumerate(matching_sets, 1):
        print(f"\nKachel {i}/{len(matching_sets)}")
        if process_tile(tile_set, output_folders, CALCULATION_METHOD,
                        MIN_PIXELS_GEBAEUDE, MIN_PIXELS_WALD, CONNECTIVITY, num_proc):
            successful += 1
        else:
            failed += 1

    print("\n" + "=" * 80)
    print("VERARBEITUNG ABGESCHLOSSEN")
    print("=" * 80)
    print(f"Erfolgreich: {successful}/{len(matching_sets)}")
    if failed > 0:
        print(f"Fehlgeschlagen: {failed}/{len(matching_sets)}")

    global_ende = datetime.datetime.now()
    time = global_ende.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nProgrammende: {time}")
    differenz = global_ende - global_start
    print("Prozesszeit gesamt: " + str(differenz).split('.')[0])


if __name__ == "__main__":
    main()
