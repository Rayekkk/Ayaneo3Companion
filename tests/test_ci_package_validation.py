"""Run the workflow's import setup as a temporary Actions-style script."""
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class PackageWorkflowTests(unittest.TestCase):
    def test_temporary_python_step_can_import_repository_validator(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/checks.yml").read_text(encoding="utf-8")
        step = workflow.split("      - name: Verify archive integrity and contents\n", 1)[1]
        code = textwrap.dedent(step.split("        run: |\n", 1)[1].split(
            "      - name: Upload verified plugin ZIP\n", 1)[0])
        setup = code.split("manifest = ", 1)[0]
        with tempfile.TemporaryDirectory(prefix="aya3-actions-python-") as directory:
            script = Path(directory) / "step.py"
            script.write_text(setup + "from lego_updater import Updater\n"
                              "assert callable(Updater._validate_archive)\n", encoding="utf-8")
            # Isolated mode prevents PYTHONPATH or the caller's interpreter
            # from making this pass without the actual workflow setup.
            result = subprocess.run([sys.executable, "-I", str(script)], cwd=root,
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
