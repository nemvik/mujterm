from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from mujterm import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_metadata_matches_package_version(self) -> None:
        control = (ROOT / "packaging" / "control").read_text(encoding="utf-8")
        control_version = next(
            line.removeprefix("Version: ")
            for line in control.splitlines()
            if line.startswith("Version: ")
        )
        releases = ET.parse(
            ROOT / "data" / "io.github.viktornemcok.MujTerm.metainfo.xml"
        ).findall("./releases/release")

        self.assertEqual(control_version, __version__)
        self.assertTrue(releases)
        self.assertEqual(releases[0].attrib["version"], __version__)

    def test_python_package_uses_the_central_version_attribute(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('version = {attr = "mujterm.__version__"}', pyproject)


if __name__ == "__main__":
    unittest.main()
