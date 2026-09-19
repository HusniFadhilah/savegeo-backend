"""
landcover_dataset_registry.py — Central registry for land cover datasets.

Single source of truth for dataset metadata, legends, visualization params,
and helper functions. Supports GEE and ArcGIS providers.

Imported by app.py; no Flask or GEE dependency allowed here.
"""
import logging
from datetime import UTC, datetime

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Legend: class value → color + label
# ─────────────────────────────────────────────

LAND_COVER_LEGENDS: dict[str, dict] = {
    "Dynamic_World": {
        "0": {"color": "#419BDF", "label": "Water"},
        "1": {"color": "#397D49", "label": "Trees"},
        "2": {"color": "#88B053", "label": "Grass"},
        "3": {"color": "#7A87C6", "label": "Flooded vegetation"},
        "4": {"color": "#E49635", "label": "Crops"},
        "5": {"color": "#DFC35A", "label": "Shrub & Scrub"},
        "6": {"color": "#C4281B", "label": "Built Area"},
        "7": {"color": "#A59B8F", "label": "Bare ground"},
        "8": {"color": "#B39FE1", "label": "Snow & Ice"},
    },
    "GeoSave_Copernicus_DynamicWorld": {
        "0": {"color": "#419BDF", "label": "Water"},
        "1": {"color": "#397D49", "label": "Trees"},
        "2": {"color": "#88B053", "label": "Grass"},
        "3": {"color": "#7A87C6", "label": "Flooded vegetation"},
        "4": {"color": "#E49635", "label": "Crops"},
        "5": {"color": "#DFC35A", "label": "Shrub & Scrub"},
        "6": {"color": "#C4281B", "label": "Built Area"},
        "7": {"color": "#A59B8F", "label": "Bare ground"},
        "8": {"color": "#B39FE1", "label": "Snow & Ice"},
    },
    "ESA_WorldCover": {
        "10":  {"color": "#006400", "label": "Trees"},
        "20":  {"color": "#ffbb22", "label": "Shrubland"},
        "30":  {"color": "#ffff4c", "label": "Grassland"},
        "40":  {"color": "#f096ff", "label": "Cropland"},
        "50":  {"color": "#fa0000", "label": "Built-up"},
        "60":  {"color": "#b4b4b4", "label": "Barren/sparse vegetation"},
        "70":  {"color": "#f0f0f0", "label": "Snow and ice"},
        "80":  {"color": "#0032c8", "label": "Open water"},
        "90":  {"color": "#0096a0", "label": "Herbaceous wetland"},
        "95":  {"color": "#00cf75", "label": "Mangroves"},
        "100": {"color": "#fae6a0", "label": "Moss and lichen"},
    },
    "ESRI_LandCover": {
        "1":  {"color": "#1A5BAB", "label": "Water"},
        "2":  {"color": "#358221", "label": "Trees"},
        "4":  {"color": "#87D19E", "label": "Flooded vegetation"},
        "5":  {"color": "#FFDB5C", "label": "Crops"},
        "7":  {"color": "#ED022A", "label": "Built area"},
        "8":  {"color": "#EDE9E4", "label": "Bare ground"},
        "9":  {"color": "#F2FAFF", "label": "Snow/Ice"},
        "10": {"color": "#C8C8C8", "label": "Clouds"},
        "11": {"color": "#C6AD8D", "label": "Rangeland"},
    },
    "ESRI_LULC_LivingAtlas": {
        "1":  {"color": "#1A5BAB", "label": "Water"},
        "2":  {"color": "#358221", "label": "Trees"},
        "4":  {"color": "#87D19E", "label": "Flooded vegetation"},
        "5":  {"color": "#FFDB5C", "label": "Crops"},
        "7":  {"color": "#ED022A", "label": "Built area"},
        "8":  {"color": "#EDE9E4", "label": "Bare ground"},
        "9":  {"color": "#F2FAFF", "label": "Snow/Ice"},
        "10": {"color": "#C8C8C8", "label": "Clouds"},
        "11": {"color": "#C6AD8D", "label": "Rangeland"},
    },
    "MODIS_LandCover": {
        "1":  {"color": "#05450a", "label": "Evergreen Needleleaf Forest"},
        "2":  {"color": "#086a10", "label": "Evergreen Broadleaf Forest"},
        "3":  {"color": "#54a708", "label": "Deciduous Needleleaf Forest"},
        "4":  {"color": "#78d203", "label": "Deciduous Broadleaf Forest"},
        "5":  {"color": "#009900", "label": "Mixed Forests"},
        "6":  {"color": "#c6b044", "label": "Closed Shrublands"},
        "7":  {"color": "#dcd159", "label": "Open Shrublands"},
        "8":  {"color": "#dade48", "label": "Woody Savannas"},
        "9":  {"color": "#fbff13", "label": "Savannas"},
        "10": {"color": "#b6ff05", "label": "Grasslands"},
        "11": {"color": "#27ff87", "label": "Permanent Wetlands"},
        "12": {"color": "#c24f44", "label": "Croplands"},
        "13": {"color": "#a5a5a5", "label": "Urban and Built-up"},
        "14": {"color": "#ff6d4c", "label": "Cropland/Natural Mosaic"},
        "15": {"color": "#69fff8", "label": "Snow and Ice"},
        "16": {"color": "#f9ffa4", "label": "Barren"},
        "17": {"color": "#1c0dff", "label": "Water"},
    },
    "Copernicus_LandCover": {
        "20":  {"color": "#ffbb22", "label": "Shrubs"},
        "30":  {"color": "#ffff4c", "label": "Herbaceous vegetation"},
        "40":  {"color": "#f096ff", "label": "Cultivated"},
        "50":  {"color": "#fa0000", "label": "Urban / Built-up"},
        "60":  {"color": "#b4b4b4", "label": "Bare / Sparse"},
        "70":  {"color": "#f0f0f0", "label": "Snow and ice"},
        "80":  {"color": "#0032c8", "label": "Permanent water"},
        "90":  {"color": "#0096a0", "label": "Herbaceous wetland"},
        "100": {"color": "#fae6a0", "label": "Moss and lichen"},
        "111": {"color": "#58481f", "label": "Closed forest, evergreen needleleaf"},
        "112": {"color": "#009900", "label": "Closed forest, evergreen broadleaf"},
        "113": {"color": "#70663e", "label": "Closed forest, deciduous needleleaf"},
        "114": {"color": "#00cc00", "label": "Closed forest, deciduous broadleaf"},
        "115": {"color": "#4e751f", "label": "Closed forest, mixed"},
        "116": {"color": "#007800", "label": "Closed forest, unknown"},
        "121": {"color": "#666000", "label": "Open forest, evergreen needleleaf"},
        "122": {"color": "#8db400", "label": "Open forest, evergreen broadleaf"},
        "123": {"color": "#8d7400", "label": "Open forest, deciduous needleleaf"},
        "124": {"color": "#a0dc00", "label": "Open forest, deciduous broadleaf"},
        "125": {"color": "#929900", "label": "Open forest, mixed"},
        "126": {"color": "#648c00", "label": "Open forest, unknown"},
        "200": {"color": "#000080", "label": "Ocean"},
    },
    "GLC_FCS30D": {
        "10":  {"color": "#ffff64", "label": "Rainfed cropland"},
        "11":  {"color": "#ffff64", "label": "Herbaceous cover cropland"},
        "12":  {"color": "#ffff00", "label": "Tree or shrub cover cropland"},
        "20":  {"color": "#aaf0f0", "label": "Irrigated cropland"},
        "51":  {"color": "#4c7300", "label": "Open evergreen broadleaved forest"},
        "52":  {"color": "#006400", "label": "Closed evergreen broadleaved forest"},
        "61":  {"color": "#aac800", "label": "Open deciduous broadleaved forest"},
        "62":  {"color": "#00a000", "label": "Closed deciduous broadleaved forest"},
        "71":  {"color": "#005000", "label": "Open evergreen needle-leaved forest"},
        "72":  {"color": "#003c00", "label": "Closed evergreen needle-leaved forest"},
        "81":  {"color": "#286400", "label": "Open deciduous needle-leaved forest"},
        "82":  {"color": "#285000", "label": "Closed deciduous needle-leaved forest"},
        "91":  {"color": "#a0b432", "label": "Open mixed leaf forest"},
        "92":  {"color": "#788200", "label": "Closed mixed leaf forest"},
        "120": {"color": "#966400", "label": "Shrubland"},
        "121": {"color": "#964b00", "label": "Evergreen shrubland"},
        "122": {"color": "#966400", "label": "Deciduous shrubland"},
        "130": {"color": "#ffb432", "label": "Grassland"},
        "140": {"color": "#ffdcd2", "label": "Lichens and mosses"},
        "150": {"color": "#ffebaf", "label": "Sparse vegetation"},
        "152": {"color": "#ffd278", "label": "Sparse shrubland"},
        "153": {"color": "#ffebaf", "label": "Sparse herbaceous"},
        "181": {"color": "#00a884", "label": "Swamp"},
        "182": {"color": "#73ffdf", "label": "Marsh"},
        "183": {"color": "#9ebbd7", "label": "Flooded flat"},
        "184": {"color": "#828282", "label": "Saline"},
        "185": {"color": "#f57ab6", "label": "Mangrove"},
        "186": {"color": "#66cdab", "label": "Salt marsh"},
        "187": {"color": "#444f89", "label": "Tidal flat"},
        "190": {"color": "#c31400", "label": "Impervious surfaces"},
        "200": {"color": "#fff5d7", "label": "Bare areas"},
        "201": {"color": "#dcdcdc", "label": "Consolidated bare areas"},
        "202": {"color": "#fff5d7", "label": "Unconsolidated bare areas"},
        "210": {"color": "#0046c8", "label": "Water body"},
        "220": {"color": "#ffffff", "label": "Permanent ice and snow"},
    },
    "C3S_LandCover": {
        "10":  {"color": "#ffff64", "label": "Cropland, rainfed"},
        "20":  {"color": "#aaf0f0", "label": "Cropland, irrigated or post-flooding"},
        "30":  {"color": "#dcf064", "label": "Mosaic cropland / natural vegetation"},
        "40":  {"color": "#c8c864", "label": "Mosaic natural vegetation / cropland"},
        "50":  {"color": "#006400", "label": "Tree cover, broadleaved, evergreen"},
        "60":  {"color": "#00a000", "label": "Tree cover, broadleaved, deciduous, closed to open"},
        "61":  {"color": "#00a000", "label": "Tree cover, broadleaved, deciduous, closed"},
        "62":  {"color": "#aac800", "label": "Tree cover, broadleaved, deciduous, open"},
        "70":  {"color": "#003c00", "label": "Tree cover, needleleaved, evergreen"},
        "71":  {"color": "#003c00", "label": "Tree cover, needleleaved, evergreen, closed"},
        "72":  {"color": "#005000", "label": "Tree cover, needleleaved, evergreen, open"},
        "80":  {"color": "#285000", "label": "Tree cover, needleleaved, deciduous"},
        "81":  {"color": "#285000", "label": "Tree cover, needleleaved, deciduous, closed"},
        "82":  {"color": "#286400", "label": "Tree cover, needleleaved, deciduous, open"},
        "90":  {"color": "#788200", "label": "Tree cover, mixed leaf type"},
        "100": {"color": "#8ca000", "label": "Mosaic tree and shrub / herbaceous cover"},
        "110": {"color": "#be9600", "label": "Mosaic herbaceous cover / tree and shrub"},
        "120": {"color": "#966400", "label": "Shrubland"},
        "121": {"color": "#966400", "label": "Evergreen shrubland"},
        "122": {"color": "#966400", "label": "Deciduous shrubland"},
        "130": {"color": "#ffb432", "label": "Grassland"},
        "140": {"color": "#ffdcd2", "label": "Lichens and mosses"},
        "150": {"color": "#ffebaf", "label": "Sparse vegetation"},
        "151": {"color": "#ffd278", "label": "Sparse tree"},
        "152": {"color": "#ffebaf", "label": "Sparse shrub"},
        "153": {"color": "#ffebaf", "label": "Sparse herbaceous"},
        "160": {"color": "#00785a", "label": "Tree cover, flooded, fresh or brackish water"},
        "170": {"color": "#009678", "label": "Tree cover, flooded, saline water"},
        "180": {"color": "#00dc82", "label": "Shrub or herbaceous cover, flooded"},
        "190": {"color": "#c31400", "label": "Urban areas"},
        "200": {"color": "#fff5d7", "label": "Bare areas"},
        "201": {"color": "#dcdcdc", "label": "Consolidated bare areas"},
        "202": {"color": "#fff5d7", "label": "Unconsolidated bare areas"},
        "210": {"color": "#0046c8", "label": "Water bodies"},
        "220": {"color": "#ffffff", "label": "Permanent snow and ice"},
    },
    "JAXA_FNF": {
        "1": {"color": "#006400", "label": "Forest"},
        "2": {"color": "#feff99", "label": "Non-Forest"},
        "3": {"color": "#0000ff", "label": "Water"},
    },
    "JAXA_FNF4": {
        "1": {"color": "#006400", "label": "Dense Forest"},
        "2": {"color": "#00a000", "label": "Non-Dense Forest"},
        "3": {"color": "#feff99", "label": "Non-Forest"},
        "4": {"color": "#0000ff", "label": "Water"},
    },
    "MapBiomas_Indonesia": {
        "1":  {"color": "#1f8d49", "label": "Forest"},
        "3":  {"color": "#1f8d49", "label": "Forest Formation"},
        "5":  {"color": "#04381d", "label": "Mangrove"},
        "76": {"color": "#2f7360", "label": "Peat Swamp Forest"},
        "10": {"color": "#d6bc74", "label": "Non-Forest Natural Formation"},
        "13": {"color": "#d89f5c", "label": "Other Non-Forest Natural Vegetation"},
        "18": {"color": "#e974ed", "label": "Agriculture"},
        "40": {"color": "#f272c2", "label": "Rice Paddy"},
        "35": {"color": "#9065d0", "label": "Oil Palm"},
        "9":  {"color": "#7a5900", "label": "Pulpwood Plantation"},
        "21": {"color": "#ffefc3", "label": "Other Agriculture"},
        "22": {"color": "#d4271e", "label": "Non-Vegetated Area"},
        "30": {"color": "#9c0027", "label": "Mining Pit"},
        "24": {"color": "#d4271e", "label": "Urban Area"},
        "25": {"color": "#db4d4f", "label": "Other Non-Vegetation"},
        "26": {"color": "#2532e4", "label": "Water Body"},
        "31": {"color": "#091077", "label": "Aquaculture"},
        "33": {"color": "#2532e4", "label": "River, Lake, Ocean"},
        "27": {"color": "#ffffff", "label": "Not Observed / Cloud"},
    },
    "GeoSave_MapBiomas_Indonesia": {
        "1":  {"color": "#1f8d49", "label": "Forest"},
        "3":  {"color": "#1f8d49", "label": "Forest Formation"},
        "5":  {"color": "#04381d", "label": "Mangrove"},
        "76": {"color": "#2f7360", "label": "Peat Swamp Forest"},
        "10": {"color": "#d6bc74", "label": "Non-Forest Natural Formation"},
        "13": {"color": "#d89f5c", "label": "Other Non-Forest Natural Vegetation"},
        "18": {"color": "#e974ed", "label": "Agriculture"},
        "40": {"color": "#f272c2", "label": "Rice Paddy"},
        "35": {"color": "#9065d0", "label": "Oil Palm"},
        "9":  {"color": "#7a5900", "label": "Pulpwood Plantation"},
        "21": {"color": "#ffefc3", "label": "Other Agriculture"},
        "22": {"color": "#d4271e", "label": "Non-Vegetated Area"},
        "30": {"color": "#9c0027", "label": "Mining Pit"},
        "24": {"color": "#d4271e", "label": "Urban Area"},
        "25": {"color": "#db4d4f", "label": "Other Non-Vegetation"},
        "26": {"color": "#2532e4", "label": "Water Body"},
        "31": {"color": "#091077", "label": "Aquaculture"},
        "33": {"color": "#2532e4", "label": "River, Lake, Ocean"},
        "27": {"color": "#ffffff", "label": "Not Observed / Cloud"},
    },
    # ESA CCI 2018 — 9 classes (class labels from service legend endpoint)
    # DEA Mangroves Landsat — canopy cover classes (from service legend)
    "DEA_Mangroves": {
        "1": {"color": "#76C442", "label": "Mangrove Woodland (20-50% cover)"},
        "2": {"color": "#2B8C22", "label": "Mangrove Open Forest (50-80% cover)"},
        "3": {"color": "#004E00", "label": "Mangrove Closed Forest (>80% cover)"},
    },
    # JRC Tropical Moist Forest Annual Changes v1 2022
    "JRC_TMF": {
        "1": {"color": "#006400", "label": "Undisturbed Tropical Moist Forest"},
        "2": {"color": "#FFCC00", "label": "Degraded Tropical Moist Forest"},
        "3": {"color": "#CC0000", "label": "Deforested Land"},
        "4": {"color": "#00CC00", "label": "Forest Regrowth"},
        "5": {"color": "#0064C8", "label": "Permanent Water"},
        "6": {"color": "#AAAAAA", "label": "Other Land Cover"},
    },
    # Tsinghua FROM-GLC 10m Global Land Cover 2017
    "FROM_GLC10": {
        "10":  {"color": "#FFFF64", "label": "Cropland"},
        "20":  {"color": "#006400", "label": "Forest"},
        "30":  {"color": "#ADFF2F", "label": "Grassland"},
        "40":  {"color": "#DAA520", "label": "Shrubland"},
        "50":  {"color": "#00CED1", "label": "Wetland"},
        "60":  {"color": "#0000CD", "label": "Water"},
        "70":  {"color": "#F5F5DC", "label": "Tundra"},
        "80":  {"color": "#DC143C", "label": "Impervious Surface"},
        "90":  {"color": "#D2B48C", "label": "Bareland"},
        "100": {"color": "#FFFFFF", "label": "Snow and Ice"},
    },
    # GLAD Annual Global Land Use/Land Cover by Potapov et al. 2022 (cloud_geotiff)
    "GLAD_GLCLUC": {
        "1": {"color": "#006400", "label": "Forest"},
        "2": {"color": "#ADFF2F", "label": "Short Vegetation"},
        "3": {"color": "#FFFF64", "label": "Cropland"},
        "4": {"color": "#DC143C", "label": "Built-up"},
        "5": {"color": "#D2B48C", "label": "Bareland"},
        "6": {"color": "#FFFFFF", "label": "Snow and Ice"},
        "7": {"color": "#0000CD", "label": "Water"},
        "8": {"color": "#00CED1", "label": "Wetland"},
    },
}


# ─────────────────────────────────────────────
# Visualization parameters per dataset
# ─────────────────────────────────────────────

LAND_COVER_VIS: dict[str, dict] = {
    "Dynamic_World":        {"min": 0,  "max": 8,   "palette": ["#419BDF","#397D49","#88B053","#7A87C6","#E49635","#DFC35A","#C4281B","#A59B8F","#B39FE1"]},
    "GeoSave_Copernicus_DynamicWorld": {"min": 0,  "max": 8,   "palette": ["#419BDF","#397D49","#88B053","#7A87C6","#E49635","#DFC35A","#C4281B","#A59B8F","#B39FE1"]},
    "ESA_WorldCover":       {"min": 10, "max": 100, "palette": ["006400","ffbb22","ffff4c","f096ff","fa0000","b4b4b4","f0f0f0","0032c8","0096a0","00cf75","fae6a0"]},
    "ESRI_LandCover":       {"min": 1,  "max": 11,  "palette": ["1A5BAB","358221","87D19E","FFDB5C","ED022A","EDE9E4","F2FAFF","C8C8C8","C6AD8D"]},
    "ESRI_LULC_LivingAtlas":{"min": 1,  "max": 11,  "palette": ["1A5BAB","358221","87D19E","FFDB5C","ED022A","EDE9E4","F2FAFF","C8C8C8","C6AD8D"]},
    "MODIS_LandCover":      {"min": 1,  "max": 17,  "palette": ["05450a","086a10","54a708","78d203","009900","c6b044","dcd159","dade48","fbff13","b6ff05","27ff87","c24f44","a5a5a5","ff6d4c","69fff8","f9ffa4","1c0dff"]},
    "Copernicus_LandCover": {"min": 20, "max": 200, "palette": ["ffbb22","ffff4c","f096ff","fa0000","b4b4b4","f0f0f0","0032c8","0096a0","fae6a0","58481f","009900","70663e","00cc00","4e751f","007800","666000","8db400","8d7400","a0dc00","929900","648c00","000080"]},
    "GLC_FCS30D":           {"min": 10, "max": 220, "palette": ["ffff64","ffff64","ffff00","aaf0f0","4c7300","006400","aac800","00a000","005000","003c00","286400","285000","a0b432","788200","966400","964b00","966400","ffb432","ffdcd2","ffebaf","ffd278","ffebaf","00a884","73ffdf","9ebbd7","828282","f57ab6","66cdab","444f89","c31400","fff5d7","dcdcdc","fff5d7","0046c8","ffffff"]},
    "C3S_LandCover":        {"min": 10, "max": 220, "palette": ["ffff64","aaf0f0","dcf064","c8c864","006400","00a000","00a000","aac800","003c00","003c00","005000","285000","285000","286400","788200","8ca000","be9600","966400","966400","966400","ffb432","ffdcd2","ffebaf","ffd278","ffebaf","ffebaf","00785a","009678","00dc82","c31400","fff5d7","dcdcdc","fff5d7","0046c8","ffffff"]},
    "JAXA_FNF":             {"min": 1,  "max": 3,   "palette": ["006400","feff99","0000ff"]},
    "JAXA_FNF4":            {"min": 1,  "max": 4,   "palette": ["006400","00a000","feff99","0000ff"]},
    "MapBiomas_Indonesia":     {"min": 1,  "max": 76,  "palette": ["1f8d49","04381d","2f7360","d6bc74","d89f5c","e974ed","f272c2","9065d0","7a5900","ffefc3","d4271e","9c0027","db4d4f","2532e4","091077","ffffff"]},
    "GeoSave_MapBiomas_Indonesia": {"min": 1,  "max": 76,  "palette": ["1f8d49","04381d","2f7360","d6bc74","d89f5c","e974ed","f272c2","9065d0","7a5900","ffefc3","d4271e","9c0027","db4d4f","2532e4","091077","ffffff"]},
    "DEA_Mangroves":           {"min": 1,  "max": 3,   "palette": ["76C442","2B8C22","004E00"]},
    "JRC_TMF":                 {"min": 1,  "max": 6,   "palette": ["006400","FFCC00","CC0000","00CC00","0064C8","AAAAAA"]},
    "FROM_GLC10":              {"min": 10, "max": 100, "palette": ["FFFF64","006400","ADFF2F","DAA520","00CED1","0000CD","F5F5DC","DC143C","D2B48C","FFFFFF"]},
    "GLAD_GLCLUC":             {"min": 1,  "max": 8,   "palette": ["006400","ADFF2F","FFFF64","DC143C","D2B48C","FFFFFF","0000CD","00CED1"]},
}


# ─────────────────────────────────────────────
# Native scale (metres) per dataset
# ─────────────────────────────────────────────

LAND_COVER_NATIVE_SCALE: dict[str, int] = {
    "Dynamic_World":         10,
    "GeoSave_Copernicus_DynamicWorld": 10,
    "ESA_WorldCover":        10,
    "ESRI_LandCover":        10,
    "ESRI_LULC_LivingAtlas": 10,
    "MODIS_LandCover":       500,
    "Copernicus_LandCover":  100,
    "GLC_FCS30D":            30,
    "C3S_LandCover":         300,
    "JAXA_FNF":              25,
    "JAXA_FNF4":             25,
    "MapBiomas_Indonesia":   30,
    "GeoSave_MapBiomas_Indonesia": 30,
    "DEA_Mangroves":           25,
    "JRC_TMF":                 30,
    "FROM_GLC10":              10,
    "GLAD_GLCLUC":             30,
}


# ─────────────────────────────────────────────
# Dataset options — main registry
# Fields:
#   name, source, provider, provider_type, resolution,
#   year_min, year_max, monthly, gee_id,
#   arcgis_item_id, arcgis_service_url,
#   requires_auth, supports_summary, supports_transition,
#   official_url, publication_url, limitations,
#   model (method description), accuracy
# ─────────────────────────────────────────────

LAND_COVER_DATASET_OPTIONS: dict[str, dict] = {
    "ESA_WorldCover": {
        "name":                "ESA WorldCover",
        "source":              "ESA",
        "provider":            "ESA WorldCover Consortium",
        "provider_type":       "gee_official",
        "resolution":          "10m",
        "year_min":            2020,
        "year_max":            2021,
        "monthly":             False,
        "gee_id":              "ESA/WorldCover/v100, ESA/WorldCover/v200",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": False,
        "official_url":        "https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200?hl=id",
        "publication_url":     "https://pure.iiasa.ac.at/id/eprint/18478/",
        "limitations":         [],
        "model":               "Global land cover from Sentinel-1 and Sentinel-2; separate v100/v200 products.",
        "accuracy":            "2021 v200 independently validated overall accuracy: 76.7%. 2020 product reported about 75% global OA.",
    },
    "Dynamic_World": {
        "name":                "Dynamic World",
        "source":              "Google + WRI",
        "provider":            "World Resources Institute / Google",
        "provider_type":       "gee_near_real_time",
        "resolution":          "10m",
        "year_min":            2015,
        "year_max":            "present",
        "monthly":             True,
        "gee_id":              "GOOGLE/DYNAMICWORLD/V1",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1",
        "publication_url":     "https://www.nature.com/articles/s41597-022-01307-4",
        "limitations":         [
            "Near-real-time: data available from 2015-06-27 to present, including 2026.",
            "Probability threshold (dw_probability_threshold) recommended for high-confidence mapping.",
        ],
        "description":         "Near-real-time global land cover at 10m from Sentinel-2. Supports current-year (2026) analysis. Optional probability threshold mask via dw_probability_threshold parameter.",
        "model":               "Deep learning semantic segmentation on Sentinel-2 L1C scenes; outputs class probabilities and top-1 label.",
        "accuracy":            "No single design-based global OA for the live collection; official paper reports technical validation against expert consensus and recommends probability thresholding.",
    },
    "GeoSave_Copernicus_DynamicWorld": {
        "name":                "GeoSave Copernicus Sentinel-2 Land Cover",
        "source":              "GeoSave Engine + Copernicus CDSE",
        "provider":            "GeoSave Engine / Copernicus Data Space Ecosystem",
        "provider_type":       "geosave_cdse_stac",
        "resolution":          "10m",
        "year_min":            2015,
        "year_max":            "present",
        "monthly":             True,
        "gee_id":              None,
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       True,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://github.com/weedkat/geosave-engine",
        "publication_url":     "https://pypi.org/project/geosave-engine/",
        "limitations":         [
            "Membutuhkan paket geosave-engine dan kredensial Copernicus CDSE untuk ingestion scene live.",
            "Kelas analisis memakai skema Dynamic World 9 kelas agar langsung kompatibel dengan ringkasan dan land-cover change.",
            "Jika scene STAC live belum tersedia/credential belum aktif, backend tetap memakai koleksi Dynamic World berbasis Sentinel-2 sebagai fallback klasifikasi operasional.",
        ],
        "description":         "Opsi land cover 10m yang diekspos sebagai dataset GeoSave/Copernicus. Cocok untuk analisis land cover dan land-cover change berbasis Sentinel-2 pre/post atau multi-tahun.",
        "model":               "GeoSave Engine menyiapkan ingestion Copernicus STAC/Sentinel-2; klasifikasi operasional memakai label Dynamic World agar hasil summary/change map langsung dapat dihitung.",
        "accuracy":            "Mengikuti karakteristik Dynamic World untuk label kelas; gunakan confidence threshold untuk memfilter piksel rendah keyakinan.",
        "attribution":         "GeoSave Engine, Copernicus CDSE, Google Dynamic World/WRI",
        "class_schema":        "DYNAMIC_WORLD_9_CLASS",
        "alias_of":            "Dynamic_World",
    },
    "ESRI_LandCover": {
        "name":                "ESRI 10m Annual LULC v3 (GEE Community Catalog)",
        "source":              "Esri / Impact Observatory",
        "provider":            "Esri, Impact Observatory, Microsoft",
        "provider_type":       "gee_community_catalog",
        "resolution":          "10m",
        "year_min":            2017,
        "year_max":            2025,
        "monthly":             False,
        "gee_id":              "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS",
        "arcgis_item_id":      "cfcb7609de5f478eb7666240902d4d3d",
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://gee-community-catalog.org/projects/S2TSLULC/",
        "publication_url":     "https://www.esri.com/about/newsroom/announcements/esri-releases-latest-land-cover-map-with-updated-sentinel-2-satellite-data",
        "limitations":         [
            "GEE Community Catalog copy may lag behind official ArcGIS Living Atlas by one year. Use ESRI_LULC_LivingAtlas for latest year via ArcGIS.",
            "Latest annual product in this GEE Community Catalog copy is 2025. Requests for 2026 return 2025 data with fallback_reason in response metadata.",
            "Annual 2026 product is NOT yet available; use Dynamic_World for near-real-time 2026 LULC.",
        ],
        "model":               "Deep learning AI land classification model using Sentinel-2 annual imagery and large human-labeled training data; v3 uses 9 annual classes.",
        "accuracy":            "Community Catalog notes average assessed accuracy over 75% for each annual map.",
    },
    "ESRI_LULC_LivingAtlas": {
        "name":                "Esri Sentinel-2 10m LULC Time Series (ArcGIS Living Atlas)",
        "source":              "ArcGIS Living Atlas",
        "provider":            "Esri, Impact Observatory, Microsoft",
        "provider_type":       "arcgis_living_atlas",
        "resolution":          "10m",
        "year_min":            2017,
        "year_max":            2025,
        "monthly":             False,
        "gee_id":              None,
        "arcgis_item_id":      "cfcb7609de5f478eb7666240902d4d3d",
        "arcgis_service_url":  "https://ic.imagery1.arcgis.com/arcgis/rest/services/Sentinel2_10m_LandCover/ImageServer",
        "arcgis_rendering_rule": "Cartographic Renderer for Visualization and Analysis",
        "arcgis_year_field":   "Year",
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://www.arcgis.com/home/item.html?id=cfcb7609de5f478eb7666240902d4d3d",
        "publication_url":     "https://www.esri.com/about/newsroom/announcements/esri-releases-latest-land-cover-map-with-updated-sentinel-2-satellite-data",
        "limitations":         [
            "Requires ArcGIS account or API key for full ImageServer access.",
            "Area statistics require ArcGIS raster analysis privilege or backend sampling fallback.",
            "Tile proxy depends on ARCGIS_ENABLED=true and valid credential in server config.",
        ],
        "model":               "Same deep learning AI model as ESRI_LandCover but served from ArcGIS Living Atlas ImageServer. Includes 2025 data not yet in GEE Community Catalog.",
        "accuracy":            "Average assessed accuracy over 75% for each annual map.",
        "attribution":         "Esri, Impact Observatory",
        "class_schema":        "ESRI_9_CLASS",
        "description":         "Official Living Atlas item. Updated to 2025 as of 2026-04-23.",
        "alias_of":             "ESRI_LandCover",
    },
    "MODIS_LandCover": {
        "name":                "MODIS MCD12Q1 IGBP",
        "source":              "NASA LP DAAC",
        "provider":            "NASA MODIS Terra+Aqua",
        "provider_type":       "gee_official",
        "resolution":          "500m",
        "year_min":            2001,
        "year_max":            2024,
        "monthly":             False,
        "gee_id":              "MODIS/061/MCD12Q1",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://www.earthdata.nasa.gov/data/catalog/lpcloud-mcd12q1-061",
        "publication_url":     "https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD12Q1",
        "limitations":         [],
        "model":               "Supervised classifications of MODIS Terra/Aqua reflectance with post-processing and ancillary information; IGBP LC_Type1 used here.",
        "accuracy":            "Collection provides QA/assessment layers; official catalog describes supervised classification but not a single universal OA value.",
    },
    "Copernicus_LandCover": {
        "name":                "Copernicus Global Land Cover",
        "source":              "Copernicus Land Monitoring Service",
        "provider":            "Copernicus / VITO",
        "provider_type":       "gee_official",
        "resolution":          "100m",
        "year_min":            2015,
        "year_max":            2019,
        "monthly":             False,
        "gee_id":              "COPERNICUS/Landcover/100m/Proba-V-C3/Global",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://land.copernicus.eu/en/products/global-dynamic-land-cover/copernicus-global-land-service-land-cover-100m-collection-3-epoch-2015-2019-globe",
        "publication_url":     "https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_Landcover_100m_Proba-V-C3_Global",
        "limitations":         [],
        "model":               "Annual CGLS-LC100 Collection 3 from PROBA-V time series and ancillary datasets; LCCS discrete classification.",
        "accuracy":            "Validated thematic accuracy: about 80.3% for the 2015-2019 Collection 3 product.",
    },
    "GLC_FCS30D": {
        "name":                "GLC_FCS30D",
        "source":              "CAS / GEE Community Catalog",
        "provider":            "Aerospace Information Research Institute, Chinese Academy of Sciences",
        "provider_type":       "gee_community_catalog",
        "resolution":          "30m",
        "year_min":            1985,
        "year_max":            2022,
        "monthly":             False,
        "gee_id":              "projects/sat-io/open-datasets/GLC-FCS30D/annual, projects/sat-io/open-datasets/GLC-FCS30D/five-years-map",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://gee-community-catalog.org/projects/glc_fcs/",
        "publication_url":     "https://doi.org/10.5194/essd-16-1353-2024",
        "limitations":         ["Years before 2000 available at 5-year steps only (1985, 1990, 1995). Annual from 2000."],
        "description":         "Global 30m land-cover dynamic monitoring product with 35 fine land-cover classes.",
        "model":               "Continuous change detection, local adaptive updating models, and temporal consistency optimization from dense Landsat time series.",
        "accuracy":            "Overall accuracy reported as 80.88% for 10 major classes and 73.24% for LCCS level-1 validation.",
    },
    "C3S_LandCover": {
        "name":                "C3S Land Cover",
        "source":              "Copernicus Climate Change Service",
        "provider":            "C3S / ESA CCI / VITO",
        "provider_type":       "gee_community_catalog",
        "resolution":          "300m",
        "year_min":            1992,
        "year_max":            2022,
        "monthly":             False,
        "gee_id":              "projects/sat-io/open-datasets/ESA/C3S-LC-L4-LCCS",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://gee-community-catalog.org/projects/c3slc/",
        "publication_url":     "https://cds.climate.copernicus.eu/datasets/satellite-land-cover",
        "limitations":         [],
        "description":         "Global annual land-cover maps with UN FAO LCCS classes, designed for long-term temporal consistency.",
        "model":               "ESA CCI-consistent annual LC maps using AVHRR, SPOT-VGT, PROBA-V, and Sentinel-3 OLCI/SLSTR time series.",
        "accuracy":            "Useful for climate and long-term accounting workflows; coarse 300m resolution.",
    },
    "JAXA_FNF": {
        "name":                "JAXA ALOS Forest/Non-Forest",
        "source":              "JAXA EORC",
        "provider":            "JAXA EORC",
        "provider_type":       "gee_official",
        "resolution":          "25m",
        "year_min":            2007,
        "year_max":            2018,
        "monthly":             False,
        "gee_id":              "JAXA/ALOS/PALSAR/YEARLY/FNF",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://developers.google.com/earth-engine/datasets/catalog/JAXA_ALOS_PALSAR_YEARLY_FNF",
        "publication_url":     "https://earth.jaxa.jp/en/data/2555/index.html",
        "limitations":         [],
        "model":               "Region-dependent SAR backscatter thresholding on ALOS PALSAR/PALSAR-2 mosaics using FAO forest definition.",
        "accuracy":            "Classification accuracy checked with in-situ photos and high-resolution optical satellite images; no single global OA stated on GEE catalog.",
        "deprecated":           True,
        "replacement_key":      "JAXA_FNF4",
    },
    "JAXA_FNF4": {
        "name":                "JAXA PALSAR Forest/Non-Forest 4-class",
        "source":              "JAXA EORC",
        "provider":            "JAXA EORC",
        "provider_type":       "gee_official",
        "resolution":          "25m",
        "year_min":            2017,
        "year_max":            2020,
        "monthly":             False,
        "gee_id":              "JAXA/ALOS/PALSAR/YEARLY/FNF4",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://developers.google.com/earth-engine/datasets/catalog/JAXA_ALOS_PALSAR_YEARLY_FNF4",
        "publication_url":     "https://www.eorc.jaxa.jp/ALOS/en/dataset/fnf_e.htm",
        "limitations":         [],
        "model":               "Random Forest classification of 25m global PALSAR-2/PALSAR SAR mosaics into dense forest, non-dense forest, non-forest, and water using FAO forest definition.",
        "accuracy":            "Official pages describe the forest definition and validation approach; useful for forest/carbon monitoring but not a complete LULC taxonomy.",
    },
    "MapBiomas_Indonesia": {
        "name":                "MapBiomas Indonesia LANDY",
        "source":              "MapBiomas Indonesia",
        "provider":            "Auriga Nusantara and MapBiomas Indonesia network",
        "provider_type":       "gee_community_catalog",
        "resolution":          "30m",
        "year_min":            2000,
        "year_max":            2023,
        "monthly":             False,
        "gee_id":              "GeoTIFF: gs://mapbiomas-public/initiatives/indonesia/collection_3/coverage/indonesia_coverage_{year}.tif",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://landy.mapbiomas.id/en/collectionmap",
        "publication_url":     "https://landy.mapbiomas.id/methodologytransition/about",
        "limitations":         ["Data loaded as Cloud GeoTIFF; availability of specific years depends on MapBiomas public bucket."],
        "description":         "Dataset LULC tahunan khusus Indonesia. Implementasi memakai GeoTIFF Collection 3 bila tersedia dan fallback ke Collection 2.",
        "model":               "Pendekatan MapBiomas berbasis citra satelit tahunan dan klasifikasi otomatis di Google Earth Engine.",
        "accuracy":            "Dokumentasi MapBiomas Indonesia menyediakan metodologi, legenda, peta referensi, dan statistik.",
    },
    "GeoSave_MapBiomas_Indonesia": {
        "name":                "GeoSave MapBiomas Indonesia LANDY",
        "source":              "MapBiomas Indonesia via GeoSave Engine",
        "provider":            "Auriga Nusantara / MapBiomas Indonesia network",
        "provider_type":       "geosave_geotiff",
        "resolution":          "30m",
        "year_min":            2000,
        "year_max":            2022,
        "monthly":             False,
        "gee_id":              None,
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "raster_url_template": "gs://mapbiomas-public/initiatives/indonesia/collection_3/coverage/indonesia_coverage_{year}.tif",
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://landy.mapbiomas.id/en/collectionmap",
        "publication_url":     "https://landy.mapbiomas.id/methodologytransition/about",
        "limitations":         [
            "Dataset Indonesia paling lengkap di katalog ini: kelas hutan, gambut, mangrove, sawit, sawah, tambang, urban, tambak, dan badan air.",
            "GeoSave Engine dipakai sebagai jalur GeoTIFF/COG-compatible probe dan metadata loader; komputasi tile/ringkasan tetap memakai backend raster pipeline yang sudah kompatibel.",
            "Ketersediaan efektif mengikuti resolver MapBiomas lokal; request setelah 2022 akan memakai fallback tahun terdekat yang tersedia.",
        ],
        "description":         "Jalur GeoSave untuk MapBiomas Indonesia LANDY. Cocok sebagai dataset LULC lengkap untuk summary, land-cover change, forest/non-forest, gambut, sawit, tambang, urban, dan air.",
        "model":               "MapBiomas Indonesia annual LULC dari klasifikasi citra satelit tahunan; dimuat sebagai GeoTIFF-compatible source melalui adapter GeoSave.",
        "accuracy":            "Mengikuti dokumentasi dan metodologi MapBiomas Indonesia LANDY.",
        "attribution":         "MapBiomas Indonesia / Auriga Nusantara",
        "class_schema":        "MAPBIOMAS_INDONESIA_LANDY",
        "alias_of":            "MapBiomas_Indonesia",
    },
    "DEA_Mangroves": {
        "name":                "Digital Earth Australia Mangroves Landsat (ArcGIS Living Atlas)",
        "source":              "ArcGIS Living Atlas",
        "provider":            "Geoscience Australia / Digital Earth Australia",
        "provider_type":       "arcgis_living_atlas",
        "resolution":          "25m",
        "year_min":            1987,
        "year_max":            2024,
        "monthly":             False,
        "time_aware":          True,
        "gee_id":              None,
        "arcgis_item_id":      None,
        "arcgis_service_url":  "https://di-daa.img.arcgis.com/arcgis/rest/services/Land_and_vegetation/DEA_Mangroves_Landsat/ImageServer",
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://www.ga.gov.au/scientific-topics/earth-obs/accessing-satellite-imagery/dea",
        "publication_url":     "https://doi.org/10.1016/j.rse.2018.04.038",
        "limitations":         [
            "Coverage focused on Australia and nearby Southeast Asia coast (incl. Papua / Maluku).",
            "Only coastal mangrove pixels classified; inland areas show no data.",
            "3 canopy cover density classes; not a full LULC classification.",
            "AOI outside coverage extent returns empty histogram — use coastal/estuarine AOI.",
        ],
        "description":         "Annual mangrove canopy cover at 25m from Landsat archive, classified into 3 density classes. Good for monitoring mangrove change in coastal Indonesia.",
        "model":               "Fractional cover model from Landsat time series using spectral unmixing. Canopy cover: sparse 10-20%, moderate 20-50%, dense >50%.",
        "accuracy":            "Validation against field data and high-resolution optical imagery; see Lucas et al. (2014) and Mitchell et al. (2021) publications.",
        "attribution":         "Geoscience Australia / Digital Earth Australia",
        "class_schema":        "DEA_MANGROVE_COVER_3CLASS",
    },
    # ── New provider types ────────────────────────────────────────────────────
    "JRC_TMF": {
        "name":                "JRC Tropical Moist Forest Annual Changes v1 2022",
        "source":              "European Commission JRC",
        "provider":            "Joint Research Centre, European Commission",
        "provider_type":       "gee_official",
        "resolution":          "30m",
        "year_min":            1990,
        "year_max":            2022,
        "monthly":             False,
        "time_aware":          True,
        "gee_id":              "JRC/TMF/v1_2022/AnnualChanges",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://developers.google.com/earth-engine/datasets/catalog/JRC_TMF_v1_2022_AnnualChanges",
        "publication_url":     "https://doi.org/10.1126/sciadv.abe1603",
        "limitations":         [
            "Tropical moist forest biome only; not a full global LULC.",
            "6 classes focused on forest state and change — no cropland/urban sub-classes.",
            "AOI outside tropical moist forest biome may return mostly 'Other Land Cover'.",
        ],
        "description":         "Annual tropical moist forest land cover 1990–2022 at 30m. Classifies forest state (undisturbed, degraded, deforested, regrowth) and water. Excellent for deforestation tracking in Indonesia.",
        "model":               "Annual Landsat time-series classification of tropical moist forest biome using pixel-based decision tree algorithm. Tracks forest state transitions annually.",
        "accuracy":            "Reported overall accuracy >94% for the 6-class schema; validated against reference samples and VHR imagery.",
        "attribution":         "European Commission Joint Research Centre (JRC)",
        "class_schema":        "JRC_TMF_6CLASS",
    },
    "FROM_GLC10": {
        "name":                "Tsinghua FROM-GLC 10m Global Land Cover 2017",
        "source":              "Tsinghua University / GEE Official",
        "provider":            "Department of Earth System Science, Tsinghua University",
        "provider_type":       "gee_official",
        "resolution":          "10m",
        "year_min":            2017,
        "year_max":            2017,
        "monthly":             False,
        "time_aware":          False,
        "gee_id":              "Tsinghua/FROM-GLC10/2017V01",
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": False,
        "official_url":        "https://developers.google.com/earth-engine/datasets/catalog/Tsinghua_FROM-GLC10_2017V01",
        "publication_url":     "https://doi.org/10.1016/j.rse.2019.111322",
        "limitations":         [
            "Single epoch — only 2017 data is available in this 10m product.",
            "Transition analysis not supported (single year only).",
            "10 broad land cover classes; limited sub-class detail for tropical forests.",
            "Accuracy varies by region; tropical areas may have lower accuracy than temperate.",
        ],
        "description":         "Global 10m land cover map for 2017, produced using Sentinel-2 imagery and machine learning at Tsinghua University. 10 land cover classes including cropland, forest, grassland, wetland, water, impervious, and bareland.",
        "model":               "Random forest classification of Sentinel-2 10m imagery. Training samples collected via active learning from global experts. 10 thematic classes based on IGBP-inspired schema.",
        "accuracy":            "Overall accuracy 72.8% reported in the FROM-GLC10 paper (Chen et al. 2019). Validation against 38,400 reference samples globally.",
        "attribution":         "Department of Earth System Science, Tsinghua University",
        "class_schema":        "FROM_GLC10_10CLASS",
        "deprecated":           True,
        "replacement_key":      "ESRI_LandCover",
    },
    "GLAD_GLCLUC": {
        "name":                "GLAD Annual Global Land Use/Land Cover (Potapov et al. 2022)",
        "source":              "UMD GLAD Lab",
        "provider":            "Global Land Analysis & Discovery (GLAD), University of Maryland",
        "provider_type":       "cloud_geotiff",
        "resolution":          "30m",
        "year_min":            2000,
        "year_max":            2020,
        "monthly":             False,
        "time_aware":          True,
        "gee_id":              None,
        "arcgis_item_id":      None,
        "arcgis_service_url":  None,
        "raster_url_template": "gs://earthenginepartners-hansen/GLCLUC2020/GLCLUC2020_{year}.tif",
        "requires_auth":       False,
        "supports_summary":    True,
        "supports_transition": True,
        "official_url":        "https://glad.umd.edu/dataset/GLCLUC2020",
        "publication_url":     "https://doi.org/10.1126/science.abo1489",
        "limitations":         [
            "Loaded via Cloud GeoTIFF (ee.Image.loadGeoTIFF). GCS path format may require verification.",
            "If the GCS URI is inaccessible, the backend will return an error with the attempted URI.",
            "Data available 2000–2020 only. No data before 2000.",
            "8 broad classes; no sub-class tropical forest detail.",
        ],
        "description":         "Annual global land use and land cover at 30m from 2000 to 2020, produced by the UMD GLAD lab. 8 classes: Forest, Short Vegetation, Cropland, Built-up, Bareland, Snow/Ice, Water, Wetland. Demonstrates cloud_geotiff provider pattern.",
        "model":               "Annual Landsat time-series analysis using spectral-temporal segmentation and supervised classification. Tracks annual LULC state globally at 30m.",
        "accuracy":            "Potapov et al. (2022, Science) report 86.7% overall accuracy for the 8-class annual maps at global scale.",
        "attribution":         "Global Land Analysis & Discovery (GLAD) Lab, University of Maryland",
        "class_schema":        "GLAD_GLCLUC_8CLASS",
    },
}


# ─────────────────────────────────────────────
# Tropical improbable LULC labels (skip by default for Indonesia)
# ─────────────────────────────────────────────

TROPICAL_IMPROBABLE_LULC_LABELS = {
    "Snow & Ice",
    "Snow and Ice",
    "Snow and ice",
    "Snow/Ice",
    "Permanent Snow and Ice",
}


# ─────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────

def should_skip_lulc_class(label: str, include_improbable_classes: bool = False) -> bool:
    if include_improbable_classes:
        return False
    return label in TROPICAL_IMPROBABLE_LULC_LABELS


def _landcover_year_bound(value) -> int:
    if isinstance(value, int):
        return min(value, datetime.now(UTC).year)
    text = str(value or "").strip().lower()
    if text in {"present", "now", "latest"}:
        return datetime.now(UTC).year
    try:
        return min(int(text), datetime.now(UTC).year)
    except (TypeError, ValueError):
        return datetime.now(UTC).year


def supported_glc_fcs30d_year(year: int) -> int:
    supported = [1985, 1990, 1995] + list(range(2000, 2023))
    requested = int(year)
    return min(supported, key=lambda item: (abs(item - requested), item))


def mapbiomas_indonesia_geotiff_uri(year: int) -> tuple[str, dict]:
    requested_year = int(year)
    candidates = [
        {
            "collection": "Collection 3",
            "year": max(2000, min(2023, requested_year)),
            "path": "collection_3",
        },
        {
            "collection": "Collection 2",
            "year": max(2000, min(2022, requested_year)),
            "path": "collection_2",
        },
    ]

    for candidate in candidates:
        https_url = (
            "https://storage.googleapis.com/mapbiomas-public/initiatives/indonesia/"
            f"{candidate['path']}/coverage/indonesia_coverage_{candidate['year']}.tif"
        )
        try:
            response = requests.head(https_url, timeout=8)
            if response.status_code < 400:
                gs_uri = https_url.replace("https://storage.googleapis.com/", "gs://", 1)
                return gs_uri, candidate
        except requests.RequestException:
            continue

    fallback = candidates[-1]
    return (
        f"gs://mapbiomas-public/initiatives/indonesia/{fallback['path']}/coverage/indonesia_coverage_{fallback['year']}.tif",
        fallback,
    )


def glad_glcluc_geotiff_uri(year: int) -> tuple[str, dict]:
    """Resolve Cloud GeoTIFF URI for GLAD Annual Global Land Use/Land Cover (Potapov et al. 2022).

    Loads via ee.Image.loadGeoTIFF() in app.py. Year clamped to 2000–2020.
    GCS path: gs://earthenginepartners-hansen/GLCLUC2020/GLCLUC2020_{year}.tif
    If GCS URI is incorrect, backend returns a clear error with the attempted URI.
    """
    effective_year = max(2000, min(2020, int(year)))
    gs_uri = f"gs://earthenginepartners-hansen/GLCLUC2020/GLCLUC2020_{effective_year}.tif"
    return gs_uri, {"year": effective_year}


def get_dataset_list(
    module: str | None = None,
    provider: str | None = None,
) -> list[dict]:
    """Return filtered list of datasets for /api/datasets endpoint.

    Each entry contains a compact summary suitable for discovery responses.
    """
    result = []
    for key, info in LAND_COVER_DATASET_OPTIONS.items():
        dataset_provider = info.get("provider_type", "gee_official")

        if module is not None and module != "landcover":
            continue

        # Match provider_type prefix or 'arcgis' shorthand
        if provider is not None and (
            (provider == "arcgis" and not dataset_provider.startswith("arcgis"))
            or (provider not in ("arcgis",) and provider != dataset_provider)
        ):
            continue

        year_max_raw = info.get("year_max")
        year_max = _landcover_year_bound(year_max_raw) if year_max_raw else None

        result.append({
            "key":                key,
            "name":               info.get("name"),
            "module":             "landcover",
            "provider":           info.get("provider"),
            "provider_type":      dataset_provider,
            "source":             info.get("source"),
            "resolution":         info.get("resolution"),
            "year_min":           info.get("year_min"),
            "year_max":           year_max,
            "requires_auth":      info.get("requires_auth", False),
            "supports_summary":   info.get("supports_summary", True),
            "supports_transition":info.get("supports_transition", True),
            "deprecated":        bool(info.get("deprecated", False)),
            "replacement_key":   info.get("replacement_key"),
            "alias_of":          info.get("alias_of"),
            "official_url":       info.get("official_url"),
            "limitations":        info.get("limitations", []),
            "arcgis_item_id":     info.get("arcgis_item_id"),
            "native_scale":       LAND_COVER_NATIVE_SCALE.get(key),
            "class_count":        len(LAND_COVER_LEGENDS.get(key, {})),
            "description":        info.get("description"),
            "attribution":        info.get("attribution"),
        })

    return result
