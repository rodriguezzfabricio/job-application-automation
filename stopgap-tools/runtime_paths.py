"""One explicit private-state root, independent of the source checkout."""
import os
from pathlib import Path

PRIVATE = os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or str(Path(__file__).resolve().parents[1] / 'stopgap-private')
if not Path(PRIVATE).is_absolute():
    raise ValueError('JOB_AUTOMATION_PRIVATE_ROOT must be an absolute directory')
PRIVATE = str(Path(PRIVATE).resolve())
