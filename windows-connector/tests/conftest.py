import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows-connector"))
sys.path.insert(0, str(ROOT / "mt5-connector"))
sys.path.insert(0, str(ROOT / "mt5-connector" / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RRMT5_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def vault():
    from rr_connector.vault import MemoryKeyring, Vault
    return Vault(MemoryKeyring())


@pytest.fixture
def store(home, vault):
    from rr_connector.store import Store
    s = Store(home / "config.db", vault)
    yield s
    s.close()
