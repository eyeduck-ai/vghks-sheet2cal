"""Portable credential envelopes; the decoding key travels in the same database.

This avoids plaintext in ordinary database inspection, but deliberately does
not protect credentials from someone who possesses the database. No machine,
Windows account, external key file or master password is required.
"""
from cryptography.fernet import Fernet, InvalidToken

PREFIX = b"VGHKS-PORTABLE-1\x00"


def is_portable(value):
    return value is not None and bytes(value).startswith(PREFIX)


def seal(db, value):
    db.execute("CREATE TABLE IF NOT EXISTS bot_credential_key (id INTEGER PRIMARY KEY CHECK(id=1), key BLOB NOT NULL)")
    db.execute("INSERT OR IGNORE INTO bot_credential_key VALUES(1,?)", (Fernet.generate_key(),))
    key = bytes(db.execute("SELECT key FROM bot_credential_key WHERE id=1").fetchone()[0])
    return PREFIX + Fernet(key).encrypt(value)


def unseal(db, value):
    if not is_portable(value):
        raise ValueError("登入資訊格式不受支援，請重新輸入或匯入。")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_credential_key'").fetchone():
        raise ValueError("可攜式登入資訊不完整，請重新輸入或匯入。")
    row = db.execute("SELECT key FROM bot_credential_key WHERE id=1").fetchone()
    if not row:
        raise ValueError("可攜式登入資訊不完整，請重新輸入或匯入。")
    try:
        return Fernet(bytes(row[0])).decrypt(bytes(value)[len(PREFIX):])
    except (InvalidToken, ValueError) as exc:
        raise ValueError("可攜式登入資訊無法讀取，請重新輸入或匯入。") from exc
