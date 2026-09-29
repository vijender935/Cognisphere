"""Single-account authentication for the Personal AI Assistant."""
from __future__ import annotations
import base64,hashlib,hmac,os,secrets,time
from datetime import datetime,timezone
from db import connect

SESSION_COOKIE="pa_session"
SESSION_DAYS=max(1,int(os.getenv("AUTH_SESSION_DAYS","7")))
PBKDF2_ITERATIONS=max(100_000,int(os.getenv("AUTH_PBKDF2_ITERATIONS","310000")))
def _now(): return int(time.time())
def _hash_password(password):
    if len(password)<8: raise ValueError("Password must be at least 8 characters.")
    salt=secrets.token_bytes(16); digest=hashlib.pbkdf2_hmac("sha256",password.encode(),salt,PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%s$%s$%s"%(PBKDF2_ITERATIONS,base64.urlsafe_b64encode(salt).decode(),base64.urlsafe_b64encode(digest).decode())
def _verify_password(password,encoded):
    try:
        scheme,iterations,salt_b64,digest_b64=encoded.split("$",3)
        if scheme!="pbkdf2_sha256": return False
        actual=hashlib.pbkdf2_hmac("sha256",password.encode(),base64.urlsafe_b64decode(salt_b64),int(iterations))
        return hmac.compare_digest(actual,base64.urlsafe_b64decode(digest_b64))
    except (ValueError,TypeError): return False
def _token_hash(token): return hashlib.sha256(token.encode()).hexdigest()
def init_auth_db():
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY,display_name TEXT NOT NULL,email TEXT NOT NULL,password_hash TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS auth_login_attempts(
            bucket TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        )""")
        con.execute("DELETE FROM auth_sessions WHERE expires_at<=?",(_now(),))
        con.execute("DELETE FROM auth_login_attempts WHERE updated_at<?",(_now()-900,))
def account_exists():
    init_auth_db()
    with connect() as con: return bool(con.execute("SELECT id FROM account WHERE id=1").fetchone())
def account_public(row=None):
    if row is None:
        with connect() as con: row=con.execute("SELECT id,display_name,email,created_at,updated_at FROM account WHERE id=1").fetchone()
    if not row:return {}
    return {"id":int(row[0]),"display_name":row[1],"email":row[2],"created_at":row[3],"updated_at":row[4]}
def setup_account(display_name,email,password):
    display_name=display_name.strip();email=email.strip().lower()
    if not display_name: raise ValueError("Name is required.")
    if "@" not in email or len(email)>254: raise ValueError("Enter a valid email address.")
    if account_exists(): raise RuntimeError("An account is already configured.")
    now=datetime.now(timezone.utc).isoformat()
    with connect() as con: con.execute("INSERT INTO account(id,display_name,email,password_hash,created_at,updated_at) VALUES(1,?,?,?,?,?)",(display_name,email,_hash_password(password),now,now))
    return account_public()

def login_rate_limited(key: str, limit: int = 8, window_seconds: int = 900) -> bool:
    """Atomically rate-limit login attempts in PostgreSQL across all app instances."""
    now = _now()
    bucket = f"{key}:{now // window_seconds}"
    with connect() as con:
        row = con.execute(
            """INSERT INTO auth_login_attempts(bucket, attempts, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(bucket) DO UPDATE SET
                   attempts=auth_login_attempts.attempts+1,
                   updated_at=excluded.updated_at
               RETURNING attempts""",
            (bucket, 1, now),
        ).fetchone()
    return bool(row and int(row[0]) > limit)


def clear_login_rate_limit(key: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM auth_login_attempts WHERE bucket LIKE ?", (f"{key}:%",))

def login(email,password):
    init_auth_db();email=email.strip().lower()
    with connect() as con: row=con.execute("SELECT id,display_name,email,password_hash,created_at,updated_at FROM account WHERE id=1").fetchone()
    if not row or row[2].lower()!=email or not _verify_password(password,row[3]): return None
    token=secrets.token_urlsafe(48);now=_now()
    with connect() as con: con.execute("INSERT INTO auth_sessions(token_hash,created_at,expires_at) VALUES(?,?,?)",(_token_hash(token),now,now+SESSION_DAYS*86400))
    return token,account_public(row)
def get_account_for_session(token):
    if not token:return None
    with connect() as con:
        row=con.execute("SELECT a.id,a.display_name,a.email,a.created_at,a.updated_at FROM auth_sessions s JOIN account a ON a.id=1 WHERE s.token_hash=? AND s.expires_at>?",(_token_hash(token),_now())).fetchone()
    return account_public(row) if row else None
def logout(token):
    if token:
        with connect() as con: con.execute("DELETE FROM auth_sessions WHERE token_hash=?",(_token_hash(token),))
def update_account(token,display_name,email):
    if not get_account_for_session(token): raise PermissionError("Not authenticated.")
    display_name=display_name.strip();email=email.strip().lower()
    if not display_name: raise ValueError("Name is required.")
    if "@" not in email or len(email)>254: raise ValueError("Enter a valid email address.")
    now=datetime.now(timezone.utc).isoformat()
    with connect() as con: con.execute("UPDATE account SET display_name=?,email=?,updated_at=? WHERE id=1",(display_name,email,now))
    return account_public()
def change_password(token,current_password,new_password):
    if not get_account_for_session(token): raise PermissionError("Not authenticated.")
    with connect() as con: row=con.execute("SELECT password_hash FROM account WHERE id=1").fetchone()
    if not row or not _verify_password(current_password,row[0]): raise ValueError("Current password is incorrect.")
    encoded=_hash_password(new_password)
    with connect() as con:
        con.execute("UPDATE account SET password_hash=?,updated_at=? WHERE id=1",(encoded,datetime.now(timezone.utc).isoformat()))
        con.execute("DELETE FROM auth_sessions WHERE token_hash<>?",(_token_hash(token),))
