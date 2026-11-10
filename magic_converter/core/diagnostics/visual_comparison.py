"""Fixed-raster PDF comparisons: no alignment, resizing or layout masking."""
import hashlib
from pathlib import Path
import math
import pymupdf
from PIL import Image, ImageChops, ImageStat
from magic_converter.core.jobs.job_control import check_cancelled


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def compare(reference, candidate, *, dpi=96, mae_limit=1.0, changed_limit=.005, artifacts=None):
    if (not isinstance(dpi, int) or not 36 <= dpi <= 300 or not math.isfinite(mae_limit)
            or not 0 <= mae_limit <= 255 or not math.isfinite(changed_limit) or not 0 <= changed_limit <= 1):
        raise ValueError('Invalid visual comparison policy')
    reference, candidate = Path(reference), Path(candidate)
    report = {'reference': str(reference), 'candidate': str(candidate),
              'reference_sha256': sha(reference), 'candidate_sha256': sha(candidate),
              'renderer': 'PyMuPDF ' + pymupdf.VersionBind, 'dpi': dpi,
              'policy': {'mae_limit': mae_limit, 'changed_fraction_limit': changed_limit,
                         'pixel_delta': 12, 'page_size_tolerance_pt': .1}, 'pages': []}
    with pymupdf.open(reference) as first, pymupdf.open(candidate) as second:
        if first.needs_pass or second.needs_pass or not 0 < len(first) <= 1000 or not 0 < len(second) <= 1000:
            raise ValueError('Encrypted, empty or excessive PDF comparison')
        report['page_counts'] = [len(first), len(second)]
        for index in range(min(len(first), len(second))):
            check_cancelled()
            a, b = first[index], second[index]
            item = {'page': index + 1, 'reference_size_pt': list(a.rect)[2:], 'candidate_size_pt': list(b.rect)[2:]}
            if abs(a.rect.width - b.rect.width) > .1 or abs(a.rect.height - b.rect.height) > .1:
                item.update(status='different', reason='page_geometry')
            else:
                dimensions = [(math.ceil(p.rect.width * dpi / 72), math.ceil(p.rect.height * dpi / 72)) for p in (a,b)]
                if any(w * h > 40_000_000 for w,h in dimensions): raise ValueError('PDF raster exceeds 40 MP')
                images = [Image.frombytes('RGB', (p.width, p.height), p.samples) for p in
                          (a.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False),
                           b.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False))]
                try:
                    if images[0].size != images[1].size:
                        item.update(status='different', reason='raster_geometry')
                    else:
                        diff = ImageChops.difference(*images)
                        with diff:
                            mae = sum(ImageStat.Stat(diff).mean) / 3
                            red, green, blue = diff.split()
                            with red, green, blue:
                                with ImageChops.lighter(ImageChops.lighter(red, green), blue) as maximum:
                                    with maximum.point(lambda value: 255 if value > 12 else 0) as mask:
                                        fraction = mask.histogram()[255] / (mask.width * mask.height)
                            item.update(mae=mae, changed_fraction=fraction,
                                        status='matched' if mae <= mae_limit and fraction <= changed_limit else 'different')
                            if artifacts is not None and item['status'] == 'different':
                                folder = Path(artifacts); folder.mkdir(parents=True, exist_ok=True)
                                diff.save(folder / f'page-{index+1:04}-difference.png')
                                with Image.new('RGB', (images[0].width * 2, images[0].height), 'white') as pair:
                                    pair.paste(images[0], (0,0)); pair.paste(images[1], (images[0].width,0))
                                    pair.save(folder / f'page-{index+1:04}-pair.png')
                finally:
                    for image in images: image.close()
            report['pages'].append(item)
    report['status'] = ('matched' if report['page_counts'][0] == report['page_counts'][1]
                        and all(p['status'] == 'matched' for p in report['pages']) else 'different')
    return report
