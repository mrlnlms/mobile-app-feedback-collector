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

    def report_version(self):
        version = self.drive / 'reports/google-play-descritiva/2026-10-05T120000'
        version.mkdir(parents=True)
        (version / 'google-play-descritiva.html').write_text('report')
        assets = version / 'google-play-descritiva_files'
        assets.mkdir()
        (assets / 'test.js').write_text('script')
        analysis = self.root / 'analysis'
        analysis.mkdir()
        return analysis, version

    def test_legacy_report_links_become_one_link_and_setup_can_repeat(self):
        analysis, version = self.report_version()
        legacy = analysis / 'google-play-descritiva-output'
        legacy.symlink_to(version, target_is_directory=True)
        for name in ('google-play-descritiva.html', 'google-play-descritiva_files'):
            (analysis / name).symlink_to('google-play-descritiva-output/' + name)
        setup(self.root, self.drive)
        setup(self.root, self.drive)
        self.assertEqual((analysis / 'output').resolve(), version.resolve())
        self.assertEqual((analysis / 'output/google-play-descritiva.html').read_text(), 'report')
        self.assertEqual((version / 'google-play-descritiva_files/test.js').read_text(), 'script')
        for name in ('google-play-descritiva-output', 'google-play-descritiva.html', 'google-play-descritiva_files'):
            self.assertFalse((analysis / name).is_symlink())
            self.assertFalse((analysis / name).exists())

    def test_new_clone_restores_only_output_link(self):
        analysis, version = self.report_version()
        setup(self.root, self.drive)
        self.assertEqual((analysis / 'output').resolve(), version.resolve())
        self.assertEqual([p.name for p in analysis.iterdir()], ['output'])

    def test_divergent_legacy_report_link_is_preserved(self):
        analysis, version = self.report_version()
        legacy = analysis / 'google-play-descritiva-output'
        legacy.symlink_to(version, target_is_directory=True)
        other = analysis / 'other.html'
        other.write_text('different report')
        html = analysis / 'google-play-descritiva.html'
        html.symlink_to('other.html')
        with self.assertRaisesRegex(RuntimeError, 'divergente'):
            setup(self.root, self.drive)
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(html.read_text(), 'different report')
        self.assertFalse((analysis / 'output').is_symlink())

    def test_missing_drive_never_creates_a_fake_destination(self):
        target = self.drive / 'missing'
        with self.assertRaises(FileNotFoundError):
            setup(self.root, target)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
