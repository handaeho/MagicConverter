"""Bounded column planning for address-labelled workbook cell values."""
import math

from magic_converter.core.jobs.job_control import check_cancelled

LINES_PER_COLUMN = 59


def column_count(rows, width, maximum, font, font_size, advances):
    """Prefer fewer pages, retaining wider columns when the estimate ties.

    This only chooses a layout. The writer still wraps every actual character
    against its column width and preserves explicit continuation addresses.
    """
    from openpyxl.utils import get_column_letter

    minimum = min(3, maximum)
    costs = {count: 0 for count in range(minimum, maximum + 1)}

    def length(text):
        total = 0
        for character in text:
            if character not in advances:
                advances[character] = font.glyph_advance(ord(character)) * font_size
            total += advances[character]
        return total

    for index, values in rows:
        check_cancelled()
        for column, value in enumerate(values, 1):
            if value is None:
                continue
            address = f'{get_column_letter(column)}{index}'
            prefix = length(address + ' ')
            continuation = length('+999')
            for paragraph in str(value).split('\n'):
                text_width = length(paragraph)
                for count in costs:
                    available = width / count - 10 - prefix
                    if text_width <= available:
                        costs[count] += 1
                    else:
                        available -= continuation
                        costs[count] += max(1, math.ceil(text_width / max(1, available)))
    # Same 59 baselines as the actual writer (46 .. 568 at 9pt spacing).
    return min(costs, key=lambda count: (math.ceil(costs[count] / (LINES_PER_COLUMN * count)), count))
