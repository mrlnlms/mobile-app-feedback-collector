"""Testes do render temporário de analysis/render.py.

Uso: venv/bin/python -m unittest discover -s analysis -p "test_*.py"
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import render
from scripts.reports import render_google_play as report


class RenderQmdTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = (Path(temp.name) / "repo").resolve()
        (self.root / "analysis").mkdir(parents=True)
        drive = Path(temp.name) / "drive"
        drive.mkdir()
        (self.root / "private").symlink_to(drive, target_is_directory=True)
        for name in ("google-play-descritiva", "novo"):
            (self.root / f"analysis/{name}.qmd").write_text("---\ntitle: T\n---\n")
        self.seen = {}

    def generate(self, args, **kwargs):
        self.seen["pythonpath"] = kwargs["env"]["PYTHONPATH"]
        source = Path(args[2])
        source.with_suffix(".html").write_text(source.stem + " report")
        assets = source.with_name(source.stem + "_files")
        assets.mkdir()
        (assets / "test.js").write_text("script")

    def test_new_qmd_gets_own_link_and_leaves_registry_unchanged(self):
        before = dict(report.REPORTS)
        with patch.object(report.subprocess, "run", side_effect=self.generate):
            published = render.render_qmd("analysis/novo.qmd", self.root)
        link = self.root / "analysis/novo-output"
        self.assertEqual(link.resolve(), published.resolve())
        self.assertEqual((link / "novo.html").read_text(), "novo report")
        self.assertIn(str(self.root / "analysis"), self.seen["pythonpath"].split(":"))
        self.assertEqual(report.REPORTS, before)

    def test_known_report_keeps_original_link(self):
        with patch.object(report.subprocess, "run", side_effect=self.generate):
            published = render.render_qmd("google-play-descritiva.qmd", self.root)
        self.assertEqual((self.root / "analysis/output").resolve(), published.resolve())

    def test_missing_qmd_fails_before_publishing(self):
        with self.assertRaises(FileNotFoundError):
            render.render_qmd("analysis/inexistente.qmd", self.root)
        self.assertFalse((self.root / "analysis/inexistente-output").exists())

    def test_render_failure_keeps_previous_link_and_registry(self):
        before = dict(report.REPORTS)
        with patch.object(report.subprocess, "run", side_effect=self.generate):
            first = render.render_qmd("novo", self.root)
        with patch.object(report.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "quarto")):
            with self.assertRaises(subprocess.CalledProcessError):
                render.render_qmd("novo", self.root)
        self.assertEqual((self.root / "analysis/novo-output").resolve(), first.resolve())
        self.assertEqual(report.REPORTS, before)


if __name__ == "__main__":
    unittest.main()
