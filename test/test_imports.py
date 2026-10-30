import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / 'epubcast'
# Modules of this package. Imported bare (`import core`) they only resolve when the package
# directory happens to be on sys.path -- which it was, via a sys.path.append in __init__.py.
# That hack is gone, so a bare import is now a crash waiting for the code path to run.
MODULE_NAMES = {p.stem for p in PACKAGE.glob('*.py')} - {'__init__'}


class NoBareIntraPackageImportsTest(unittest.TestCase):
    """`import core` inside epubcast/ raises ModuleNotFoundError at runtime.

    It bit cli.py (`from core import main`) and ui.py (`import core` inside
    CoreThread.run, which only fires when synthesis starts, so it survived import-time
    checks and a GUI smoke test).
    """

    def offenders(self):
        found = []
        for path in sorted(PACKAGE.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split('.')[0] in MODULE_NAMES:
                            found.append(f'{path.name}:{node.lineno}: import {alias.name}')
                elif isinstance(node, ast.ImportFrom):
                    # level > 0 is an explicit relative import, which is fine.
                    if node.level == 0 and node.module and node.module.split('.')[0] in MODULE_NAMES:
                        found.append(f'{path.name}:{node.lineno}: from {node.module} import ...')
        return found

    def test_no_module_is_imported_under_a_bare_name(self):
        offenders = self.offenders()
        self.assertEqual(offenders, [], 'Use "epubcast.<module>" instead:\n  ' + '\n  '.join(offenders))

    def test_the_check_can_actually_see_the_modules(self):
        # Guard against the scan silently passing because it found nothing to scan.
        self.assertIn('core', MODULE_NAMES)
        self.assertIn('ui', MODULE_NAMES)


class EntryPointsImportTest(unittest.TestCase):
    """Every module the console scripts touch must import cleanly."""

    def test_core_imports(self):
        import epubcast.core  # noqa: F401

    def test_cli_imports(self):
        import epubcast.cli  # noqa: F401

    def test_backends_imports(self):
        import epubcast.backends  # noqa: F401


if __name__ == '__main__':
    unittest.main()
