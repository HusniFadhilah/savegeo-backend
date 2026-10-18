"""Regional interpretation rules for Dynamic World class labels."""
import ee


SNOW_ICE_CLASS_ID = 8
INDONESIA_BOUNDARY_ASSET = "USDOS/LSIB_SIMPLE/2017"
INDONESIA_BOUNDARY_FIELD = "country_na"
INDONESIA_NAME = "Indonesia"

# Dynamic World occasionally assigns its snow/ice label to tropical terrain.
# Keep the label within a 20 km radius of Puncak Jaya, where Indonesian
# glaciers are present, and mask it elsewhere inside Indonesia.
PUNCAK_JAYA_LON_LAT = (137.159533, -4.078883)
PUNCAK_JAYA_RADIUS_M = 20_000


def mask_indonesia_snow_ice(label_image):
    """Mask Dynamic World Snow & Ice pixels in Indonesia except Puncak Jaya.

    Other classes in Indonesia and all classes outside Indonesia are left
    unchanged. Pixels are masked rather than reassigned to another class, so
    area, coverage, and change statistics do not invent a replacement label.
    """
    indonesia = (
        ee.FeatureCollection(INDONESIA_BOUNDARY_ASSET)
        .filter(ee.Filter.eq(INDONESIA_BOUNDARY_FIELD, INDONESIA_NAME))
        .geometry()
    )
    puncak_jaya = ee.Geometry.Point(list(PUNCAK_JAYA_LON_LAT)).buffer(PUNCAK_JAYA_RADIUS_M)

    # Clip-based masks keep these vector boundaries at the requested output
    # scale, even when a collection reduction has a coarse default projection.
    indonesia_mask = ee.Image.constant(1).clip(indonesia).unmask(0)
    puncak_mask = ee.Image.constant(1).clip(puncak_jaya).unmask(0)
    keep_pixel = (
        label_image.neq(SNOW_ICE_CLASS_ID)
        .Or(indonesia_mask.eq(0))
        .Or(puncak_mask.eq(1))
    )
    return label_image.updateMask(keep_pixel)


def indonesia_snow_ice_note() -> str:
    return "Snow & Ice Dynamic World disaring di Indonesia; kelas dipertahankan dalam radius 20 km dari Puncak Jaya."
