"""Identify this workspace without exposing its filesystem path over HTTP."""

import hashlib
import os
from pathlib import Path

WORKSPACE_ID = hashlib.sha256(
    os.path.normcase(str(Path(__file__).resolve().parent.parent)).encode()
).hexdigest()
