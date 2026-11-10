import unittest
from magic_converter.core.diagnostics.printer_geometry import predicted_native_origin


class PrinterGeometryTests(unittest.TestCase):
    def test_device_offset_is_explanatory_math_with_axis_specific_page_scale(self):
        self.assertEqual(predicted_native_origin([72,100],[600,800],[600,800],[12,12]),[60,88])
        self.assertEqual(predicted_native_origin([72,100],[600,800],[300,800],[12,0]),[30,100])
    def test_invalid_page_geometry_is_not_accepted(self):
        with self.assertRaises(ValueError):predicted_native_origin([72,100],[0,800],[600,800],[12,12])
