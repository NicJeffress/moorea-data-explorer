import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('data_bundle', Path(__file__).resolve().parents[1] / 'scripts/data_bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'original'
        self.target = self.base / 'clone with "quotes"'
        self.source.mkdir()
        self.target.mkdir()
        for name in bundle.PATHS:
            p = self.source / name
            if p.suffix:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('{}' if p.suffix == '.json' else 'database fixture')
            else:
                p.mkdir(parents=True)
                (p / 'sample.bin').write_bytes(bytes(range(256)) * 20)
        (self.source / 'glen-explorer/library/query-index.json').write_text(json.dumps([
            {'parquet_path': str(self.source / 'mcr_data/sample.bin'), 'view_name': 'data_unchanged'}]))
        self.output = self.base / 'bundle'
        bundle.package(self.source, self.output, limit=127)

    def test_round_trip_relocation_and_stable_ids(self):
        bundle.restore(self.target, self.output)
        self.assertEqual((self.target / 'mcr_data/sample.bin').read_bytes(), (self.source / 'mcr_data/sample.bin').read_bytes())
        records = json.loads((self.target / 'glen-explorer/library/query-index.json').read_text())
        self.assertEqual(records[0]['view_name'], 'data_unchanged')
        self.assertEqual(records[0]['parquet_path'], str(self.target / 'mcr_data/sample.bin'))
        self.assertTrue((self.target / '.data-snapshot.json').exists())

    def test_checksum_failure_installs_nothing(self):
        part = next(self.output.glob('*.part-*'))
        part.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
            bundle.restore(self.target, self.output)
        self.assertFalse((self.target / 'mcr_data').exists())

    def test_existing_data_is_preserved(self):
        data = self.target / 'mcr_data'
        data.mkdir()
        (data / 'keep.txt').write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'Refusing to overwrite'):
            bundle.restore(self.target, self.output)
        self.assertEqual((data / 'keep.txt').read_text(), 'keep')

    def test_unlisted_archive_file_rejected(self):
        p = self.output / 'manifest.json'
        m = json.loads(p.read_text())
        m['files'] = m['files'][1:]
        p.write_text(json.dumps(m))
        with self.assertRaisesRegex(RuntimeError, 'Unsafe or unexpected'):
            bundle.restore(self.target, self.output)
        self.assertFalse((self.target / 'mcr_data').exists())


if __name__ == '__main__':
    unittest.main()
