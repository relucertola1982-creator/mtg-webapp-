from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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
from pathlib import Path

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

def _fetch_github() -> dict:
    # Try GitHub repo first
    if GITHUB_REPO:
        headers = {"Accept": "application/vnd.github.v3.raw"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/prezzi_riferimento.json"
            try:
                r = requests.get(url, headers=headers, timeout=15)
                if r.ok:
                    return r.json()
            except Exception:
                continue

    # Fallback: local file bundled in the repo
    local = BASE_DIR / "prezzi_riferimento.json"
    if local.exists():
        try:
            import json as _json
            with open(local, "r", encoding="utf-8") as f:
                data = _json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def get_prices(force: bool = False) -> dict:
    conn = get_db()

    # Decide whether to refresh from GitHub
    should_fetch = force
    if not should_fetch:
        last = conn.execute(
            "SELECT fetched_at FROM fetch_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not last:
            should_fetch = True
        else:
            age = datetime.now() - datetime.fromisoformat(last["fetched_at"])
            if age > timedelta(minutes=CACHE_MINUTES):
                should_fetch = True

    if should_fetch:
        raw = _fetch_github()
        if raw:
            now = datetime.now().isoformat()
            for key, d in raw.items():
                # Only insert if price changed
                last_price = conn.execute(
                    "SELECT price FROM price_history WHERE card_key=? ORDER BY id DESC LIMIT 1",
                    (key,)
                ).fetchone()
                if not last_price or abs(last_price["price"] - d["prezzo"]) > 0.001:
                    conn.execute(
                        """INSERT INTO price_history
                           (card_key, card_name, set_code, set_name, collector_number,
                            foil, finish, language, frame_effects, price, recorded_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (key, d.get("nome", ""), d.get("set_code", ""), d.get("set", ""),
                         d.get("collector_number", ""), 1 if d.get("foil") else 0,
                         d.get("finish", "foil" if d.get("foil") else "normal"),
                         d.get("language", "en"), d.get("frame_effects", ""),
                         d["prezzo"], d.get("ultimo_aggiornamento", now))
                    )
            conn.execute("INSERT INTO fetch_log (cards_count) VALUES (?)", (len(raw),))
            conn.commit()

    rows = conn.execute("""
        SELECT ph.card_key, ph.card_name, ph.set_code, ph.set_name,
               ph.collector_number, ph.foil, ph.finish, ph.language,
               ph.frame_effects, ph.price, ph.recorded_at
        FROM price_history ph
        INNER JOIN (
            SELECT card_key, MAX(id) AS mid FROM price_history GROUP BY card_key
        ) latest ON ph.id = latest.mid
        ORDER BY ph.price DESC
    """).fetchall()
    conn.close()

    if not rows:
        raw = _fetch_github()
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

    prices = get_prices()
    items  = list(prices.items())
    total_value  = sum(v["prezzo"] for v in prices.values())
    foil_count   = sum(1 for v in prices.values() if v["foil"])
    top_card     = items[0] if items else None

    conn = get_db()
    last_fetch = conn.execute(
        "SELECT fetched_at FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user,
        "prices": items,
        "total_value": total_value,
        "foil_count": foil_count,
        "total_cards": len(prices),
        "top_card": top_card,
        "last_fetch": last_fetch["fetched_at"][:16].replace("T", " ") if last_fetch else "—",
    })


@app.post("/refresh")
async def refresh(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    get_prices(force=True)
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
            "id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"])
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

    prices = get_prices()
    card   = prices.get(card_key)
    if not card:
        raise HTTPException(status_code=404, detail="Carta non trovata")

    conn = get_db()
    history = conn.execute(
        "SELECT price, recorded_at FROM price_history WHERE card_key=? ORDER BY recorded_at ASC",
        (card_key,)
    ).fetchall()
    conn.close()

    history_data = [{"price": h["price"], "date": h["recorded_at"]} for h in history]

    return templates.TemplateResponse("card.html", {
        "request": request, "user": user,
        "card_key": card_key, "card": card, "history": history_data,
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return RedirectResponse("/", status_code=302)

    conn = get_db()
    users = conn.execute(
        "SELECT id, username, telegram_chat_id, is_admin, created_at FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("admin.html", {"request": request, "user": user, "users": users})


@app.post("/admin/add-user")
async def admin_add_user(request: Request,
                         username: str = Form(...), password: str = Form(...),
                         telegram_chat_id: str = Form("")):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, telegram_chat_id) VALUES (?, ?, ?)",
            (username, _hash(password), telegram_chat_id.strip() or None)
        )
        conn.commit()
    except sqlite3.IntegrityError:
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


def _get_csv_from_github():
    if not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/ManaBox_Collection.csv"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.ok:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]
    except Exception:
        pass
    return None, None


def _update_csv_on_github(new_content: str, sha: str, message: str) -> bool:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/ManaBox_Collection.csv"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    try:
        r = requests.put(url, headers=_gh_headers(), json={
            "message": message, "content": encoded, "sha": sha
        }, timeout=15)
        return r.ok
    except Exception:
        return False


def _get_json_from_github():
    if not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/prezzi_riferimento.json"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.ok:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]
    except Exception:
        pass
    return None, None


def _update_json_on_github(new_content: str, sha: str, message: str) -> bool:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/prezzi_riferimento.json"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    try:
        r = requests.put(url, headers=_gh_headers(), json={
            "message": message, "content": encoded, "sha": sha
        }, timeout=15)
        return r.ok
    except Exception:
        return False


# ── Delete card route ─────────────────────────────────────────────────────────

@app.post("/delete-card/{card_key:path}")
async def delete_card(request: Request, card_key: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import json as _json

    # 1. Remove from price_history DB
    conn = get_db()
    conn.execute("DELETE FROM price_history WHERE card_key=?", (card_key,))
    conn.commit()
    conn.close()

    # 2. Remove from prezzi_riferimento.json on GitHub
    json_content, json_sha = _get_json_from_github()
    if json_content and json_sha:
        try:
            data = _json.loads(json_content)
            if card_key in data:
                del data[card_key]
                _update_json_on_github(
                    _json.dumps(data, indent=2, ensure_ascii=False),
                    json_sha,
                    f"Remove {card_key} (sold)"
                )
        except Exception:
            pass

    # 3. Remove matching row(s) from CSV on GitHub
    # card_key format: {set_code}_{collector_num}_{finish}_{lang}
    parts = card_key.split("_")
    if len(parts) >= 4:
        c_set  = parts[0].upper()
        c_num  = parts[1]
        c_fin  = parts[2]
        c_lang = parts[3]
        csv_content, csv_sha = _get_csv_from_github()
        if csv_content and csv_sha:
            lines = csv_content.splitlines(keepends=True)
            new_lines = []
            for line in lines:
                low = line.lower()
                if (c_set.lower() in low and c_num in low and
                        c_fin in low and c_lang in low):
                    continue  # skip = delete
                new_lines.append(line)
            if len(new_lines) < len(lines):
                _update_csv_on_github(
                    "".join(new_lines), csv_sha,
                    f"Remove {card_key} from collection (sold)"
                )

    return RedirectResponse("/", status_code=302)


# ── Add card routes ───────────────────────────────────────────────────────────

@app.get("/add-card", response_class=HTMLResponse)
async def add_card_get(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("add_card.html", {
        "request": request, "user": user, "error": None, "success": None
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

    csv_content, sha = _get_csv_from_github()
    if csv_content is None:
        ctx["error"] = "Impossibile leggere il CSV da GitHub. Controlla GITHUB_TOKEN e GITHUB_REPO su Railway."
        return templates.TemplateResponse("add_card.html", ctx)

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    set_name_safe = set_name.replace('"', '""')
    lang = language.strip() or "en"
    new_row = (f'webapp,binder,{name},{set_code.upper()},"{set_name_safe}",'
               f'{collector_number},{foil},{rarity},{quantity},,{scryfall_id},'
               f'{purchase_price},false,false,near_mint,{lang},EUR,{now}\n')

    updated = csv_content.rstrip("\n") + "\n" + new_row
    ok = _update_csv_on_github(updated, sha, f"Add {name} ({set_code.upper()}) via webapp")

    if ok:
        ctx["success"] = f"'{name}' aggiunta! Sarà monitorata dal prossimo aggiornamento prezzi."
    else:
        ctx["error"] = "Errore durante il salvataggio su GitHub. Controlla GITHUB_TOKEN."
    return templates.TemplateResponse("add_card.html", ctx)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
