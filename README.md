# Veraenderungsdetektion-mit-bDOM-Forstgrundkarte-und-Feldblockkataster

# bDOM Change Detection Tool

### Filtering-based detection of land use changes using digital surface models

This project implements a Python-based workflow for detecting potential land use changes based on multi-temporal **digital surface models (bDOM)**.
It was developed as part of a bachelor thesis in the field of geoinformatics and cadastral management.

---

## 📖 Background

Updating land use information in the cadastral system is a **time-consuming manual process**.
Large areas must be visually inspected using aerial imagery, which leads to high workload and delayed updates.

This project provides a **semi-automated approach** to identify potential changes using height differences derived from bDOM data.

The goal is not full automation, but:

> **efficient pre-filtering of relevant change areas for further manual inspection**

---

## ⚙️ Methodology

The implemented workflow follows a structured multi-step approach:

### 1. tDOM Calculation (Height Difference)

Two temporal surface models are subtracted pixel-wise to generate a **height difference model (tDOM)**. 

---

### 2. Multi-band Height Classification

The tDOM is split into multiple overlapping height classes:

* **Buildings & low vegetation:** −14 m to +14 m
* **Forest changes:** −30 m to −14 m 

Each class is stored as a binary raster.

---

### 3. Cluster-based Filtering

Connected components are detected within each class:

* small clusters → removed (noise, vegetation artifacts)
* large clusters → preserved (likely real changes)

This step is crucial, as:

> vegetation changes create fragmented patterns, while buildings form compact clusters 

---

### 4. Parallel Processing

Filtering is performed in parallel for improved performance using multiprocessing.

---

### 5. Final Mask Extraction

Filtered building and forest masks are combined to generate the final output raster containing only relevant change areas.

---

## 🖥️ Features

* Automated tDOM calculation
* Multi-band classification of height differences
* Parallelized cluster filtering
* Separation of building and forest changes
* GUI-based workflow for practical usage
* GeoTIFF output (0.2 m resolution, float32)

---

## 🧰 Requirements

Python 3.x

Required libraries:

* numpy
* rasterio
* scipy
* tkinter

Install via pip:

```bash
pip install numpy rasterio scipy
```

---

## ▶️ Usage

### Run GUI:

```bash
python bDOM_Filterung_final.py
```

The graphical interface allows:

* selection of input datasets (bDOM, DGM)
* configuration of filter parameters
* execution of the full processing pipeline

---

## 📂 Output

The tool generates structured output folders:

* `01_tDOM` – height difference raster
* `02/03_classification` – classified height ranges
* `04/05_masks` – filtered masks
* `06_tDOM_final` – final filtered result

Each output represents **potential land use changes**.

---

## 📊 Results

The method significantly reduces the area that must be manually inspected:

* strong reduction of irrelevant vegetation changes
* high detection quality for buildings
* efficient support for cadastral updating processes

---

## ⚠️ Limitations

* not fully automated – manual validation still required
* sensitive to vegetation and seasonal effects
* parameter tuning required for different regions

---

## 🎓 Scientific Context

Developed as part of a bachelor thesis:

**“Detection of changes based on digital surface models, field block cadastre and forest base map for accelerated updating of land use in the cadastral system”**

---

## 📜 License

This project is licensed under the
GNU General Public License (GPL v3).

---

## 👤 Author

Fabian Britze

---

## 📌 Citation

If you use this project, please cite it via the provided DOI (see Zenodo release).
