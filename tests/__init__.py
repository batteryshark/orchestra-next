"""Keep every test inside an isolated Orchestra-next and DSH home."""
import atexit
import os
import shutil
import tempfile

for _name in [name for name in os.environ if name.startswith("ORCHESTRA_NEXT_")]:
    del os.environ[_name]

_ROOT = tempfile.mkdtemp(prefix="orchestra-next-tests-")
os.environ["ORCHESTRA_NEXT_HOME"] = os.path.join(_ROOT, "state")
os.environ["DSH_HOME"] = os.path.join(_ROOT, "dsh")
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
