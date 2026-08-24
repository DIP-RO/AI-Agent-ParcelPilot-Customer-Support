import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import actions, datastore  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    datastore.reset_overlay()
    actions.reset_log()
    yield
    datastore.reset_overlay()
    actions.reset_log()
