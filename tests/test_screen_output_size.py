"""Unit tests for the OLED screen output size of the SteelSeries Arctis Nova Pro
Wireless Base Station.

The Base Station's screen API only registers a fixed set of documented panel
sizes (128x36/40/48/52). A 128x64 frame -- or any frame whose packed byte length
does not match the declared panel -- is silently dropped and the screen stays
blank. So every code path that produces image bytes MUST agree on exactly one
size: 128x52, packed to 832 bytes (52 rows x 16 bytes/row).

These tests pin that contract. If someone changes IMAGE_W/IMAGE_H or the packing,
the tests break before a blank screen ships.
"""
import sys
import unittest
from pathlib import Path

# Make the repo root importable when run via ``python -m unittest`` (which,
# unlike pytest, does not load conftest.py).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import claude_gamesense_statusline as gs

# Panel geometry the Base Station actually accepts.
EXPECTED_WIDTH = 128
EXPECTED_HEIGHT = 52
# Packed length: each row is ceil(width / 8) bytes, one bit per pixel.
BYTES_PER_ROW = (EXPECTED_WIDTH + 7) // 8          # 16
EXPECTED_PACKED_BYTES = BYTES_PER_ROW * EXPECTED_HEIGHT  # 832


class ScreenDimensionsTest(unittest.TestCase):
    """The declared panel dimensions and derived API keys must match hardware."""

    def test_image_dimensions_are_128x52(self):
        self.assertEqual(gs.IMAGE_W, EXPECTED_WIDTH)
        self.assertEqual(gs.IMAGE_H, EXPECTED_HEIGHT)

    def test_image_key_encodes_dimensions(self):
        self.assertEqual(gs.IMAGE_KEY, f"image-data-{EXPECTED_WIDTH}x{EXPECTED_HEIGHT}")

    def test_device_type_encodes_dimensions(self):
        self.assertEqual(gs.DEVICE_TYPE, f"screened-{EXPECTED_WIDTH}x{EXPECTED_HEIGHT}")

    def test_panel_is_a_documented_size(self):
        # 128x64 does not exist; guard against it being set by accident.
        documented = {(128, 36), (128, 40), (128, 48), (128, 52)}
        self.assertIn((gs.IMAGE_W, gs.IMAGE_H), documented)


class PackedByteLengthTest(unittest.TestCase):
    """Every producer of frame bytes must emit exactly EXPECTED_PACKED_BYTES."""

    def test_expected_packed_bytes_is_832(self):
        # Sanity-check the test's own arithmetic against the known panel.
        self.assertEqual(EXPECTED_PACKED_BYTES, 832)

    def test_blank_image_data_length(self):
        self.assertEqual(len(gs._blank_image_data()), EXPECTED_PACKED_BYTES)

    def test_blank_image_data_is_all_zero(self):
        blank = gs._blank_image_data()
        self.assertTrue(all(b == 0 for b in blank))

    def test_pack_empty_canvas_length(self):
        packed = gs._pack_image(gs._new_canvas())
        self.assertEqual(len(packed), EXPECTED_PACKED_BYTES)

    def test_pack_full_canvas_length_and_values(self):
        # A fully-lit canvas packs to all 0xFF bytes, same length.
        canvas = [[1] * gs.IMAGE_W for _ in range(gs.IMAGE_H)]
        packed = gs._pack_image(canvas)
        self.assertEqual(len(packed), EXPECTED_PACKED_BYTES)
        self.assertTrue(all(b == 0xFF for b in packed))

    def test_blank_matches_packed_blank_canvas(self):
        # The two independent "blank" paths must produce identical output.
        self.assertEqual(gs._blank_image_data(), gs._pack_image(gs._new_canvas()))


class RenderedFrameSizeTest(unittest.TestCase):
    """The real render + frame-build paths must honour the size contract."""

    def setUp(self):
        self.status = gs.SAMPLE_STATUS

    def test_render_limits_image_length(self):
        image_data, *_ = gs.render_limits_image(self.status)
        self.assertEqual(len(image_data), EXPECTED_PACKED_BYTES)

    def test_render_bytes_in_valid_range(self):
        image_data, *_ = gs.render_limits_image(self.status)
        self.assertTrue(all(0 <= b <= 255 for b in image_data))

    def test_build_frame_image_length(self):
        frame = gs.build_frame(self.status)["frame"]
        self.assertIn(gs.IMAGE_KEY, frame)
        self.assertEqual(len(frame[gs.IMAGE_KEY]), EXPECTED_PACKED_BYTES)

    def test_size_is_stable_across_inputs(self):
        # Empty status falls back to context bars but must keep the same size.
        for status in ({}, {"rate_limits": {}}, self.status):
            image_data, *_ = gs.render_limits_image(status)
            self.assertEqual(
                len(image_data), EXPECTED_PACKED_BYTES,
                msg=f"wrong size for status={status!r}",
            )


if __name__ == "__main__":
    unittest.main()
