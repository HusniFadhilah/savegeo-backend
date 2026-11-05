# dMRV field-data fixtures

- `bu_dessy_sample.csv` — the 21 tree rows of Point ID 1 (Tembalang, transect T1) from the Bu Dessy carbon survey
  (`Paket_Notebook_Estimasi_Stok_Karbon_Full/Data_Pohon_Lengkap.csv`). Source headers are kept, including the junk
  trailing columns. `Tahun` (2024), `Latitude` and `Longitude` were added from the survey's coordinate table.
  Biomass values are allometric results reported by the survey, not destructive measurements. The 400 m² plot
  area used in tests is an assumption, not a measurement.
- `bu_dessy_sample_synthetic_2022.csv` — **synthetic**. Three trees of the same point, shrunk by an arbitrary
  factor, used only to test repeat-visit history. It is not measured data.
