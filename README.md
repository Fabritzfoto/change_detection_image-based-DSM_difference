# change detection - image-based-DSM difference

> [!NOTE]
> 🇩🇪 [Zur deutschen Version springen](#veränderungsdetektion-auf-basis-vpn-bdom-differenz-tdom-mit-filterung)
---

# Change detection based on bDOM difference (tDOM) with filtering

### Detection of land use changes using filtered digital surface models

This program implements a Python-based workflow for detecting potential land use changes based on **digital surface models (bDOM)**.
The development was carried out as part of a bachelor thesis in the study program Surveying and Geoinformatics at Anhalt University of Applied Sciences (Dessau).

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19392547.svg)](https://doi.org/10.5281/zenodo.19392547)

---

## 📖 Background

Updating land use (TN) in the cadastral system is a **time-consuming manual process**.
Large areas must be visually inspected using aerial imagery, which leads to high workload and delayed updates.

This bachelor thesis therefore follows a **semi-automated approach** to identify potential changes based on height differences.
The goal of this work was not full automation, but an efficient pre-filtering of relevant change areas to support cadastral operators.

For this purpose, three data sources are used:

* digital forest base map (processed in ArcGIS)
* digital field block cadastre (processed in ArcGIS)
* image-based digital surface model (bDOM) (processed in Python)

---

## ⚙️ Methodology of bDOM filtering (see bachelor thesis)

(Script: `Core_bDOM_fitering_algorithm`)

The workflow is based on a multi-stage filtering approach:

### 1. tDOM calculation (height difference)

Two bDOM datasets from different time points are subtracted pixel-wise to generate a **difference raster (tDOM)**.

---

### 2. Multi-band classification

The tDOM is divided into multiple height classes:

* **Buildings & low vegetation:** −14 m to +14 m with 2 meter class intervals
* **Forest changes:** −30 m to −14 m with 4 meter class intervals

Each class is stored as a binary raster.

---

### 3. Cluster-based filtering

* Connected pixels (clusters) are mapped
* The size of each cluster is calculated
* Clusters below a defined threshold are filtered out

Basic principle:

> [!IMPORTANT]
> Misclassifications (e.g. caused by seasonal vegetation changes) often show fragmented patterns, whereas building changes form compact and more homogeneous structures.
> Therefore, building-related changes typically result in larger clusters.

(The filtering process is parallelized using multiprocessing to improve performance)

---

### 5. Result generation

The filtering process creates a boolean mask which is applied to the tDOM.

> Result: filtered 1 km² raster tiles

---

## 🖥️ Functions

(Script: `GUI_bDOM_filtering_for_practical_application`)

* Implementation of the `Core_bDOM_fitering_algorithm` script
* Graphical user interface (GUI) for easy application
* Interfaces to control all algorithm parameters (thresholds and classes)
* Automatic detection of connected 1 km² raster tiles in selected directories
* Automatic detection of already processed tiles → skipped
* Interface for user-defined file management
* Output of log files
* Soft-stop functionality to safely interrupt processing
* Memory-efficient LZW compression

📂 Output

The program automatically generates a structured output folder system:

* `01_tDOM` – height difference model
* `02/03_classification` – classified height ranges
* `04/05_masks` – filtered masks
* `06_tDOM_final` – final result

---

### 🔗 Raster merge

* Working with many 1 km² raster tiles is not user-friendly.
* `GUI_terrainTIFF_merge` merges the tiles into a single continuous TIFF.

---

### 🖌️ Visualization

* Visualization can be applied in ArcGIS Pro using
  `tDOM -14 bis +30m.lyrx`.

---

## 📊 Results

The filtering algorithm significantly reduces misdetections in the tDOM.
It enables area-wide change detection and leads to a strong reduction of the area that needs to be manually inspected.

This results in:

* good detection of building changes
* good detection of larger vegetation changes
* efficient support for updating land use information

<img width="460" height="285" alt="image" src="https://github.com/user-attachments/assets/17db1817-eb7a-4a0c-98fc-4137d47cd99b" />

<img width="460" height="285" alt="image" src="https://github.com/user-attachments/assets/2a9be4fa-e273-4d72-a2f1-4e60b2c9fdcb" />

---

## ⚠️ Limitations

* Errors are reduced but not completely eliminated
* → not a fully automated solution – expert validation is still required

---

## 🎓 Scientific context

Developed as part of the bachelor thesis:

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

When using this project, please use the associated DOI [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19392547.svg)](https://doi.org/10.5281/zenodo.19392547)

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# Veränderungsdetektion auf Basis vpn bDOM Differenz (tDOM) mit Filterung

### Detektion von Nutzungsänderungen mit gefiltertem digitalen Oberflächenmodellen

Dieses Programm implementiert einen Python-basierten Workflow zur Detektion potenzieller Nutzungsänderungen auf Basis **digitaler Oberflächenmodelle (bDOM)**.
Die Entwicklung erfolgte im Rahmen einer Bachelorarbeit im Studiengang Vermessung und Geoinformatik an der Hochschule Anhalt Dessau.

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19392547.svg)](https://doi.org/10.5281/zenodo.19392547)

---

## 📖 Hintergrund

Die Aktualisierung der tatsächlichen Nutzung (TN) im Liegenschaftskataster ist ein **zeitaufwändiger manueller Prozess**.
Große Flächen müssen visuell anhand von Luftbildern durchmustert werden, was zu hohem Arbeitsaufwand und verzögerten Aktualisierungen führt.

Diese Bacherlorarbeit verfolgt daher einen **semi-automatischen Ansatz**, um potenzielle Veränderungen auf Basis von Höhendifferenzen zu identifizieren.
Ziel der Arbeit war nicht die vollständige Automatisierung, sondern eine effiziente Vorfilterung relevanter Änderungsbereiche zur Unterstützung der Sachbearbeiter. 

Dazu werden 3 Datenquellen genutzt:
- digitale Forstgrundkarte (Umsetzung in Arcgis)
- digitales Feldblockkataster (Umsetzung in Arcgis)
- bildbasiertes digitales Oberflächenmodell (bDOM) (Umsetzung in Python)

---

## ⚙️ Methodik der bDOM Filterung (vgl. Bachelorarbeit)
(Skript: `Core_bDOM_fitering_algorithm`)

Der Workflow basiert auf einem mehrstufigen Filteransatz:

### 1. tDOM-Berechnung (Höhendifferenz)

Zwei bDOM-Datensätze unterschiedlicher Zeitpunkte werden pixelweise subtrahiert, um ein **Differenzraster (tDOM)** zu erzeugen.

### 2. Mehrband-Klassifikation

Das tDOM wird in mehrere Höhenklassen unterteilt:

* **Gebäude & niedrige Vegetation:** −14 m bis +14 m mit 2 Meter Klassenweite
* **Waldveränderungen:** −30 m bis −14 m mit 4 Meter Klassenweite
Jede Klasse wird als binäres Raster gespeichert.

### 3. Clusterbasierte Filterung

* Zusammenhängende Pixel (Cluster) werden gemaped
* größe jedes Clusters wird ermittelt
* Cluster unter einem festgelegten Schwellwert werden gefiltert

Grundlegender Ansatz:
> [!IMPORTANT]
> Fehldetektionen bspw. durch Blattbestandsveränderungen zeigen oft fragmentierte Muster, während bspw. Gebäudeveränderungen kompakte gleichmäßigere Strukturen bilden. 
> Folglich sind bspw. an veränderten Gebäuden größere Cluster vor zufinden.

(Die Filterung ist mittels Multiprocessing parallelisiert, um die Performance zu erhöhen)

### 5. Ergebnisgenerierung

Die Filterung erstellt eine booleanische Maske mit der das tDOM ausgeschnitten wird. 
> Ergebnis: gefilterte 1Km² Raster

---

## 🖥️ Funktionen der 
(Skript: `GUI_bDOM_filtering_for_practical_application`)

* Implementierung des `Core_bDOM_fitering_algorithm Skripts`
* Grafische Benutzeroberfläche (GUI) zur einfachen Anwendung/Benutzung
* Schnittstellen um alle Parameter des Algorythmus benutzerdefiniert zu steuern (Schwellwerte und Klassen)
* Automatische Suche nach zusammenhängenden Km² Rastern in angegeben Ordnern
* Automatische Suche nach bereits vollständig berechneten Km² Rastern -> werden Übersprungen
* Schnittstelle für Benutzerdefiniertes Dateimanagement 
* Ausgabe von Protokolldatei
* Soft-Stop Berechnungsabbruch zur Fehlervermeidung
* Speichersparende LZW Komprimierung 

📂 Ausgabe

Das Programm erzeugt automatisch die Ergebnisordnerstruktur:

* `01_tDOM` – Höhendifferenzmodell
* `02/03_Klassifikation` – klassifizierte Höhenbereiche
* `04/05_Masken` – gefilterte Masken
* `06_tDOM_final` – finales Ergebnis

### 🔗​ Raster Merge

* Die Darstellung von vielen 1km² Rastern ist nicht benutzerfreundlich und umständlich.
* `GUI_terrainTIFF_merge` führt die 1km² Raster zu einem geschlossenen TIFF zusammen.

### 🖌️​ Darstellung

* Kann im Programm ArgGIS Pro aus `tDOM -14 bis +30m.lyrx` entnommen werden.

---

## 📊 Ergebnisse

Der Filterungsalgorithmus reduziert Fehldetektionen im tDOM deutlich. Es ist eine flächendeckende Veränderungsdetektion möglich die zur erheblichen Reduktion der zu überprüfenden Fläche führt.

Daraus folgt:
* gute Erkennung von Gebäudeänderungen
* gute Erkennung von größeren Vegetationsänderungen
* effiziente Unterstützung bei der Aktualisierung der tatsächlichen Nutzung

<img width="460" height="285" alt="image" src="https://github.com/user-attachments/assets/17db1817-eb7a-4a0c-98fc-4137d47cd99b" />

<img width="460" height="285" alt="image" src="https://github.com/user-attachments/assets/2a9be4fa-e273-4d72-a2f1-4e60b2c9fdcb" />

---

## ⚠️ Einschränkungen

* Fehler werden nicht vollumfänglich entfernt
* -> keine vollautomatische Lösung – fachliche Prüfung weiterhin erforderlich

---

## 🎓 Wissenschaftlicher Kontext

Entwickelt im Rahmen der Bachelorarbeit:

**„Detektion von Veränderungen auf Basis digitaler Oberflächenmodelle, Feldblockkataster und Waldgrundkarte zur beschleunigten Aktualisierung der tatsächlichen Nutzung im Liegenschaftskataster“**

---

## 📜 Lizenz

Dieses Projekt steht unter der
GNU General Public License (GPL v3).

---

## 👤 Autor

Fabian Britze

---

## 📌 Zitierung

Bei Nutzung des Projekts bitte den zugehörigen [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19392547.svg)](https://doi.org/10.5281/zenodo.19392547)
 verwenden.

