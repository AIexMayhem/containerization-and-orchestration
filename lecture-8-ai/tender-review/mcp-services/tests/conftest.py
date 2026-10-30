import shutil
from pathlib import Path

import pytest


@pytest.fixture
def data(tmp_path):
    source = Path(__file__).resolve().parents[2] / "fixtures"
    return Path(shutil.copytree(source, tmp_path / "fixtures"))
