"""Explicit tropical-forest proxy labels for detrital carbon regression.

The labels are not field measurements. Ratios follow UNFCCC AR-TOOL12 v3,
tables on pp17-18. Living AGB carbon comes from NASA/ORNL 2010; climate is
WorldClim's long-term rainfall. Conversion back to dry AGB uses 0.47 and
litter converts to carbon using 0.37. Mangroves have a separate model domain.
"""

SOURCE = "https://cdm.unfccc.int/methodologies/ARmethodologies/tools/ar-am-tool-12-v3.0.pdf"
KEYS = {"litter": "TROPICAL_LITTER_REFERENCE", "deadwood": "TROPICAL_DEADWOOD_REFERENCE"}


def reference_image(pool, ee):
    agb_c = ee.Image(ee.ImageCollection("NASA/ORNL/biomass_carbon_density/v1").first()).select("agb")
    rain = ee.Image("WORLDCLIM/V1/BIO").select("bio12")
    elevation = ee.Image("USGS/SRTMGL1_003").select("elevation")
    high = elevation.gt(2000)
    if pool == "deadwood":
        ratio = (
            ee.Image.constant(0.06).where(rain.lte(1600), 0.01).where(rain.lte(1000), 0.02).where(high, 0.07)
        )
        carbon_fraction = 0.47
    else:
        ratio = ee.Image.constant(0.01).where(rain.lte(1000).And(high.Not()), 0.04)
        carbon_fraction = 0.37
    forest = ee.Image("UMD/hansen/global_forest_change_2023_v1_11")
    mask = (
        forest.select("treecover2000")
        .gte(30)
        .And(forest.select("lossyear").eq(0).Or(forest.select("lossyear").gt(10)))
    )
    mangrove = ee.ImageCollection("LANDSAT/MANGROVE_FORESTS").mosaic().select("1").unmask(0).eq(1)
    tropical = ee.Image.pixelLonLat().select("latitude").abs().lte(23.5)
    return (
        agb_c.divide(0.47)
        .multiply(ratio)
        .multiply(carbon_fraction)
        .updateMask(mask)
        .updateMask(mangrove.Not())
        .updateMask(tropical)
        .rename("agb")
    )
