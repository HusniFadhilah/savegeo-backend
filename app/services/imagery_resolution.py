"""Preserve native band grids without forcing an eager reprojection."""
import ee


def native_median(collection, bands):
    """Cloud masking must be applied to the collection before this call.

    A multi-UTM mosaic has a one-degree default projection. Assign each
    median band its source band's grid, including 20/60m bands, rather than
    inheriting the first (possibly coarse atmospheric) band's projection.
    Earth Engine still evaluates tiles lazily in the requested map projection.
    """
    first = collection.first()
    return ee.Image.cat([
        collection.select(band).median().setDefaultProjection(first.select(band).projection())
        for band in dict.fromkeys(bands)
    ])
