# Veraenderungsdetektion-mit-bDOM-Forstgrundkarte-und-Feldblockkataster



---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# bDOM Veränderungsdetektion mit Filterung

### Filterbasierte Detektion von Nutzungsänderungen mit digitalen Oberflächenmodellen

Dieses Programm implementiert einen Python-basierten Workflow zur Detektion potenzieller Nutzungsänderungen auf Basis **digitaler Oberflächenmodelle (bDOM)**.
Die Entwicklung erfolgte im Rahmen einer Bachelorarbeit im Studiengang Vermessung und Geoinformatik an der Hochschule Anhalt Dessau.

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
> Fehldetektionen bspw. durch Blattsatndveränderungen zeigen oft fragmentierte Muster, während bspw. Gebäudeveränderungen kompakte gleichmäßigere Strukturen bilden. 
> Folglich sind bspw. an veränderten Gebäuden größere Cluster vor zufinden

(Die Filterung ist mittels Multiprocessing parallelisiert, um die Performance zu erhöhen)

### 5. Ergebnisgenerierung

Die Filterung erstellt eine booleanische Maske mit der das tDOM ausgeschnitten wird. 
> Ergebnis: gefilterte 1Km² Raster

### Raster Merge

Die Dartellung von vielen 1km² Rastern ist nicht benutzerfreundlich und umständlich.
`GUI_terrainTIFF_merge` führt die 1km² Raster zu einem geschlossenen TIFF zusammen.

### Darstellung

Kann im Programm ArgGIS Pro aus `tDOM -14 bis +30m.lyrx` entnommen werden. 

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
* Soft-Stop Berechnungsabbruch zur Fehler vermeidung
* Speichersparende LZW Komprimierung 

📂 Ausgabe

Das Programm erzeugt automatisch die Ergebnisordnerstruktur:

* `01_tDOM` – Höhendifferenzmodell
* `02/03_Klassifikation` – klassifizierte Höhenbereiche
* `04/05_Masken` – gefilterte Masken
* `06_tDOM_final` – finales Ergebnis

---

## 📊 Ergebnisse

Der Filterungsalgorithmus reduziert Fehldetektionen im tDOM deutlich. Es ist eine flächendeckende Veränderungsdetektion möglich die zur erheblichen Reduktion der zu überprüfenden Fläche führt.

Daraus folgt:
* gute Erkennung von Gebäudeänderungen
* gute Erkennung von größeren Vegetationsänderungen
* effiziente Unterstützung bei der Aktualisierung der tatsächlichen Nutzung

---

## ⚠️ Einschränkungen

* Fehler werden nicht zu gänzlich entfernt
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

Bei Nutzung des Projekts bitte den zugehörigen DOI (z. B. über Zenodo) verwenden.

