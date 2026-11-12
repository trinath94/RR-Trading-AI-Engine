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


@pytest.fixture(autouse=True)
def _close_qt_widgets():
    """Close and delete every top-level Qt widget after each test so nothing is torn down at interpreter exit."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for w in QApplication.topLevelWidgets():
        w.hide()
        w.deleteLater()
    app.processEvents()


_EXIT = {"status": 0}


def pytest_sessionfinish(session, exitstatus):
    _EXIT["status"] = int(exitstatus)


def pytest_unconfigure(config):
    # PySide6 on Windows can crash during interpreter finalisation after all tests passed, turning a green run
    # red. Once pytest has printed its summary, exit with pytest's own result and skip that teardown.
    if sys.platform == "win32" and "PySide6.QtWidgets" in sys.modules:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_EXIT["status"])
