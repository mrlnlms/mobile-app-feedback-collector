"""Publicação completa do relatório e preservação da versão anterior em falhas."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.reports import render_google_play as report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'repo').resolve()
        (self.root / 'analysis').mkdir(parents=True)
        self.drive = Path(self.temp.name) / 'drive'
        self.drive.mkdir()
        (self.root / 'private').symlink_to(self.drive, target_is_directory=True)
        (self.root / 'analysis/google-play-descritiva.qmd').write_text('---\ntitle: Test\n---\n')

    def generate(self, args, **kwargs):
        source = Path(args[2])
        self.assertTrue(source.is_relative_to(self.root / '.runtime'))
        self.assertEqual(kwargs['env']['QUARTO_PYTHON'], str(self.root / 'venv/bin/python'))
        source.with_suffix('.html').write_text('report')
        assets = source.with_name(source.stem + '_files')
        assets.mkdir()
        (assets / 'test.js').write_text('script')

    def test_publish_changes_one_output_link_and_cleans_local_render(self):
        with patch.object(report.subprocess, 'run', side_effect=self.generate):
            first = report.render(self.root)
            second = report.render(self.root)
        self.assertNotEqual(first, second)
        self.assertTrue((first / 'google-play-descritiva.html').is_file())
        self.assertEqual((self.root / 'analysis/output/google-play-descritiva.html').read_text(), 'report')
        self.assertEqual((self.root / 'analysis/output').resolve(), second.resolve())
        self.assertEqual(list((self.root / '.runtime/reports').glob('render-*')), [])
        for name in ('google-play-descritiva-output', 'google-play-descritiva.html', 'google-play-descritiva_files'):
            self.assertFalse((self.root / 'analysis' / name).is_symlink())
            self.assertFalse((self.root / 'analysis' / name).exists())

    def test_render_failure_keeps_previous_output_link(self):
        with patch.object(report.subprocess, 'run', side_effect=self.generate):
            first = report.render(self.root)
        with patch.object(report.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'quarto')):
            with self.assertRaises(subprocess.CalledProcessError):
                report.render(self.root)
        self.assertEqual((self.root / 'analysis/output').resolve(), first.resolve())

    def test_copy_hash_failure_keeps_previous_output_link(self):
        with patch.object(report.subprocess, 'run', side_effect=self.generate):
            first = report.render(self.root)
        original_hash = report.sha256
        def wrong_hash(path):
            if '.pending' in str(path):
                return 'wrong'
            return original_hash(path)
        with patch.object(report.subprocess, 'run', side_effect=self.generate), patch.object(report, 'sha256', side_effect=wrong_hash):
            with self.assertRaisesRegex(RuntimeError, 'Hash divergente'):
                report.render(self.root)
        self.assertEqual((self.root / 'analysis/output').resolve(), first.resolve())
        self.assertFalse(list(first.parent.glob('*.pending')))


if __name__ == '__main__':
    unittest.main()
