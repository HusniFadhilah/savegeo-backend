import re

from app.core.default_configs import DEFAULT_CONFIGS


def _relative_luminance(hex_color):
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def test_default_pool_palettes_are_distinct_valid_and_high_contrast():
    values = {item[0]: item[1] for item in DEFAULT_CONFIGS}
    palettes = {
        pool: values[f"carbon.pool.{pool}.vis_palette"].split(",")
        for pool in ("agb", "bgb", "soc", "litter", "deadwood")
    }

    assert len({tuple(colors) for colors in palettes.values()}) == len(palettes)
    for colors in palettes.values():
        assert len(colors) == 6
        assert all(re.fullmatch(r"[0-9a-fA-F]{6}", color) for color in colors)
        luminance = [_relative_luminance(color) for color in colors]
        assert all(low < high for low, high in zip(luminance, luminance[1:]))
