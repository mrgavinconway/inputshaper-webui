"""Configure an isolated app before test-module collection, in any file order."""
import os
import tempfile


_test_data = tempfile.TemporaryDirectory(prefix='inputshaper-tests-', ignore_cleanup_errors=True)
os.environ['INPUTSHAPER_PASSWORD'] = 'test-password'
os.environ['INPUTSHAPER_DATA'] = _test_data.name
