"""Ordinary GIF image routes use the owned Pillow worker, with real frames."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageSequence
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.conversion_execution import execute_file


class GifRouteTests(unittest.TestCase):
    def test_animation_canvas_disposal_alpha_timing_and_repeat_semantics(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'animated.gif'
            frames = [Image.new('RGBA', (20, 16), (0, 0, 0, 0)) for _ in range(3)]
            for index, frame in enumerate(frames):
                for x in range(index * 4, index * 4 + 4):
                    for y in range(4, 9): frame.putpixel((x, y), (255, 0, 0, 255))
            frames[0].save(source, save_all=True, append_images=frames[1:], duration=[40, 80, 120], loop=2, disposal=2)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            from magic_converter.core.conversion.file_router import slots_for
            self.assertTrue({'image:png','image:webp'} <= {s.action.key for s in slots_for([source],'convert')})
            with Image.open(source) as image:
                expected = [f.convert('RGBA').copy() for f in ImageSequence.Iterator(image)]
            for target in ('png', 'webp'):
                request = prepare_request([source], 'image:' + target, create_catalog(ffmpeg_available=False))
                self.assertEqual(request.files[0].route.steps[0].engine_id, 'images')
                output = execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
                with Image.open(output) as image:
                    self.assertEqual(image.n_frames, 3)
                    self.assertEqual(image.info['loop'], 3)
                    actual = []
                    for frame in ImageSequence.Iterator(image):
                        pixels = frame.convert('RGBA'); pixels.load()
                        actual.append((pixels.copy(), frame.info['duration']))
                    self.assertEqual([duration for _, duration in actual], [40, 80, 120])
                    for reference, (pixels, _) in zip(expected, actual):
                        self.assertEqual(pixels.size, reference.size)
                        # RGB under fully transparent pixels is not visible content.
                        self.assertEqual(Image.alpha_composite(Image.new('RGBA', pixels.size, 'white'), pixels).tobytes(),
                                         Image.alpha_composite(Image.new('RGBA', reference.size, 'white'), reference).tobytes())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_bad_gif_refuses_without_output_and_unverified_targets_stay_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'invalid.gif'; source.write_bytes(b'not gif')
            catalog = create_catalog(ffmpeg_available=False)
            for target in ('png', 'webp'):
                request = prepare_request([source], 'image:' + target, catalog)
                with self.assertRaises(Exception):
                    execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
            with self.assertRaises(ValueError): prepare_request([source], 'image:jpg', catalog)
            self.assertEqual(list(Path(folder).iterdir()), [source])

    def test_no_repeat_and_infinite_animation_controls(self):
        from magic_converter.core.conversion.converter import convert_image
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'a.gif'
            for settings, expected in (({}, 1), ({'loop': 0}, 0)):
                Image.new('RGB', (8, 8), 'red').save(source, save_all=True,
                    append_images=[Image.new('RGB', (8, 8), 'blue')], duration=[50, 70], **settings)
                output = Path(folder) / 'a.png'; convert_image(source, output, '.png')
                with Image.open(output) as image: self.assertEqual(image.info['loop'], expected)


if __name__ == '__main__': unittest.main()
