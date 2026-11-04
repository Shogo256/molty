"""The version is written in two places; a release needs them to agree."""

import tomllib
import unittest
from pathlib import Path

import molty

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


class Version(unittest.TestCase):
    @unittest.skipUnless(PYPROJECT.exists(), "needs the source tree")
    def test_the_package_and_pyproject_agree(self):
        with PYPROJECT.open("rb") as f:
            version = tomllib.load(f)["project"]["version"]
        self.assertEqual(molty.__version__, version)


if __name__ == "__main__":
    unittest.main()
