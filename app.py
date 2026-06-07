from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import sqlite3
import hashlib
import requests
import base64
import os
from datetime import datetime, timedelta
import secrets
import io
import csv
from pathlib import Path
import json
import re
import time

app = FastAPI(title="MTG Price Tracker")

SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400)

GITHUB_REPO    = os.environ.get("GITHUB_REPO", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
ADMIN_USER     = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS     = os.environ.get("ADMIN_PASS", "changeme")
CACHE_MINUTES  = int(os.environ.get("CACHE_MINUTES", "30"))

DB_PATH  = "mtg_webapp.db"
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT UNIQUE NOT NULL,
            password_hash   TEXT NOT NULL,
            telegram_chat_id TEXT,
            is_admin        INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            card_key         TEXT NOT NULL,
            card_name        TEXT NOT NULL,
            set_code         TEXT DEFAULT '',
            set_name         TEXT DEFAULT '',
            collector_number TEXT DEFAULT '',
            foil             INTEGER DEFAULT 0,
            finish           TEXT DEFAULT 'normal',
            language         TEXT DEFAULT 'en',
            frame_effects    TEXT DEFAULT '',
            price            REAL NOT NULL,
            recorded_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sold_cards (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            card_key         TEXT NOT NULL,
            card_name        TEXT NOT NULL,
            set_code         TEXT DEFAULT '',
            set_name         TEXT DEFAULT '',
            collector_number TEXT DEFAULT '',
            finish           TEXT DEFAULT 'normal',
            language         TEXT DEFAULT 'en',
            frame_effects    TEXT DEFAULT '',
            last_price       REAL DEFAULT 0,
            sold_at          TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at  TEXT DEFAULT (datetime('now')),
            cards_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ph_key ON price_history(card_key);
    """)
    # Safe migration for existing DBs
    for col in [
        "ALTER TABLE price_history ADD COLUMN finish TEXT DEFAULT 'normal'",
        "ALTER TABLE price_history ADD COLUMN language TEXT DEFAULT 'en'",
        "ALTER TABLE price_history ADD COLUMN frame_effects TEXT DEFAULT ''",
        "ALTER TABLE price_history ADD COLUMN collection TEXT DEFAULT ''",
        "ALTER TABLE sold_cards ADD COLUMN collection TEXT DEFAULT ''",
        "ALTER TABLE fetch_log ADD COLUMN collection TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN collection TEXT DEFAULT ''",
        "ALTER TABLE price_history ADD COLUMN quantity INTEGER DEFAULT 1",
    ]:
        try:
            conn.execute(col)
        except Exception:
            pass
    conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
        (ADMIN_USER, _hash(ADMIN_PASS))
    )
    conn.commit()
    conn.close()


def _hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


# ── GitHub fetch ──────────────────────────────────────────────────────────────

def _fetch_github(collection="") -> dict:
    fname = _cf("prezzi_riferimento.json", collection)
    if GITHUB_REPO:
        headers = {"Accept": "application/vnd.github.v3.raw"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/{fname}"
            try:
                r = requests.get(url, headers=headers, timeout=15)
                if r.ok:
                    return r.json()
            except Exception:
                continue

    local = BASE_DIR / fname
    if local.exists():
        try:
            import json as _json
            with open(local, "r", encoding="utf-8") as f:
                data = _json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def get_prices(force: bool = False, collection: str = "") -> dict:
    conn = get_db()

    should_fetch = force
    if not should_fetch:
        last = conn.execute(
            "SELECT fetched_at FROM fetch_log WHERE collection=? ORDER BY id DESC LIMIT 1",
            (collection,)
        ).fetchone()
        if not last:
            should_fetch = True
        else:
            age = datetime.now() - datetime.fromisoformat(last["fetched_at"])
            if age > timedelta(minutes=CACHE_MINUTES):
                should_fetch = True

    if should_fetch:
        raw = _fetch_github(collection)
        if raw:
            now = datetime.now().isoformat()
            for key, d in raw.items():
                last_price = conn.execute(
                    "SELECT price FROM price_history WHERE card_key=? AND collection=? ORDER BY id DESC LIMIT 1",
                    (key, collection)
                ).fetchone()
                if not last_price or abs(last_price["price"] - d["prezzo"]) > 0.001:
                    conn.execute(
                        """INSERT INTO price_history
                           (card_key, card_name, set_code, set_name, collector_number,
                            foil, finish, language, frame_effects, price, recorded_at, collection, quantity)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (key, d.get("nome", ""), d.get("set_code", ""), d.get("set", ""),
                         d.get("collector_number", ""), 1 if d.get("foil") else 0,
                         d.get("finish", "foil" if d.get("foil") else "normal"),
                         d.get("language", "en"), d.get("frame_effects", ""),
                         d["prezzo"], d.get("ultimo_aggiornamento", now), collection,
                         int(d.get("quantity") or 1))
                    )
            conn.execute("INSERT INTO fetch_log (cards_count, collection) VALUES (?, ?)",
                         (len(raw), collection))
            conn.commit()

    rows = conn.execute("""
        SELECT ph.card_key, ph.card_name, ph.set_code, ph.set_name,
               ph.collector_number, ph.foil, ph.finish, ph.language,
               ph.frame_effects, ph.price, ph.recorded_at, ph.quantity
        FROM price_history ph
        INNER JOIN (
            SELECT card_key, MAX(id) AS mid FROM price_history
            WHERE collection=? GROUP BY card_key
        ) latest ON ph.id = latest.mid
        ORDER BY ph.price DESC
    """, (collection,)).fetchall()
    conn.close()

    if not rows:
        raw = _fetch_github(collection)
        return raw

    return {
        row["card_key"]: {
            "nome":             row["card_name"],
            "set_code":         row["set_code"],
            "set_name":         row["set_name"],
            "collector_number": row["collector_number"],
            "foil":             bool(row["foil"]),
            "finish":           row["finish"] or ("foil" if row["foil"] else "normal"),
            "language":         row["language"] or "en",
            "frame_effects":    row["frame_effects"] or "",
            "prezzo":           row["price"],
            "quantity":         int(row["quantity"] or 1),
            "ultimo_aggiornamento": row["recorded_at"],
        }
        for row in rows
    }


# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user(request: Request):
    return request.session.get("user")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    coll = user.get("collection", "")
    prices = get_prices(collection=coll)
    items  = list(prices.items())
    total_value  = sum(v["prezzo"] * v.get("quantity", 1) for v in prices.values())
    foil_count   = sum(v.get("quantity", 1) for v in prices.values() if v["foil"])
    total_copies = sum(v.get("quantity", 1) for v in prices.values())
    top_card     = items[0] if items else None

    conn = get_db()
    last_fetch = conn.execute(
        "SELECT fetched_at FROM fetch_log WHERE collection=? ORDER BY id DESC LIMIT 1",
        (coll,)
    ).fetchone()
    conn.close()

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user,
        **_coll_ctx(user),
        "prices": items,
        "total_value": total_value,
        "foil_count": foil_count,
        "total_cards": len(prices),
        "total_copies": total_copies,
        "top_card": top_card,
        "last_fetch": last_fetch["fetched_at"][:16].replace("T", " ") if last_fetch else "—",
    })


@app.post("/refresh")
async def refresh(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    get_prices(force=True, collection=user.get("collection", ""))
    return RedirectResponse("/", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    u = conn.execute(
        "SELECT * FROM users WHERE username=? AND password_hash=?",
        (username, _hash(password))
    ).fetchone()
    conn.close()

    if u:
        request.session["user"] = {
            "id": u["id"], "username": u["username"],
            "is_admin": bool(u["is_admin"]),
            "collection": u["collection"] or "",
        }
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request, "user": None, "error": "Credenziali non valide"
    })


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/card/{card_key:path}", response_class=HTMLResponse)
async def card_detail(request: Request, card_key: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    coll = user.get("collection", "")
    prices = get_prices(collection=coll)
    card   = prices.get(card_key)
    if not card:
        raise HTTPException(status_code=404, detail="Carta non trovata")

    gh_history = _get_history_from_github(card_key, coll)
    if gh_history:
        history_data = [{"price": h["price"], "date": h["date"]} for h in gh_history]
    else:
        conn = get_db()
        rows = conn.execute(
            "SELECT price, recorded_at FROM price_history WHERE card_key=? AND collection=? ORDER BY recorded_at ASC",
            (card_key, coll)
        ).fetchall()
        conn.close()
        history_data = [{"price": h["price"], "date": h["recorded_at"]} for h in rows]

    return templates.TemplateResponse("card.html", {
        "request": request, "user": user,
        **_coll_ctx(user),
        "card_key": card_key, "card": card, "history": history_data,
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return RedirectResponse("/", status_code=302)

    conn = get_db()
    users = conn.execute(
        "SELECT id, username, telegram_chat_id, is_admin, created_at, collection FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("admin.html", {"request": request, "user": user, **_coll_ctx(user), "users": users})


@app.post("/admin/add-user")
async def admin_add_user(request: Request,
                         username: str = Form(...), password: str = Form(...),
                         telegram_chat_id: str = Form(""),
                         collection: str = Form("")):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, telegram_chat_id, collection) VALUES (?, ?, ?, ?)",
            (username, _hash(password), telegram_chat_id.strip() or None, collection.strip().lower())
        )
        conn.commit()
    except Exception:
        pass
    conn.close()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/delete-user/{user_id}")
async def admin_delete_user(request: Request, user_id: int):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=? AND id!=?", (user_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin", status_code=302)


# ── GitHub CSV helpers ────────────────────────────────────────────────────────

def _gh_headers():
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h


def _cf(filename: str, collection: str = "") -> str:
    """Return GitHub filename prefixed by collection (e.g. 'amico_prezzi_riferimento.json')."""
    return f"{collection}_{filename}" if collection else filename


# ── Collections management helpers ───────────────────────────────────────────

_collections_cache: dict = {"data": None, "ts": 0.0}


def _get_collections() -> dict:
    """Read collections.json from GitHub. Returns {id: display_name}. Cached 60s."""
    if _collections_cache["data"] and time.time() - _collections_cache["ts"] < 60:
        return dict(_collections_cache["data"])
    result: dict = {}
    if GITHUB_REPO:
        headers = {"Accept": "application/vnd.github.v3.raw"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/collections.json"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if r.ok:
                    data = r.json()
                    if isinstance(data, dict):
                        result = data
                        break
            except Exception:
                continue
    if "" not in result:
        result[""] = "La mia collezione"
    _collections_cache["data"] = dict(result)
    _collections_cache["ts"] = time.time()
    return result


def _save_collections(data: dict) -> bool:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/collections.json"
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("ascii")
    sha = None
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=10)
        if r.ok:
            sha = r.json().get("sha")
    except Exception:
        pass
    payload = {"message": "Update collections", "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=20)
        if r.ok:
            _collections_cache["data"] = None
            return True
    except Exception:
        pass
    return False


def _name_to_id(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower().strip()).strip('_')[:20]


def _coll_ctx(user: dict) -> dict:
    if not user:
        return {}
    colls = _get_collections()
    active = user.get("collection", "")
    return {
        "collections": colls,
        "active_coll": active,
        "active_coll_name": colls.get(active, "La mia collezione"),
    }


def _get_csv_from_github(collection=""):
    if not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf('ManaBox_Collection.csv', collection)}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.ok:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]
    except Exception:
        pass
    return None, None


def _update_csv_on_github(new_content: str, sha, message: str, collection="") -> bool:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf('ManaBox_Collection.csv', collection)}"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
        return r.ok
    except Exception:
        return False


def _get_json_from_github(collection=""):
    if not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf('prezzi_riferimento.json', collection)}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.ok:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]
    except Exception:
        pass
    return None, None


def _get_history_from_github(card_key: str, collection="") -> list:
    """Read price history for one card from storico_prezzi.json on GitHub."""
    if not GITHUB_REPO:
        return []
    headers = {"Accept": "application/vnd.github.v3.raw"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    fname = _cf("storico_prezzi.json", collection)
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/{fname}"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.ok:
                return r.json().get(card_key, [])
        except Exception:
            continue
    local = BASE_DIR / fname
    if local.exists():
        try:
            import json as _json
            with open(local, encoding="utf-8") as f:
                return _json.load(f).get(card_key, [])
        except Exception:
            pass
    return []


def _load_sold_from_github(collection="") -> list:
    """Read vendute.json from GitHub. Returns list of dicts (newest first)."""
    if not GITHUB_REPO:
        return []
    import json as _json
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf('vendute.json', collection)}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.ok:
            content = base64.b64decode(r.json()["content"]).decode("utf-8")
            data = _json.loads(content)
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save_sold_to_github(entries: list, message: str, collection="") -> bool:
    """Create or update vendute.json on GitHub with the given list."""
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    import json as _json
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf('vendute.json', collection)}"
    encoded = base64.b64encode(
        _json.dumps(entries, indent=2, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    sha = None
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=10)
        if r.ok:
            sha = r.json().get("sha")
    except Exception:
        pass
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=20)
        return r.ok
    except Exception:
        return False


def _update_json_on_github(new_content: str, sha, message: str, collection="") -> bool:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf('prezzi_riferimento.json', collection)}"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
        return r.ok
    except Exception:
        return False


# ── Delete card route ─────────────────────────────────────────────────────────

@app.post("/delete-card/{card_key:path}")
async def delete_card(request: Request, card_key: str,
                      sold_price: str = Form(None)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import json as _json

    conn = get_db()

    coll = user.get("collection", "")

    # 1. Save to sold_cards (DB + GitHub)
    last = conn.execute(
        "SELECT card_name, set_code, set_name, collector_number, finish, language, frame_effects, price "
        "FROM price_history WHERE card_key=? AND collection=? ORDER BY id DESC LIMIT 1",
        (card_key, coll)
    ).fetchone()
    if last:
        sold_at = datetime.now().isoformat()
        entry_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        try:
            final_price = float(sold_price) if sold_price else last["price"]
        except (ValueError, TypeError):
            final_price = last["price"]
        entry = {
            "id": entry_id,
            "card_key": card_key,
            "card_name": last["card_name"],
            "set_code": last["set_code"] or "",
            "set_name": last["set_name"] or "",
            "collector_number": last["collector_number"] or "",
            "finish": last["finish"] or "normal",
            "language": last["language"] or "en",
            "frame_effects": last["frame_effects"] or "",
            "last_price": final_price,
            "sold_at": sold_at,
        }
        current_sold = _load_sold_from_github(coll)
        current_sold.insert(0, entry)
        _save_sold_to_github(current_sold, f"Sell {card_key}", coll)
        conn.execute(
            """INSERT INTO sold_cards
               (card_key, card_name, set_code, set_name, collector_number,
                finish, language, frame_effects, last_price, collection)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_key, last["card_name"], last["set_code"] or "", last["set_name"] or "",
             last["collector_number"] or "", last["finish"] or "normal",
             last["language"] or "en", last["frame_effects"] or "",
             last["price"], coll)
        )

    # 2. Remove from price_history DB
    conn.execute("DELETE FROM price_history WHERE card_key=? AND collection=?", (card_key, coll))
    conn.commit()
    conn.close()

    json_content, json_sha = _get_json_from_github(coll)
    if json_content and json_sha:
        try:
            data = _json.loads(json_content)
            if card_key in data:
                del data[card_key]
                _update_json_on_github(
                    _json.dumps(data, indent=2, ensure_ascii=False),
                    json_sha, f"Remove {card_key} (sold)", coll
                )
        except Exception:
            pass

    parts = card_key.split("_")
    if len(parts) >= 4:
        c_set  = parts[0].upper()
        c_num  = parts[1]
        c_fin  = parts[2]
        c_lang = parts[3]
        csv_content, csv_sha = _get_csv_from_github(coll)
        if csv_content and csv_sha:
            lines = csv_content.splitlines(keepends=True)
            new_lines = []
            for line in lines:
                low = line.lower()
                if (c_set.lower() in low and c_num in low and
                        c_fin in low and c_lang in low):
                    continue
                new_lines.append(line)
            if len(new_lines) < len(lines):
                _update_csv_on_github(
                    "".join(new_lines), csv_sha,
                    f"Remove {card_key} from collection (sold)", coll
                )

    return RedirectResponse("/", status_code=302)


@app.post("/remove-card/{card_key:path}")
async def remove_card(request: Request, card_key: str):
    """Remove card from collection WITHOUT adding to sold history."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import json as _json

    coll = user.get("collection", "")
    conn = get_db()
    conn.execute("DELETE FROM price_history WHERE card_key=? AND collection=?", (card_key, coll))
    conn.commit()
    conn.close()

    json_content, json_sha = _get_json_from_github(coll)
    if json_content and json_sha:
        try:
            data = _json.loads(json_content)
            if card_key in data:
                del data[card_key]
                _update_json_on_github(
                    _json.dumps(data, indent=2, ensure_ascii=False),
                    json_sha, f"Remove {card_key} (correction)", coll
                )
        except Exception:
            pass

    parts = card_key.split("_")
    if len(parts) >= 4:
        c_set  = parts[0].upper()
        c_num  = parts[1]
        c_fin  = parts[2]
        c_lang = parts[3]
        csv_content, csv_sha = _get_csv_from_github(coll)
        if csv_content and csv_sha:
            lines = csv_content.splitlines(keepends=True)
            new_lines = [
                l for l in lines
                if not (c_set.lower() in l.lower() and c_num in l and
                        c_fin in l and c_lang in l)
            ]
            if len(new_lines) < len(lines):
                _update_csv_on_github(
                    "".join(new_lines), csv_sha,
                    f"Remove {card_key} from collection (correction)", coll
                )

    return RedirectResponse("/", status_code=302)


@app.post("/edit-card-lang/{card_key:path}")
async def edit_card_lang(request: Request, card_key: str,
                         new_language: str = Form(...),
                         silverscroll: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    parts = card_key.split("_")
    if len(parts) < 4:
        return RedirectResponse("/", status_code=302)

    set_code = parts[0].upper()
    col_num  = parts[1]
    finish   = parts[2]
    old_lang = parts[3]
    new_lang = new_language.strip().lower()
    manual_fx = "silverscroll" if silverscroll else ""
    new_key = f"{set_code}_{col_num}_{finish}_{new_lang}"
    lang_changed = (new_lang != old_lang)

    # Fetch updated price from Scryfall only if language changed
    new_price = None
    if lang_changed:
        try:
            headers_sf = {"User-Agent": "MTGPriceTracker/1.0"}
            r = requests.get(
                f"https://api.scryfall.com/cards/{set_code.lower()}/{col_num}/{new_lang}",
                headers=headers_sf, timeout=8)
            if not r.ok:
                r = requests.get(
                    f"https://api.scryfall.com/cards/{set_code.lower()}/{col_num}",
                    headers=headers_sf, timeout=8)
            if r.ok:
                cd = r.json()
                prices = cd.get("prices", {})
                is_foil = finish in ("foil", "etched")
                p = (prices.get("eur_foil") or prices.get("eur")) if is_foil \
                    else (prices.get("eur") or prices.get("eur_foil"))
                new_price = float(p) if p else None
        except Exception:
            pass

    coll = user.get("collection", "")

    # Update prezzi_riferimento.json (with retry)
    for _ in range(2):
        json_content, json_sha = _get_json_from_github(coll)
        if not json_content:
            break
        prezzi = json.loads(json_content)
        old_entry = prezzi.pop(card_key, {})
        if old_entry:
            old_entry["language"] = new_lang
            if new_price is not None:
                old_entry["prezzo"] = new_price
            old_entry["frame_effects"] = manual_fx
            old_entry["ultimo_aggiornamento"] = datetime.now().isoformat()
            prezzi[new_key] = old_entry
        new_json = json.dumps(prezzi, ensure_ascii=False, indent=2)
        if _update_json_on_github(new_json, json_sha, f"Edit card {card_key}->{new_key} fx={manual_fx}", coll):
            (BASE_DIR / _cf("prezzi_riferimento.json", coll)).write_text(new_json, encoding="utf-8")
            break

    if lang_changed:
        try:
            storico_fname = _cf("storico_prezzi.json", coll)
            url_st = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{storico_fname}"
            r_st = requests.get(url_st, headers=_gh_headers(), timeout=15)
            if r_st.ok:
                st_data = r_st.json()
                storico = json.loads(base64.b64decode(st_data["content"]).decode("utf-8"))
                if card_key in storico:
                    storico[new_key] = storico.pop(card_key)
                    enc = base64.b64encode(
                        json.dumps(storico, ensure_ascii=False, indent=2).encode()).decode()
                    requests.put(url_st, headers=_gh_headers(), json={
                        "message": f"Rename storico {card_key}->{new_key}",
                        "content": enc, "sha": st_data["sha"]
                    }, timeout=15)
        except Exception:
            pass

        csv_content, csv_sha = _get_csv_from_github(coll)
        if csv_content and csv_sha:
            lines = csv_content.splitlines(keepends=True)
            new_lines = []
            for line in lines:
                low = line.lower()
                if (set_code.lower() in low and col_num in low and
                        finish in low and old_lang in low):
                    line = line.replace(f",{old_lang},", f",{new_lang},")
                new_lines.append(line)
            _update_csv_on_github("".join(new_lines), csv_sha,
                                  f"Edit lang {card_key}->{new_lang}", coll)

    conn = get_db()
    conn.execute(
        "UPDATE price_history SET card_key=?, language=?, frame_effects=? WHERE card_key=? AND collection=?",
        (new_key, new_lang, manual_fx, card_key, coll))
    conn.commit()
    conn.close()

    return RedirectResponse("/", status_code=302)


# ── Sold cards ───────────────────────────────────────────────────────────────

@app.get("/sold", response_class=HTMLResponse)
async def sold_list(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    cards = _load_sold_from_github(coll)
    if not cards:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM sold_cards WHERE collection=? ORDER BY sold_at DESC", (coll,)
        ).fetchall()
        conn.close()
        cards = [dict(row) for row in rows]
    total_sold_value = sum(float(c.get("last_price") or 0) for c in cards)
    return templates.TemplateResponse("sold.html", {
        "request": request, "user": user,
        **_coll_ctx(user),
        "cards": cards, "total_sold_value": total_sold_value,
    })


@app.post("/sold/delete/{sold_id}")
async def sold_delete(request: Request, sold_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    current_sold = _load_sold_from_github(coll)
    new_sold = [e for e in current_sold if str(e.get("id", "")) != sold_id]
    if len(new_sold) < len(current_sold):
        _save_sold_to_github(new_sold, f"Remove sold entry {sold_id}", coll)
    conn = get_db()
    try:
        conn.execute("DELETE FROM sold_cards WHERE id=? AND collection=?", (int(sold_id), coll))
        conn.commit()
    except (ValueError, Exception):
        pass
    conn.close()
    return RedirectResponse("/sold", status_code=302)


@app.post("/sold/relist/{sold_id}")
async def sold_relist(request: Request, sold_id: str):
    """Move a sold card back into the active collection."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import json as _json

    coll = user.get("collection", "")

    # 1. Find and remove from vendute.json on GitHub
    current_sold = _load_sold_from_github(coll)
    entry = next((e for e in current_sold if str(e.get("id", "")) == sold_id), None)
    if not entry:
        return RedirectResponse("/sold", status_code=302)

    new_sold = [e for e in current_sold if str(e.get("id", "")) != sold_id]
    _save_sold_to_github(new_sold, f"Relist {entry.get('card_name', sold_id)}", coll)

    # 2. Add back to ManaBox_Collection.csv on GitHub
    name        = entry.get("card_name", "")
    set_code    = (entry.get("set_code") or "").upper()
    set_name    = entry.get("set_name") or ""
    col_num     = entry.get("collector_number") or ""
    finish      = entry.get("finish") or "normal"
    language    = entry.get("language") or "en"
    now_str     = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Try to get scryfall_id + rarity from Scryfall
    scryfall_id = ""
    rarity = "unknown"
    if set_code and col_num:
        try:
            r = requests.get(
                f"https://api.scryfall.com/cards/{set_code.lower()}/{col_num}",
                headers={"User-Agent": "MTGPriceTracker/1.0"}, timeout=8
            )
            if r.ok:
                cd = r.json()
                scryfall_id = cd.get("id", "")
                rarity = cd.get("rarity", "unknown")
        except Exception:
            pass

    name_safe     = name.replace('"', '""')
    set_name_safe = set_name.replace('"', '""')
    new_row = (f'webapp,binder,"{name_safe}",{set_code},"{set_name_safe}",'
               f'{col_num},{finish},{rarity},1,,{scryfall_id},'
               f'0,false,false,near_mint,{language},EUR,{now_str}\n')

    csv_content, csv_sha = _get_csv_from_github(coll)
    if csv_content and csv_sha:
        updated = csv_content.rstrip("\n") + "\n" + new_row
        _update_csv_on_github(updated, csv_sha, f"Relist {name} ({set_code})", coll)

    # 3. Add back to prezzi_riferimento.json with current Scryfall price
    if scryfall_id:
        _add_card_to_prezzi(name, set_code, set_name, col_num, finish, language, scryfall_id, coll)
    elif set_code and col_num:
        try:
            r = requests.get(
                f"https://api.scryfall.com/cards/{set_code.lower()}/{col_num}",
                headers={"User-Agent": "MTGPriceTracker/1.0"}, timeout=8
            )
            if r.ok:
                cd = r.json()
                _add_card_to_prezzi(name, set_code, set_name, col_num,
                                    finish, language, cd["id"], coll)
        except Exception:
            pass

    return RedirectResponse("/sold", status_code=302)


@app.get("/sold/export")
async def sold_export(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    cards = _load_sold_from_github(coll)
    if not cards:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM sold_cards WHERE collection=? ORDER BY sold_at DESC", (coll,)
        ).fetchall()
        conn.close()
        cards = [dict(row) for row in rows]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Nome", "Set", "# Collezionista", "Finitura", "Lingua",
                     "Versione Speciale", "Ultimo Prezzo (EUR)", "Data Vendita"])
    for c in cards:
        writer.writerow([
            c.get("card_name", ""),
            (c.get("set_code") or "").upper(),
            c.get("collector_number", ""),
            c.get("finish", "normal"),
            c.get("language", "en"),
            c.get("frame_effects", ""),
            f"{float(c.get('last_price') or 0):.2f}",
            (c.get("sold_at") or "")[:16].replace("T", " "),
        ])
    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM for Excel
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=carte_vendute.csv"},
    )


# ── Add card routes ───────────────────────────────────────────────────────────

def _add_card_to_prezzi(name: str, set_code: str, set_name: str,
                        collector_number: str, finish: str,
                        language: str, scryfall_id: str,
                        collection: str = "", quantity: int = 1) -> bool:
    """Fetch current price from Scryfall and add card to prezzi_riferimento.json on GitHub."""
    try:
        r = requests.get(f"https://api.scryfall.com/cards/{scryfall_id}", timeout=10)
        if not r.ok:
            return False
        card_data = r.json()
        is_foil = finish in ("foil", "etched")
        prices = card_data.get("prices", {})
        price_str = (prices.get("eur_foil") or prices.get("eur")) if is_foil \
                    else (prices.get("eur") or prices.get("eur_foil"))
        # Always add the card even with no EUR price (shows as 0.00, tracker will fill it in)
        price_val = float(price_str) if price_str else 0.0

        card_key = f"{set_code.upper()}_{collector_number}_{finish}_{language}"
        qty = max(1, int(quantity) if quantity else 1)
        entry = {
            "nome": name,
            "set": set_name or card_data.get("set_name", ""),
            "set_code": set_code.upper(),
            "collector_number": collector_number,
            "prezzo": price_val,
            "foil": is_foil,
            "finish": finish,
            "language": language,
            "quantity": qty,
            "frame_effects": ",".join(card_data.get("frame_effects") or []),
            "ultimo_aggiornamento": datetime.now().isoformat(),
        }

        # Write to SQLite immediately (bypasses CDN caching delay)
        now_iso = datetime.now().isoformat()
        conn = get_db()
        conn.execute(
            """INSERT INTO price_history
               (card_key, card_name, set_code, set_name, collector_number,
                foil, finish, language, frame_effects, price, recorded_at, collection, quantity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_key, name, set_code.upper(), entry["set"],
             collector_number, 1 if is_foil else 0, finish, language,
             entry["frame_effects"], price_val, now_iso, collection, qty)
        )
        conn.commit()
        conn.close()

        # Write to GitHub JSON (with retry on SHA conflict)
        for _ in range(2):
            json_content, sha = _get_json_from_github(collection)
            prezzi = json.loads(json_content) if json_content else {}
            prezzi[card_key] = entry
            new_content = json.dumps(prezzi, ensure_ascii=False, indent=2)
            if _update_json_on_github(new_content, sha, f"Add {name} price via webapp", collection):
                break

        local = BASE_DIR / _cf("prezzi_riferimento.json", collection)
        with open(local, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    except Exception:
        return False


@app.get("/import-csv", response_class=HTMLResponse)
async def import_csv_get(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("import_csv.html", {"request": request, "user": user,
                                                           **_coll_ctx(user),
                                                           "result": None, "error": None})


@app.post("/import-csv", response_class=HTMLResponse)
async def import_csv_post(request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    ctx = {"request": request, "user": user, "result": None, "error": None}
    ctx.update(_coll_ctx(user))

    # --- 1. Read uploaded file ---
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig")  # handle BOM
    except Exception:
        ctx["error"] = "Impossibile leggere il file. Assicurati che sia UTF-8."
        return templates.TemplateResponse("import_csv.html", ctx)

    try:
        reader = csv.DictReader(io.StringIO(text))
        uploaded_rows = list(reader)
    except Exception:
        ctx["error"] = "Formato CSV non riconosciuto."
        return templates.TemplateResponse("import_csv.html", ctx)

    required = {"Name", "Set code", "Collector number", "Foil", "Language"}
    if not required.issubset(set(reader.fieldnames or [])):
        ctx["error"] = f"CSV non valido. Colonne attese: {', '.join(required)}"
        return templates.TemplateResponse("import_csv.html", ctx)

    # --- 2. Load existing CSV from GitHub (or start fresh for new collections) ---
    coll = user.get("collection", "")
    existing_content, existing_sha = _get_csv_from_github(coll)
    if existing_content is None:
        # New collection: create an empty CSV using the uploaded file's headers
        existing_content = ",".join(f'"{f}"' if "," in f else f for f in (reader.fieldnames or [])) + "\n"
        existing_sha = None

    existing_reader = csv.DictReader(io.StringIO(existing_content))
    existing_rows = list(existing_reader)
    fieldnames = existing_reader.fieldnames or reader.fieldnames

    # Dedup key: (set_code, collector_number, finish, language)
    existing_keys = {
        (r["Set code"].upper(), r["Collector number"], r["Foil"], r["Language"])
        for r in existing_rows
    }

    # --- 3. Find new rows ---
    new_rows = []
    for r in uploaded_rows:
        key = (r["Set code"].upper(), r["Collector number"], r["Foil"], r["Language"])
        if key not in existing_keys:
            new_rows.append(r)

    if not new_rows:
        ctx["result"] = {"added": 0, "skipped": len(uploaded_rows), "prices_fetched": 0}
        return templates.TemplateResponse("import_csv.html", ctx)

    # --- 4. Append new rows to CSV on GitHub ---
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writerows(new_rows)
    updated_content = existing_content.rstrip("\n") + "\n" + buf.getvalue()

    for _ in range(2):
        ok_csv = _update_csv_on_github(
            updated_content, existing_sha,
            f"Import {len(new_rows)} cards from ManaBox CSV", coll
        )
        if ok_csv:
            break
        existing_content, existing_sha = _get_csv_from_github(coll)

    # --- 5. Batch-fetch prices from Scryfall (75 cards per request) ---
    prices_fetched = 0
    try:
        json_content, json_sha = _get_json_from_github(coll)
        prezzi = json.loads(json_content) if json_content else {}
        now_iso = datetime.now().isoformat()

        BATCH = 75
        for i in range(0, len(new_rows), BATCH):
            batch = new_rows[i:i + BATCH]
            identifiers = []
            for r in batch:
                sid = r.get("Scryfall ID", "").strip()
                if sid:
                    identifiers.append({"id": sid})
                elif r.get("Set code") and r.get("Collector number"):
                    identifiers.append({"set": r["Set code"].lower(),
                                        "collector_number": r["Collector number"]})

            if not identifiers:
                continue

            resp = requests.post(
                "https://api.scryfall.com/cards/collection",
                headers={"User-Agent": "MTGPriceTracker/1.0",
                         "Content-Type": "application/json"},
                json={"identifiers": identifiers},
                timeout=20
            )
            if not resp.ok:
                continue

            for card in resp.json().get("data", []):
                # Find matching row in batch by set+number
                finish_map = {}
                for r in batch:
                    sid = r.get("Scryfall ID", "").strip()
                    if sid == card.get("id") or (
                        r["Set code"].lower() == card["set"] and
                        r["Collector number"] == card["collector_number"]
                    ):
                        finish_map = r
                        break

                finish   = (finish_map.get("Foil") or "normal")
                language = (finish_map.get("Language") or "en")
                is_foil  = finish in ("foil", "etched")
                prices_d = card.get("prices", {})
                price_str = (prices_d.get("eur_foil") or prices_d.get("eur")) if is_foil \
                            else (prices_d.get("eur") or prices_d.get("eur_foil"))
                price_val = float(price_str) if price_str else 0.0
                qty = max(1, int(finish_map.get("Quantity") or 1))

                card_key = f"{card['set'].upper()}_{card['collector_number']}_{finish}_{language}"
                if card_key in prezzi:
                    # Same card in multiple CSV rows — sum quantities
                    prezzi[card_key]["quantity"] = prezzi[card_key].get("quantity", 1) + qty
                else:
                    prezzi[card_key] = {
                        "nome": card["name"],
                        "set": card.get("set_name", ""),
                        "set_code": card["set"].upper(),
                        "collector_number": card["collector_number"],
                        "prezzo": price_val,
                        "foil": is_foil,
                        "finish": finish,
                        "language": language,
                        "quantity": qty,
                        "ultimo_aggiornamento": now_iso,
                    }
                prices_fetched += 1

        # Write updated prezzi to GitHub with retry
        new_prezzi = json.dumps(prezzi, ensure_ascii=False, indent=2)
        for _ in range(2):
            if _update_json_on_github(new_prezzi, json_sha,
                                      f"Add prices for {prices_fetched} imported cards", coll):
                break
            json_content, json_sha = _get_json_from_github(coll)
            prezzi_existing = json.loads(json_content) if json_content else {}
            prezzi_existing.update(prezzi)
            new_prezzi = json.dumps(prezzi_existing, ensure_ascii=False, indent=2)

        (BASE_DIR / _cf("prezzi_riferimento.json", coll)).write_text(new_prezzi, encoding="utf-8")

    except Exception:
        pass  # prices will be picked up by tracker on next run

    get_prices(force=True, collection=coll)  # sync SQLite so cards appear immediately

    ctx["result"] = {
        "added": len(new_rows),
        "skipped": len(uploaded_rows) - len(new_rows),
        "prices_fetched": prices_fetched,
    }
    return templates.TemplateResponse("import_csv.html", ctx)


@app.get("/add-card", response_class=HTMLResponse)
async def add_card_get(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("add_card.html", {
        "request": request, "user": user,
        **_coll_ctx(user),
        "error": None, "success": None
    })


@app.post("/add-card", response_class=HTMLResponse)
async def add_card_post(
    request: Request,
    name: str = Form(...),
    set_code: str = Form(...),
    set_name: str = Form(...),
    collector_number: str = Form(...),
    scryfall_id: str = Form(...),
    rarity: str = Form("common"),
    foil: str = Form("normal"),
    language: str = Form("en"),
    frame_effects: str = Form(""),
    purchase_price: str = Form("0"),
    quantity: str = Form("1"),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    ctx = {"request": request, "user": user, "error": None, "success": None}
    ctx.update(_coll_ctx(user))

    coll = user.get("collection", "")
    csv_content, sha = _get_csv_from_github(coll)
    if csv_content is None:
        # New collection: start with an empty CSV
        csv_content = "Source,Trade In,Name,Set code,Set name,Collector number,Foil,Rarity,Quantity,Language,Scryfall ID,Purchase price,Misprint,Altered,Condition,Language,Currency,Added\n"
        sha = None

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    name_safe     = name.replace('"', '""')
    set_name_safe = set_name.replace('"', '""')
    lang = language.strip() or "en"
    new_row = (f'webapp,binder,"{name_safe}",{set_code.upper()},"{set_name_safe}",'
               f'{collector_number},{foil},{rarity},{quantity},,{scryfall_id},'
               f'{purchase_price},false,false,near_mint,{lang},EUR,{now}\n')

    updated = csv_content.rstrip("\n") + "\n" + new_row
    ok = _update_csv_on_github(updated, sha, f"Add {name} ({set_code.upper()}) via webapp", coll)

    if ok:
        _add_card_to_prezzi(name, set_code, set_name, collector_number,
                            foil, language, scryfall_id, coll,
                            quantity=int(quantity) if quantity else 1)
        ctx["success"] = f"'{name}' aggiunta alla collezione!"
    else:
        ctx["error"] = "Errore durante il salvataggio su GitHub. Controlla GITHUB_TOKEN."
    return templates.TemplateResponse("add_card.html", ctx)


# ── Sealed products ──────────────────────────────────────────────────────────

SEALED_JSON = "sealed_products.json"

def _get_sealed_from_github(collection=""):
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf(SEALED_JSON, collection)}"
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.ok:
            d = r.json()
            return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]
        if r.status_code == 404:
            return "[]", None
    except Exception:
        pass
    return None, None

def _update_sealed_on_github(content: str, sha, message: str, collection="") -> bool:
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_cf(SEALED_JSON, collection)}"
        payload = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if sha:
            payload["sha"] = sha
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False

def _load_sealed_local(collection=""):
    local = BASE_DIR / _cf(SEALED_JSON, collection)
    if local.exists():
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except Exception:
            pass
    content, _ = _get_sealed_from_github(collection)
    if content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return []



@app.get("/sealed", response_class=HTMLResponse)
async def sealed_list(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    products = _load_sealed_local(coll)
    total_purchase = sum(float(p.get("purchase_price", 0)) * int(p.get("quantity", 1))
                         for p in products)
    total_current  = sum((float(p.get("current_price") or p.get("purchase_price", 0)))
                         * int(p.get("quantity", 1)) for p in products)
    return templates.TemplateResponse("sealed.html", {
        "request": request, "user": user,
        **_coll_ctx(user),
        "products": products,
        "total_purchase": total_purchase,
        "total_current":  total_current,
        "gain": total_current - total_purchase,
    })


@app.post("/sealed/add")
async def sealed_add(request: Request,
                     name: str          = Form(...),
                     set_name: str      = Form(""),
                     product_type: str  = Form("Collector Booster Box"),
                     quantity: str      = Form("1"),
                     purchase_price: str = Form("0"),
                     current_price: str = Form(""),
                     mtggoldfish_url: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    purchase = float(purchase_price) if purchase_price else 0.0
    current  = float(current_price)  if current_price  else purchase
    qty      = max(1, int(quantity)  if quantity        else 1)
    for _ in range(2):
        content, sha = _get_sealed_from_github(coll)
        if content is None:
            break
        products = json.loads(content)
        new_id = str(max((int(p.get("id", 0)) for p in products), default=0) + 1)
        products.append({
            "id": new_id, "name": name, "set_name": set_name,
            "product_type": product_type, "quantity": qty,
            "purchase_price": purchase, "current_price": current,
            "mtggoldfish_url": mtggoldfish_url.strip(),
            "added_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
        })
        new_content = json.dumps(products, ensure_ascii=False, indent=2)
        if _update_sealed_on_github(new_content, sha, f"Add sealed: {name}", coll):
            (BASE_DIR / _cf(SEALED_JSON, coll)).write_text(new_content, encoding="utf-8")
            break
    return RedirectResponse("/sealed", status_code=302)


@app.post("/sealed/update/{item_id}")
async def sealed_update(request: Request, item_id: str,
                        current_price: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    for _ in range(2):
        content, sha = _get_sealed_from_github(coll)
        if content is None:
            break
        products = json.loads(content)
        for p in products:
            if str(p.get("id")) == item_id:
                p["current_price"] = float(current_price)
                p["last_updated"]  = datetime.now().isoformat()
                break
        new_content = json.dumps(products, ensure_ascii=False, indent=2)
        if _update_sealed_on_github(new_content, sha, f"Update sealed price id={item_id}", coll):
            (BASE_DIR / _cf(SEALED_JSON, coll)).write_text(new_content, encoding="utf-8")
            break
    return RedirectResponse("/sealed", status_code=302)


@app.post("/sealed/delete/{item_id}")
async def sealed_delete(request: Request, item_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    coll = user.get("collection", "")
    for _ in range(2):
        content, sha = _get_sealed_from_github(coll)
        if content is None:
            break
        products = json.loads(content)
        products = [p for p in products if str(p.get("id")) != item_id]
        new_content = json.dumps(products, ensure_ascii=False, indent=2)
        if _update_sealed_on_github(new_content, sha, f"Delete sealed id={item_id}", coll):
            (BASE_DIR / _cf(SEALED_JSON, coll)).write_text(new_content, encoding="utf-8")
            break
    return RedirectResponse("/sealed", status_code=302)


# ── Collection routes ─────────────────────────────────────────────────────────

@app.get("/collections/switch/{coll_id:path}")
async def switch_collection(request: Request, coll_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    colls = _get_collections()
    if coll_id not in colls:
        return RedirectResponse("/", status_code=302)
    user["collection"] = coll_id
    request.session["user"] = user
    return RedirectResponse("/", status_code=302)


@app.post("/collections/add")
async def add_collection(request: Request, coll_name: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    name = coll_name.strip()
    if not name:
        return RedirectResponse("/", status_code=302)
    colls = _get_collections()
    coll_id = _name_to_id(name) or "coll"
    base_id, i = coll_id, 2
    while coll_id in colls:
        coll_id = f"{base_id}_{i}"; i += 1
    colls[coll_id] = name
    _save_collections(colls)
    user["collection"] = coll_id
    request.session["user"] = user
    return RedirectResponse("/", status_code=302)


@app.post("/collections/delete/{coll_id:path}")
async def delete_collection(request: Request, coll_id: str):
    user = current_user(request)
    if not user or not coll_id:
        return RedirectResponse("/", status_code=302)
    colls = _get_collections()
    if coll_id in colls:
        del colls[coll_id]
        _save_collections(colls)
    if user.get("collection") == coll_id:
        user["collection"] = ""
        request.session["user"] = user
    return RedirectResponse("/", status_code=302)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
