"""Encrypted backup and verified restore (RR Trading One, Phase 1)."""
import os

import pytest
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.ops import backup as b


@pytest.fixture
def key(tmp_path):
    kf = tmp_path / "backup.key"
    assert b.main(["keygen", "--key-file", str(kf)]) == 0
    assert b.main(["keygen", "--key-file", str(kf)]) == 1          # never overwrites a key
    return b.load_key(kf), kf


@pytest.fixture
def target_db():
    name = "rr_restore_check"
    yield name
    conn = b.Conn.from_url(get_settings().database_url)
    eng = create_engine(conn.sqlalchemy_url("postgres"), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))   # test-only cleanup
    eng.dispose()


def test_backup_is_encrypted_verifiable_and_restores_with_matching_counts(db_clean, key, tmp_path, target_db):
    k, _ = key
    url = get_settings().database_url
    path = b.create(url, k, tmp_path / "bk", label="test")
    m = b.read_manifest(path)
    assert m["alembic_revision"] and m["row_counts"]["strategies"] == 215 and m["encryption"]["key_id"] == b.key_id(k)
    raw = path.read_bytes()
    assert b"PGDMP" not in raw and b"quant_martingale" not in raw            # payload is not readable
    assert b.verify(path, k)["plaintext_sha256"] == m["plaintext_sha256"]
    rep = b.restore(path, k, url, target_db)
    assert rep["ok"] and rep["alembic_revision"] == m["alembic_revision"]
    assert rep["rows"] == sum(m["row_counts"].values())


def test_tampering_wrong_key_truncation_and_overwrite_are_refused(db_clean, key, tmp_path):
    k, _ = key
    url = get_settings().database_url
    path = b.create(url, k, tmp_path / "bk")
    data = bytearray(path.read_bytes())
    bad = tmp_path / "tampered.rrbak"
    data[-100] ^= 0xFF
    bad.write_bytes(bytes(data))
    with pytest.raises(b.BackupError, match="authentication"):
        b.verify(bad, k)
    with pytest.raises(b.BackupError, match="different backup key"):
        b.verify(path, os.urandom(32))
    cut = tmp_path / "cut.rrbak"
    cut.write_bytes(path.read_bytes()[:-50])
    with pytest.raises(b.BackupError, match="truncated|authentication"):
        b.verify(cut, k)
    # editing the plaintext manifest (e.g. faking row counts) breaks every chunk's authentication
    import json
    import struct
    raw = path.read_bytes()
    n = struct.unpack(">I", raw[7:11])[0]
    m = json.loads(raw[11:11 + n])
    m["row_counts"]["strategies"] = 1
    mb = json.dumps(m, sort_keys=True).encode()
    forged = tmp_path / "forged.rrbak"
    forged.write_bytes(raw[:7] + struct.pack(">I", len(mb)) + mb + raw[11 + n:])
    with pytest.raises(b.BackupError, match="authentication"):
        b.verify(forged, k)
    with pytest.raises(b.BackupError, match="in use"):
        b.restore(path, k, url, b.Conn.from_url(url).database)
    with pytest.raises(b.BackupError, match="already exists"):
        b.restore(path, k, url, "postgres")


def test_pg_password_never_on_command_line(monkeypatch, db_clean, key, tmp_path):
    k, _ = key
    calls = []
    real = b.subprocess.run
    monkeypatch.setattr(b.subprocess, "run", lambda args, **kw: calls.append((args, kw)) or real(args, **kw))
    b.create(get_settings().database_url, k, tmp_path / "bk")
    pw = b.Conn.from_url(get_settings().database_url).password
    args, kw = calls[0]
    assert pw not in args and not any(a.startswith("--password") for a in args) and kw["env"]["PGPASSWORD"] == pw
    assert not any(x.startswith("RR_") for x in kw["env"])


def test_prune_keeps_newest(tmp_path):
    import time
    for i in range(4):
        (tmp_path / f"f{i}.rrbak").write_bytes(b"x")
        os.utime(tmp_path / f"f{i}.rrbak", (time.time() + i, time.time() + i))
    assert sorted(b.prune(tmp_path, 2)) == ["f0.rrbak", "f1.rrbak"]
