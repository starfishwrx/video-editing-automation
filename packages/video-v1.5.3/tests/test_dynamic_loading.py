"""Adapter inventory checks without importing production modules."""
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from tools import check_dynamic_loading as guard

class DynamicLoadingTests(unittest.TestCase):
    def test_actual_adapters_match_inventory(self):
        self.assertEqual(guard.check(), [])

    def test_aliases_and_nested_calls_are_detected(self):
        self.assertEqual(guard.calls('''
import importlib as il
from importlib.util import spec_from_file_location as load
import runpy as runner
from builtins import __import__ as imp
def local():
    il.import_module('anything')
    load('anything', path)
    runner.run_path(path)
    imp('anything')
    spec.loader.exec_module(module)
'''), Counter({'importlib.import_module': 1, 'importlib.util.spec_from_file_location': 1,
              'runpy.run_path': 1, 'builtins.__import__': 1, 'loader.exec_module': 1}))

    def test_comments_strings_and_static_imports_are_not_dynamic(self):
        self.assertFalse(guard.calls('import importlib\n# importlib.import_module("x")\ns="runpy.run_path(x)"'))

    def test_import_alias_shadowing_does_not_hide_loader(self):
        source = "import importlib as loader\ndef unrelated():\n    import json as loader\nloader.import_module('engine')"
        self.assertEqual(guard.calls(source), Counter({'importlib.import_module': 1}))

    def test_executable_compatibility_fallback_is_detected(self):
        self.assertEqual(guard.calls("exec(compile(source, filename, 'exec'), globals())"), Counter({'exec': 1}))

    def fixture(self, directory):
        root = Path(directory) / 'package'; root.mkdir()
        path = root / 'adapter.py'
        path.write_text("import importlib\nimportlib.import_module('local_backend')\n", encoding='utf-8')
        inventory = Path(directory) / 'inventory.json'
        inventory.write_text(json.dumps({'adapters': [{'path': 'adapter.py',
            'purpose': 'standalone adapter', 'verification': 'test_adapter.py',
            'calls': {'importlib.import_module': 1}}]}), encoding='utf-8')
        return root, path, inventory

    def test_new_business_loader_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, inventory = self.fixture(directory)
            (root / 'business.py').write_text("from importlib import import_module as load\nload('engine')")
            self.assertEqual(guard.check(root, inventory), ['DYNAMIC_LOADING_CHANGED:business.py'])

    def test_existing_adapter_cannot_gain_another_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root, path, inventory = self.fixture(directory)
            path.write_text(path.read_text() + "importlib.import_module('other')\n")
            self.assertEqual(guard.check(root, inventory), ['DYNAMIC_LOADING_CHANGED:adapter.py'])

    def test_removed_adapter_requires_inventory_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root, path, inventory = self.fixture(directory); path.unlink()
            self.assertEqual(guard.check(root, inventory), ['DYNAMIC_LOADING_CHANGED:adapter.py'])

    def test_test_harness_and_vendor_are_separate_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, inventory = self.fixture(directory)
            for name in ('tests', 'vendor'):
                (root / name).mkdir()
                (root / name / 'fixture.py').write_text("__import__('anything')")
            self.assertEqual(guard.check(root, inventory), [])

    def test_inventory_requires_explanation_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, inventory = self.fixture(directory)
            value = json.loads(inventory.read_text()); value['adapters'][0]['purpose'] = ''
            inventory.write_text(json.dumps(value))
            self.assertIn('DYNAMIC_INVENTORY_INVALID:adapter.py', guard.check(root, inventory))

if __name__ == '__main__':
    unittest.main()
