"""Persistent application preferences for the single personal assistant."""
from __future__ import annotations
import json
from config import ensure_directories
from db import connect

DEFAULTS={"appearance":"System","haptics":True,"language":"English","web_search":True,"memory":True}

def init_preferences_db():
    ensure_directories()
    with connect() as con:
        # Remove the legacy account-scoped preferences table.
        try: con.execute("DROP TABLE IF EXISTS user_preferences")
        except Exception: pass
        con.execute("""CREATE TABLE IF NOT EXISTS preferences(
            id INTEGER PRIMARY KEY CHECK(id=1), preferences TEXT NOT NULL DEFAULT '{}')""")

def _clean(data):
    result=dict(DEFAULTS)
    if isinstance(data,dict):
        for key in DEFAULTS:
            if key in data: result[key]=data[key]
    result["appearance"]=result["appearance"] if result["appearance"] in {"System","Light","Dark"} else "System"
    result["language"]=result["language"] if result["language"] in {"English","Hindi"} else "English"
    result["haptics"]=bool(result["haptics"]); result["web_search"]=bool(result["web_search"]); result["memory"]=bool(result["memory"])
    return result

def get_preferences():
    init_preferences_db()
    with connect() as con: row=con.execute("SELECT preferences FROM preferences WHERE id=1").fetchone()
    if not row:return dict(DEFAULTS)
    try: raw=json.loads(row[0])
    except (TypeError,json.JSONDecodeError): raw={}
    return _clean(raw)

def update_preferences(updates):
    current=get_preferences(); current.update({k:v for k,v in updates.items() if k in DEFAULTS}); cleaned=_clean(current)
    with connect() as con:
        con.execute("""INSERT INTO preferences(id,preferences) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET preferences=excluded.preferences""",(json.dumps(cleaned,ensure_ascii=False),))
    return cleaned
