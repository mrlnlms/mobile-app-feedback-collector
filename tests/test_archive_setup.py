"""Configuração repetível e preservação de arquivos divergentes."""

import tempfile
import unittest
from pathlib import Path

from scripts.archive_setup import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.drive = Path(self.temp.name) / 'drive'
        self.drive.mkdir()

    def test_existing_data_moves_once_and_setup_can_repeat(self):
        source = self.root / 'data/raw/example/test.txt'
        source.parent.mkdir(parents=True)
        source.write_text('preserved')
        setup(self.root, self.drive)
        self.assertTrue((self.root / 'data/raw').is_symlink())
        self.assertEqual(source.read_text(), 'preserved')
        self.assertEqual((self.drive / 'data/raw/example/test.txt').read_text(), 'preserved')
        setup(self.root, self.drive)
        self.assertFalse((self.root / '.runtime/archive-setup/raw').exists())

    def test_divergent_destination_keeps_local_source(self):
        source = self.root / 'data/raw/test.txt'
        source.parent.mkdir(parents=True)
        source.write_text('local')
        target = self.drive / 'data/raw/test.txt'
        target.parent.mkdir(parents=True)
        target.write_text('external')
        with self.assertRaisesRegex(RuntimeError, 'divergentes'):
            setup(self.root, self.drive)
        self.assertFalse((self.root / 'data/raw').is_symlink())
        self.assertEqual(source.read_text(), 'local')
        self.assertEqual(target.read_text(), 'external')

    def test_missing_drive_never_creates_a_fake_destination(self):
        target = self.drive / 'missing'
        with self.assertRaises(FileNotFoundError):
            setup(self.root, target)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
