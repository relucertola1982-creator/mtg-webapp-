import csv
import json
import time
import requests
from datetime import datetime
import os

# ========================
# CONFIGURAZIONE
# ========================
TELEGRAM_BOT_TOKEN = "8538688213:AAEVUvBGED58xKU5MO98e2jEAhCadVoR--o"
TELEGRAM_CHAT_ID = "1161277005"
SOGLIA_PERCENTUALE = 10
CSV_PATH = "ManaBox_Collection.csv"
PREZZI_SALVATI_PATH = "prezzi_riferimento.json"
STORICO_PATH = "storico_prezzi.json"
SLEEP_TRA_CARTE = 0.2
HEADERS = {"User-Agent": "MTGPriceTracker/1.0 (Ale823 personal collection tracker)", "Accept": "application/json"}

# ========================
# FUNZIONI
# ========================

def invia_telegram(messaggio):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": messaggio, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except Exception as e:
        print(f"Errore Telegram: {e}")
        return False

def get_prezzo_scryfall(nome, set_code, collector_number, foil=False, scryfall_id=None, language="en"):
    """Cerca prezzo su Scryfall con metodi multipli. Ritorna (prezzo, nome, frame_effects)."""

    def _pick_price(data, foil):
        prezzi = data.get("prices", {})
        eur_foil   = prezzi.get("eur_foil")
        eur_normal = prezzi.get("eur")
        if foil and eur_foil:   return float(eur_foil)
        if not foil and eur_normal: return float(eur_normal)
        if eur_foil:  return float(eur_foil)
        if eur_normal: return float(eur_normal)
        return None

    def _fx(data):
        return ",".join(data.get("frame_effects") or [])

    # Metodo 1: set+numero+lingua (più preciso per varianti JA ecc.)
    if language and language != "en":
        try:
            url = f"https://api.scryfall.com/cards/{set_code.lower()}/{collector_number}/{language}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.ok:
                data = r.json()
                p = _pick_price(data, foil)
                if p: return p, data.get("name", nome), _fx(data)
        except Exception:
            pass

    # Metodo 2: set + collector number
    try:
        url = f"https://api.scryfall.com/cards/{set_code.lower()}/{collector_number}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.ok:
            data = r.json()
            p = _pick_price(data, foil)
            if p: return p, data.get("name", nome), _fx(data)
    except Exception:
        pass

    # Metodo 3: Scryfall ID diretto
    if scryfall_id:
        try:
            url = f"https://api.scryfall.com/cards/{scryfall_id}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.ok:
                data = r.json()
                p = _pick_price(data, foil)
                if p: return p, data.get("name", nome), _fx(data)
        except Exception:
            pass

    # Metodo 4: nome fuzzy
    try:
        nome_ricerca = nome.split(" // ")[0].strip()
        url = "https://api.scryfall.com/cards/named"
        r = requests.get(url, headers=HEADERS, params={"fuzzy": nome_ricerca, "set": set_code.lower()}, timeout=10)
        if r.ok:
            data = r.json()
            p = _pick_price(data, foil)
            if p: return p, data.get("name", nome), _fx(data)
    except Exception:
        pass

    return None, nome, ""

def carica_prezzi_salvati():
    if os.path.exists(PREZZI_SALVATI_PATH):
        with open(PREZZI_SALVATI_PATH, "r") as f:
            return json.load(f)
    return {}

def salva_prezzi(prezzi):
    with open(PREZZI_SALVATI_PATH, "w") as f:
        json.dump(prezzi, f, indent=2, ensure_ascii=False)

def carica_storico():
    if os.path.exists(STORICO_PATH):
        try:
            with open(STORICO_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def salva_storico(storico):
    with open(STORICO_PATH, "w", encoding="utf-8") as f:
        json.dump(storico, f, indent=2, ensure_ascii=False)

def leggi_collection(csv_path):
    carte = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                purchase_price = float(row.get("Purchase price") or 0)
                if purchase_price >= 1.0:
                    finish = row.get("Foil", "normal").lower()
                    carte.append({
                        "nome": row["Name"],
                        "set_name": row["Set name"],
                        "set_code": row["Set code"],
                        "collector_number": row["Collector number"],
                        "scryfall_id": row["Scryfall ID"],
                        "foil": finish in ["foil", "etched"],
                        "finish": finish,
                        "quantita": int(row.get("Quantity") or 1),
                        "prezzo_acquisto": purchase_price,
                        "language": row.get("Language", "en")
                    })
            except (ValueError, KeyError):
                continue
    return carte

def controlla_prezzi():
    print(f"\n{'='*50}")
    print(f"Controllo prezzi: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*50}")

    carte = leggi_collection(CSV_PATH)
    prezzi_salvati = carica_prezzi_salvati()
    prezzi_aggiornati = {}
    alert_count = 0
    non_trovate = []

    print(f"Carte monitorate: {len(carte)}\n")

    for carta in carte:
        nome = carta["nome"]
        foil = carta["foil"]
        finish = carta["finish"]
        language = carta["language"]
        set_code = carta["set_code"]
        collector_number = carta["collector_number"]
        scryfall_id = carta["scryfall_id"]
        chiave = f"{set_code}_{collector_number}_{finish}_{language}"

        prezzo_attuale, nome_trovato, frame_effects = get_prezzo_scryfall(
            nome, set_code, collector_number, foil, scryfall_id, language
        )
        time.sleep(SLEEP_TRA_CARTE)

        if prezzo_attuale is None or prezzo_attuale == 0:
            non_trovate.append(f"{'✨' if foil else '📄'} {nome} ({set_code} #{collector_number})")
            continue

        prezzi_aggiornati[chiave] = {
            "nome": nome,
            "set": carta["set_name"],
            "set_code": set_code,
            "collector_number": collector_number,
            "prezzo": prezzo_attuale,
            "foil": foil,
            "finish": finish,
            "language": language,
            "frame_effects": frame_effects,
            "ultimo_aggiornamento": datetime.now().isoformat()
        }

        if chiave in prezzi_salvati:
            prezzo_precedente = prezzi_salvati[chiave]["prezzo"]
            if prezzo_precedente > 0:
                variazione = ((prezzo_attuale - prezzo_precedente) / prezzo_precedente) * 100
                simbolo = "🟢" if variazione > 0 else ("🔴" if variazione < 0 else "⚪")
                print(f"  {simbolo} {'✨' if foil else '📄'} {nome}: €{prezzo_precedente:.2f} → €{prezzo_attuale:.2f} ({variazione:+.1f}%)")

                if abs(variazione) >= SOGLIA_PERCENTUALE:
                    emoji = "📈" if variazione > 0 else "📉"
                    finish_label = {"foil": "✨ Foil", "etched": "🔆 Etched", "normal": "📄 Normal"}.get(finish, finish)
                    lang_label = language.upper() if language != "en" else ""
                    msg = (
                        f"{emoji} <b>MTG Price Alert! Ale823</b>\n\n"
                        f"<b>{nome}</b>\n"
                        f"Set: {carta['set_name']}\n"
                        f"{finish_label}{(' · ' + lang_label) if lang_label else ''}\n\n"
                        f"Precedente: <b>€{prezzo_precedente:.2f}</b>\n"
                        f"Attuale: <b>€{prezzo_attuale:.2f}</b>\n"
                        f"Variazione: <b>{variazione:+.1f}%</b>\n"
                        f"Prezzo acquisto: €{carta['prezzo_acquisto']:.2f}"
                    )
                    if invia_telegram(msg):
                        print(f"    ✅ Alert Telegram inviato!")
                        alert_count += 1
        else:
            print(f"  🆕 {'✨' if foil else '📄'} {nome}: €{prezzo_attuale:.2f} (primo rilevamento)")

    # Salva prezzi correnti
    salva_prezzi(prezzi_aggiornati)

    # Aggiorna storico (su GitHub tramite commit del workflow)
    storico = carica_storico()
    for chiave, dati in prezzi_aggiornati.items():
        prezzo = dati["prezzo"]
        punti = storico.get(chiave, [])
        if not punti or abs(punti[-1]["price"] - prezzo) > 0.001:
            punti.append({"price": prezzo, "date": dati["ultimo_aggiornamento"]})
            storico[chiave] = punti[-500:]  # massimo 500 punti per carta
    salva_storico(storico)

    print(f"\n{'='*50}")
    print(f"✅ Trovate e salvate: {len(prezzi_aggiornati)} carte")
    print(f"🚨 Alert inviati: {alert_count}")
    if non_trovate:
        print(f"⚠️  Non trovate su Scryfall ({len(non_trovate)}):")
        for c in non_trovate[:10]:
            print(f"     {c}")
        if len(non_trovate) > 10:
            print(f"     ... e altre {len(non_trovate)-10}")

# ========================
# AVVIO
# ========================
if __name__ == "__main__":
    print("🃏 MTG Price Tracker - Ale823")
    print(f"Soglia alert: ±{SOGLIA_PERCENTUALE}%")
    print(f"Chat Telegram ID: {TELEGRAM_CHAT_ID}")

    if not os.path.exists(PREZZI_SALVATI_PATH):
        print("\n⚠️  Prima esecuzione: salvo prezzi di riferimento...")
        print("Dalla prossima esecuzione riceverai gli alert!\n")

    controlla_prezzi()