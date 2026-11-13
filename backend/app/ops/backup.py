"""Encrypted PostgreSQL backup and verified restore (RR Trading One; works on Windows and Linux).

File format ``.rrbak`` (version 1):

    b"RRBAK1\\n" | uint32 manifest_len | manifest (UTF-8 JSON, plaintext but authenticated) | chunks...
    chunk = uint32 ct_len | 12-byte nonce | AES-256-GCM ciphertext+tag

* Payload: ``pg_dump --format=custom`` streamed and encrypted in 1 MiB chunks.
* Every chunk's associated data binds the manifest hash, the chunk index and a "last" flag, so a modified manifest,
  reordered, removed, appended or truncated chunks all fail authentication.
* Nonce = 8 random bytes per file + 4-byte chunk counter (never reused under one key).
* The manifest records app version, Alembic revision, per-table row counts and the plaintext SHA-256.
* The key (32 random bytes, base64) is separate from the engine secrets. **Without it a backup cannot be read**;
  keep an offline copy.

Restore never touches an existing database: it creates a NEW database, restores into it, and compares every
table's row count with the manifest. Switching the application to the restored database is a separate,
explicit step (see docs/BACKUP_AND_RECOVERY.md).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

MAGIC = b"RRBAK1\n"
CHUNK = 1 << 20
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class BackupError(Exception):
    pass


# ---- keys ------------------------------------------------------------------------------------------
def new_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def load_key(path: str | Path) -> bytes:
    raw = base64.b64decode(Path(path).read_text().strip())
    if len(raw) != 32:
        raise BackupError("backup key must be 32 bytes (base64)")
    return raw


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"rrbak-key-id" + key).hexdigest()[:16]


# ---- database helpers ----------------------------------------------------------------------------
@dataclass
class Conn:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_url(cls, url: str) -> "Conn":
        u = make_url(url)
        return cls(u.host or "localhost", int(u.port or 5432), u.username or "", u.password or "", u.database or "")

    def env(self) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith("RR_")}   # no app secrets to the tools
        e.update({"PGPASSWORD": self.password, "PGHOST": self.host, "PGPORT": str(self.port), "PGUSER": self.user})
        return e

    def sqlalchemy_url(self, database: str | None = None) -> str:
        return str(make_url("postgresql+psycopg://").set(username=self.user, password=self.password, host=self.host,
                                                          port=self.port, database=database or self.database)
                   .render_as_string(hide_password=False))


def pg_tool(name: str) -> str:
    """Locate pg_dump/pg_restore: RR_PG_BIN, then PATH (Windows adds .exe)."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    if os.environ.get("RR_PG_BIN"):
        p = Path(os.environ["RR_PG_BIN"]) / exe
        if p.is_file():
            return str(p)
    found = shutil.which(exe)
    if not found:
        raise BackupError(f"{exe} not found (set RR_PG_BIN to the PostgreSQL bin folder)")
    return found


def table_counts(url: str) -> dict[str, int]:
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            names = inspect(c).get_table_names(schema="public")
            return {n: int(c.execute(text(f'SELECT count(*) FROM public."{n}"')).scalar()) for n in sorted(names)}
    finally:
        eng.dispose()


def alembic_revision(url: str) -> str | None:
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - no alembic table
        return None
    finally:
        eng.dispose()


# ---- create ----------------------------------------------------------------------------------------
def _aad(manifest_hash: bytes, index: int, last: bool) -> bytes:
    return manifest_hash + struct.pack(">I?", index, last)


def create(database_url: str, key: bytes, out_dir: str | Path, label: str = "") -> Path:
    conn = Conn.from_url(database_url)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = table_counts(conn.sqlalchemy_url())
    from app import __version__ as app_version  # noqa: PLC0415
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="rrbak-") as tmp:
        dump = Path(tmp) / "db.dump"
        r = subprocess.run([pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges",
                            "--file", str(dump), conn.database], env=conn.env(), capture_output=True, text=True)
        if r.returncode != 0:
            raise BackupError(f"pg_dump failed: {r.stderr.strip()[:500]}")
        h = hashlib.sha256()
        with open(dump, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
        manifest = {"format": "rrbak", "version": 1, "created_at": stamp, "label": label[:80],
                    "app_version": app_version, "alembic_revision": alembic_revision(conn.sqlalchemy_url()),
                    "database": conn.database, "row_counts": counts, "plaintext_sha256": h.hexdigest(),
                    "plaintext_size": dump.stat().st_size, "encryption": {"alg": "AES-256-GCM", "chunk": CHUNK,
                                                                          "key_id": key_id(key)}}
        mbytes = json.dumps(manifest, sort_keys=True).encode()
        mhash = hashlib.sha256(mbytes).digest()
        prefix, aes = secrets.token_bytes(8), AESGCM(key)
        final = out_dir / f"rr_{conn.database}_{stamp}.rrbak"
        part = final.with_suffix(".rrbak.part")
        size = dump.stat().st_size
        with open(dump, "rb") as src, open(part, "wb") as dst:
            dst.write(MAGIC + struct.pack(">I", len(mbytes)) + mbytes)
            index, done = 0, 0
            while True:
                block = src.read(CHUNK)
                done += len(block)
                last = done >= size
                nonce = prefix + struct.pack(">I", index)
                ct = aes.encrypt(nonce, block, _aad(mhash, index, last))
                dst.write(struct.pack(">I", len(ct)) + nonce + ct)
                index += 1
                if last:
                    break
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(part, final)
    return final


# ---- read / verify ---------------------------------------------------------------------------------
def read_manifest(path: str | Path) -> dict:
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupError("not an RR Trading backup file")
        (n,) = struct.unpack(">I", f.read(4))
        return json.loads(f.read(n))


def decrypt_to(path: str | Path, key: bytes, out: str | Path) -> dict:
    """Authenticate and decrypt everything; raises before returning if any byte was altered."""
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupError("not an RR Trading backup file")
        (n,) = struct.unpack(">I", f.read(4))
        mbytes = f.read(n)
        manifest = json.loads(mbytes)
        if manifest.get("encryption", {}).get("key_id") != key_id(key):
            raise BackupError("this backup was made with a different backup key")
        mhash, aes, h = hashlib.sha256(mbytes).digest(), AESGCM(key), hashlib.sha256()
        index, saw_last = 0, False
        with open(out, "wb") as dst:
            while True:
                head = f.read(4)
                if not head:
                    break
                if saw_last:
                    raise BackupError("data after the final chunk")
                (ln,) = struct.unpack(">I", head)
                nonce, ct = f.read(12), f.read(ln)
                if len(ct) != ln or len(nonce) != 12:
                    raise BackupError("backup is truncated")
                pt = None
                for last in (False, True):
                    try:
                        pt = aes.decrypt(nonce, ct, _aad(mhash, index, last))
                        saw_last = last
                        break
                    except Exception:  # noqa: BLE001 - try the other flag, then fail
                        continue
                if pt is None:
                    raise BackupError(f"chunk {index} failed authentication (wrong key or tampered backup)")
                h.update(pt)
                dst.write(pt)
                index += 1
        if not saw_last:
            raise BackupError("backup is truncated (final chunk missing)")
        if h.hexdigest() != manifest["plaintext_sha256"]:
            raise BackupError("checksum mismatch after decryption")
    return manifest


def verify(path: str | Path, key: bytes) -> dict:
    with tempfile.TemporaryDirectory(prefix="rrbak-") as tmp:
        return decrypt_to(path, key, Path(tmp) / "db.dump")


# ---- restore ---------------------------------------------------------------------------------------
def restore(path: str | Path, key: bytes, database_url: str, target_db: str) -> dict:
    """Restore into a NEW database ``target_db`` and validate row counts against the manifest."""
    if not NAME_RE.match(target_db):
        raise BackupError("target database name must be lowercase letters, digits and underscores")
    conn = Conn.from_url(database_url)
    if target_db == conn.database:
        raise BackupError("refusing to restore over the database in use; choose a new database name")
    admin = create_engine(conn.sqlalchemy_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            if c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target_db}).scalar():
                raise BackupError(f"database {target_db} already exists; restore only into a new database")
            c.execute(text(f'CREATE DATABASE "{target_db}"'))
    finally:
        admin.dispose()
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="rrbak-") as tmp:
        dump = Path(tmp) / "db.dump"
        manifest = decrypt_to(path, key, dump)
        r = subprocess.run([pg_tool("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error",
                            "--dbname", target_db, str(dump)], env=conn.env(), capture_output=True, text=True)
        if r.returncode != 0:
            raise BackupError(f"pg_restore failed: {r.stderr.strip()[:500]}")
    restored = table_counts(conn.sqlalchemy_url(target_db))
    expected = manifest["row_counts"]
    mismatches = {t: {"expected": expected.get(t), "restored": restored.get(t)}
                  for t in set(expected) | set(restored) if expected.get(t) != restored.get(t)}
    report = {"target_database": target_db, "tables": len(restored), "rows": sum(restored.values()),
              "alembic_revision": alembic_revision(conn.sqlalchemy_url(target_db)),
              "manifest_alembic_revision": manifest.get("alembic_revision"), "mismatches": mismatches,
              "ok": not mismatches, "seconds": round(time.time() - t0, 1)}
    if mismatches:
        raise BackupError(f"restored row counts differ from the backup manifest: {mismatches}")
    return report


def prune(out_dir: str | Path, keep: int) -> list[str]:
    files = sorted(Path(out_dir).glob("*.rrbak"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for p in files[max(keep, 1):]:
        p.unlink()
        removed.append(p.name)
    return removed


# ---- CLI -------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ops.backup", description="Encrypted backup and verified restore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen", help="create a new backup key file (refuses to overwrite)")
    kg.add_argument("--key-file", required=True)
    for name in ("create", "verify", "restore", "manifest", "prune"):
        p = sub.add_parser(name)
        if name in ("create", "prune"):
            p.add_argument("--out-dir", required=True)
        if name in ("verify", "restore", "manifest"):
            p.add_argument("--file", required=True)
        if name in ("create", "verify", "restore"):
            p.add_argument("--key-file", required=True)
        if name == "create":
            p.add_argument("--label", default="")
            p.add_argument("--keep", type=int, default=0, help="also prune to the newest N backups")
        if name == "restore":
            p.add_argument("--target-db", required=True)
        if name == "prune":
            p.add_argument("--keep", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "keygen":
            kf = Path(a.key_file)
            if kf.exists():
                raise BackupError(f"{kf} exists; refusing to overwrite a backup key")
            kf.parent.mkdir(parents=True, exist_ok=True)
            kf.write_text(new_key())
            print(json.dumps({"key_file": str(kf), "key_id": key_id(load_key(kf))}))
        elif a.cmd == "create":
            from app.core.config import get_settings
            path = create(get_settings().database_url, load_key(a.key_file), a.out_dir, a.label)
            out = {"file": str(path), "manifest": read_manifest(path)}
            if a.keep:
                out["pruned"] = prune(a.out_dir, a.keep)
            print(json.dumps(out, default=str))
        elif a.cmd == "verify":
            m = verify(a.file, load_key(a.key_file))
            print(json.dumps({"ok": True, "created_at": m["created_at"], "alembic_revision": m["alembic_revision"],
                              "tables": len(m["row_counts"]), "rows": sum(m["row_counts"].values())}))
        elif a.cmd == "manifest":
            print(json.dumps(read_manifest(a.file), indent=2))
        elif a.cmd == "restore":
            from app.core.config import get_settings
            print(json.dumps(restore(a.file, load_key(a.key_file), get_settings().database_url, a.target_db)))
        elif a.cmd == "prune":
            print(json.dumps({"removed": prune(a.out_dir, a.keep)}))
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
