import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "check_version.py"


@unittest.skipIf(shutil.which("git") is None, "gitがありません")
class CheckVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "src" / "nicojk_ass").mkdir(parents=True)
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root)] + list(args), check=True, capture_output=True)

    def run_check(self, version, tags=()):
        (self.root / "src" / "nicojk_ass" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        for tag in tags:
            self.git("tag", tag)
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)],
                              capture_output=True, text=True, encoding="utf-8")

    def test_正しい版は通り版を出力する(self):
        result = self.run_check("0.02", ["0.01", "v9", "latest"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0.02")

    def test_最初の版はタグがなくても通る(self):
        self.assertEqual(self.run_check("0.01").returncode, 0)

    def test_整数部が上がる版は通る(self):
        self.assertEqual(self.run_check("1.00", ["0.99"]).returncode, 0)

    def test_形の誤りはエラー(self):
        for bad in ["0.1", "0.001", "1", "v0.01", "0.01.1", "", "0.1.0"]:
            with self.subTest(bad=bad):
                result = self.run_check(bad)
                self.assertEqual(result.returncode, 1)
                self.assertIn("wrong form", result.stderr)

    def test_既存のタグと同じ版はエラー(self):
        result = self.run_check("0.03", ["0.03"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)

    def test_既存の最大より小さい版はエラー(self):
        result = self.run_check("0.02", ["0.01", "0.10"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not greater", result.stderr)

    def test_数としての大小で比べる(self):
        self.assertEqual(self.run_check("0.10", ["0.09"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
