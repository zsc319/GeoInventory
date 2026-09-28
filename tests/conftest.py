import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The application resolves its data directory when imported.  Keep tests away
# from a real user's active workspace and any stale local project snapshot.
_test_data_dir = tempfile.TemporaryDirectory(prefix="geoinventory-tests-")
os.environ.setdefault("GEOINVENTORY_DATA_DIR", _test_data_dir.name)
