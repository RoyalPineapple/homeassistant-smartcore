"""Run the same tests without installing pytest (Python 3.12+).

HA tests are explicitly skipped unless a real Home Assistant is installed.
"""

import importlib
from pathlib import Path
import sys
import unittest

suite = unittest.TestSuite()
loader = unittest.TestLoader()
for path in sorted(Path(__file__).parent.glob("test_*.py")):
    try:
        module = importlib.import_module(path.stem)
    except unittest.SkipTest as err:
        @unittest.skip(str(err))
        def missing_dependency():
            pass
        suite.addTest(unittest.FunctionTestCase(missing_dependency, description=path.stem))
        continue
    suite.addTests(loader.loadTestsFromModule(module))
    for name, function in vars(module).items():
        if name.startswith("test_") and callable(function):
            suite.addTest(unittest.FunctionTestCase(function))

result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
