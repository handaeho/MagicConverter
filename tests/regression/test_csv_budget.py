import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized import data_converter as data


class CsvBudgetTests(unittest.TestCase):
    def test_normal_quoted_multiline_and_exact_budget_boundary_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'quoted.csv'
            source.write_bytes(b'name,value\n"a,b","first\nsecond"\nx,y\n')
            with patch.object(data,'MAX_ITEMS',7):
                # Exercise the parser's exact allocation boundary in-process;
                # public facade isolation has separate real-file regressions.
                value=data.read_data.__wrapped__(source,'csv')
                self.assertEqual(value,[{'name':'a,b','value':'first\nsecond'},{'name':'x','value':'y'}])
                data.validate(value)

    def test_excess_rows_are_refused_before_materialising_the_remaining_input(self):
        def rows(*_,**__):
            yield ['name','value']
            for _ in range(3): yield ['x','y']
            raise AssertionError('Reader advanced past the existing item budget')
        with patch.object(data,'MAX_ITEMS',7),patch.object(data,'text',return_value='input'),patch.object(data.csv,'reader',side_effect=rows):
            with self.assertRaisesRegex(ConversionError,'항목 제한'): data.read_data.__wrapped__(Path('unused.csv'),'csv')

    def test_tsv_and_bad_column_counts_keep_existing_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.tsv';source.write_text('a\tb\nx\ty\n',encoding='utf-8')
            self.assertEqual(data.read_data(source,'tsv'),[{'a':'x','b':'y'}])
            source.write_text('a\tb\nx\n',encoding='utf-8')
            with self.assertRaisesRegex(ConversionError,'열 개수'): data.read_data(source,'tsv')


if __name__=='__main__': unittest.main()
