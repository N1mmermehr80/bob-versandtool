"""
BOB Sendcloud-Versand
Liest den S4T-Versandexport ("GLS_*.csv", versendet wird mit DHL), korrigiert bekannte
Exportfehler, zeigt die Aufträge an, legt ausgewählte Sendungen bei Sendcloud an und
exportiert die Sendungsnummern.
"""
import base64
import configparser
import csv
import ctypes
import ctypes.wintypes
import datetime as dt
import io
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "BOB Sendcloud-Versand"
APP_VERSION = "1.0.0"  # wird beim Release-Build aus dem Git-Tag (v1.2.3) gesetzt
# GitHub-Repository für die Updateprüfung, Format "besitzer/repo". Leer = keine Updateprüfung.
UPDATE_REPO = "N1mmermehr80/bob-versandtool"
UPDATE_ASSET = "BOB-Sendcloud-Versand.exe"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BOB-Sendcloud-Versand")
CONFIG_PATH = os.path.join(APP_DIR, "config.ini")
LOG_PATH = os.path.join(APP_DIR, "sendungen_log.csv")
HISTORY_PATH = os.path.join(APP_DIR, "erfahrungswerte.json")
SESSION_PATH = os.path.join(APP_DIR, "arbeitsliste.json")
LABEL_DIR = os.path.join(APP_DIR, "labels")
PRINT_DIR = os.path.join(APP_DIR, "sammeldruck")
DONE_STATES = ("Label erstellt", "bereits vorhanden", "manuell versendet", "teilweise")
FILTERS = ["Alle", "Offen", "Bitte prüfen", "Nicht sendbar / Fehler", "Ausgewählt", "Versendet",
           "Label ungedruckt", "Nicht exportiert"]
FILTER_TABS = {"Alle": "Alle", "Offen": "Offen", "Bitte prüfen": "Prüfen", "Nicht sendbar / Fehler": "Fehler",
               "Ausgewählt": "Ausgewählt", "Versendet": "Versendet", "Label ungedruckt": "Ungedruckt",
               "Nicht exportiert": "Nicht exportiert"}
API_BASE = "https://panel.sendcloud.sc/api/v3"

DEFAULTS = {
    "default_weight_kg": "2.01",
    "shipping_option_de": "dhl_de:dhl_paket",
    "shipping_option_eu": "dhl_de:weltpaket",
    # Firmenspezifisch – bewusst leer (öffentliches Repository). Wird einmal unter Einstellungen
    # eingetragen und im gemeinsamen Ordner für alle Arbeitsplätze geteilt.
    "contract_id": "",
    "brand_id": "",
    "sender_name": "",
    "sender_company": "",
    "sender_email": "",
    "sender_phone": "",
    "sender_street": "",
    "sender_house_number": "",
    "sender_postal_code": "",
    "sender_city": "",
    "sender_country": "DE",
    "use_history": "1",
    "shared_dir": "",
    "update_repo": "",
    "label_paper": "A6",
    "print_direct": "0",
    "save_label_pdf": "0",
    "label_dir": "",
}

# Diese Einstellungen gelten für alle Arbeitsplätze (im gemeinsamen Ordner); Zugangsdaten bleiben pro PC.
SHARED_SETTINGS = ("default_weight_kg", "shipping_option_de", "shipping_option_eu", "contract_id", "brand_id",
                   "sender_name", "sender_company", "sender_email", "sender_phone", "sender_street",
                   "sender_house_number", "sender_postal_code", "sender_city", "sender_country", "use_history")
REQUIRED_SENDER = (("sender_company", "Absender Firma"), ("sender_street", "Absender Straße"),
                   ("sender_house_number", "Absender Hausnr."), ("sender_postal_code", "Absender PLZ"),
                   ("sender_city", "Absender Ort"), ("sender_country", "Absender Land"),
                   ("contract_id", "DHL-Vertrags-ID"))

EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
      "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}
COUNTRY_NAMES = {"DEUTSCHLAND": "DE", "GERMANY": "DE", "ÖSTERREICH": "AT", "AUSTRIA": "AT"}
EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
WHEELSET_RX = re.compile(r"kompletträder|komplettrad|rädersatz|radsatz|\bWKR\b|\bSKR\b|\d{3}/\d{2}\s?Z?R\s?\d{2}", re.I)
PLACEHOLDER_WEIGHT_KG = 0.05  # Artikelgewichte darunter (z. B. "1 g") gelten als nicht gepflegt


# --------------------------------------------------------------------------- Konfiguration

def _dpapi(data: bytes, encrypt: bool) -> bytes:
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = BLOB(len(data), buf), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI-Fehler")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(s: str) -> str:
    return base64.b64encode(_dpapi(s.encode(), True)).decode() if s else ""


def unprotect(s: str) -> str:
    try:
        return _dpapi(base64.b64decode(s), False).decode() if s else ""
    except Exception:
        return ""


class Config:
    def __init__(self):
        self.cp = configparser.ConfigParser()
        self.cp.read(CONFIG_PATH, encoding="utf-8")
        if "settings" not in self.cp:
            self.cp["settings"] = {}
        for k, v in DEFAULTS.items():
            self.cp["settings"].setdefault(k, v)
        if not self.public_key:
            self._import_env()

    def _import_env(self):
        """Beim ersten Start Schlüssel aus einer .env neben dem Programm übernehmen."""
        base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
        for p in (os.path.join(base, ".env"), os.path.join(base, "..", ".env"), os.path.join(os.getcwd(), ".env")):
            if os.path.isfile(p):
                env = {}
                for line in open(p, encoding="utf-8", errors="replace"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip("\"'")
                if env.get("SENDCLOUD_PUBLIC_KEY") and env.get("SENDCLOUD_SECRET_KEY"):
                    self.set_keys(env["SENDCLOUD_PUBLIC_KEY"], env["SENDCLOUD_SECRET_KEY"])
                    self.save()
                    return

    def __getitem__(self, k):
        return self.cp["settings"].get(k, DEFAULTS.get(k, ""))

    def __setitem__(self, k, v):
        self.cp["settings"][k] = str(v)

    @property
    def public_key(self):
        return unprotect(self.cp["settings"].get("public_key_enc", ""))

    @property
    def secret_key(self):
        return unprotect(self.cp["settings"].get("secret_key_enc", ""))

    def set_keys(self, pub, sec):
        self.cp["settings"]["public_key_enc"] = protect(pub.strip())
        self.cp["settings"]["secret_key_enc"] = protect(sec.strip())

    def save(self):
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            self.cp.write(f)


# --------------------------------------------------------------------------- Datei einlesen

def _num(s):
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_kg(value, unit):
    v = _num(value)
    if v is None:
        return None
    u = (unit or "").strip().lower()
    return v / 1000 if u in ("g", "gr", "gramm") else v


class Order:
    def __init__(self, ref):
        self.ref = ref
        self.name = self.company = self.street = self.house = self.addr2 = ""
        self.zip = self.city = self.country = self.email = self.phone = ""
        self.items = []          # physische Artikel
        self.discounts = []      # Gutschein-/Rabattzeilen (nicht im Paket)
        self.weight = 0.0            # Gewicht je Paket in kg
        self.parcels = 1             # Anzahl Pakete (je Paket eine eigene Sendung, z. B. Radsätze)
        self.weight_estimated = False
        self.weight_source = ""      # "Export", "Historie", "Standard", "manuell"
        self.hints = []
        self.selected = False
        self.status = "neu"
        self.tracking = ""
        self.parcel_id = ""
        self.error = ""
        self.labels = []             # Dateinamen der Label-PDFs in LABEL_DIR
        self.printed = ""            # Zeitpunkt des letzten Sammeldrucks
        self.exported = False
        self.note = ""               # Notiz bei manuell versendet
        self.locked_by = ""          # gemeinsamer Ordner: wer sendet gerade
        self.locked_at = ""
        self.version = 0             # gemeinsamer Ordner: Versionsstand der Datei
        self.updated_by = ""
        self.updated_at = ""
        self.local_msg = ""          # Hinweis nur für diesen Arbeitsplatz (nicht geteilt)

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d, keep_lock=False):
        o = cls(d.get("ref", ""))
        for k, v in d.items():
            if hasattr(o, k):
                setattr(o, k, v)
        if o.status == "wird gesendet…" and not (lock_fresh(d) and (keep_lock or d.get("locked_by") != USER)):
            # Senden wurde unterbrochen (Absturz/Beenden) – nicht bei laufender Reservierung eines Kollegen
            o.status = "Fehler" if not o.tracking else "teilweise"
            o.error = o.error or "Senden wurde unterbrochen – bitte in Sendcloud prüfen"
            o.locked_by = o.locked_at = ""
        return o

    @property
    def busy_elsewhere(self):
        return self.status == "wird gesendet…" and self.locked_by and self.locked_by != USER

    @property
    def done(self):
        return self.status in DONE_STATES

    @property
    def finished(self):
        """Vollständig erledigt: versendet, Labels gedruckt (falls vorhanden), Sendungsnummer exportiert (falls vorhanden)."""
        return self.done and self.status != "teilweise" and (self.printed or not self.labels) and \
            (self.exported or not self.tracking)

    @property
    def label_paths(self):
        return [os.path.join(LABEL_DIR, f) for f in self.labels if os.path.exists(os.path.join(LABEL_DIR, f))]

    @property
    def value(self):
        return round(sum(i["price"] * i["qty"] for i in self.items) +
                     sum(d["price"] * d["qty"] for d in self.discounts), 2)

    @property
    def items_text(self):
        return ", ".join(f'{i["qty"]:g}× {i["sku"] or i["name"][:30]}' for i in self.items)

    def validate(self):
        """Gibt blockierende Fehler zurück (leer = sendbar)."""
        err = []
        for label, v in (("Name", self.name), ("Straße", self.street), ("PLZ", self.zip),
                         ("Ort", self.city), ("Land", self.country)):
            if not v.strip():
                err.append(f"{label} fehlt")
        if self.country and not re.fullmatch(r"[A-Z]{2}", self.country):
            err.append("Ländercode ungültig")
        elif self.country and self.country not in EU:
            err.append("Nicht-EU-Land: Zolldaten nötig, bitte manuell in Sendcloud anlegen")
        if self.country == "DE" and not re.fullmatch(r"\d{5}", self.zip):
            err.append("PLZ ungültig")
        if not self.house.strip() and not re.search(r"\d", self.street + self.addr2):
            err.append("Hausnummer fehlt")
        if not self.items:
            err.append("keine Artikel")
        if not (0 < self.weight <= 31.5):
            err.append("Gewicht je Paket muss zwischen 0 und 31,5 kg liegen")
        if not (1 <= self.parcels <= 20):
            err.append("Paketanzahl muss zwischen 1 und 20 liegen")
        return err

    @property
    def needs_review(self):
        """Gewicht nur geschätzt und Sendung wertvoll oder Radsatz -> vor dem Senden prüfen."""
        if self.done:
            return False
        return self.weight_source in ("Standard", "Regel") and (self.value > 500 or self.weight_source == "Regel")

    @property
    def weight_text(self):
        w = f"{self.weight:.2f}".replace(".", ",")
        return (f"{self.parcels} × {w}" if self.parcels > 1 else w) + (" *" if self.weight_estimated else "")


def read_export(path, default_weight):
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    header = [h.strip().upper() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(header):
                if h == n or h.startswith(n + " ("):
                    return i
        return None

    idx = {
        "name": col("NAME"), "company": col("COMPANY NAME"), "house": col("HOUSE NUMBER"),
        "street": col("ADDRESS"), "addr2": col("ADDRESS 2"), "zip": col("POSTAL CODE"),
        "city": col("CITY"), "state": col("STATE CODE"), "country": col("COUNTRY CODE"),
        "email": col("EMAIL"), "phone": col("TELEPHONE"), "oweight": col("ORDER WEIGHT"),
        "ref": col("ORDER REFERENCE"), "item_id": col("ITEM ID"), "item_name": col("ITEM NAME"),
        "qty": col("ITEM QUANTITY"), "price": col("ITEM UNIT PRICE VALUE"),
        "iweight": col("ITEM WEIGHT VALUE"), "iunit": col("ITEM WEIGHT UNIT"), "sku": col("ITEM SKU"),
        "ean": col("ITEM EAN"),
    }
    if idx["ref"] is None or idx["name"] is None:
        raise ValueError("Unbekanntes Dateiformat: Spalten 'NAME' und 'ORDER REFERENCE' nicht gefunden.")

    def g(r, k):
        i = idx[k]
        return r[i].strip() if i is not None and i < len(r) else ""

    orders, order_weight = {}, {}
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        ref = g(r, "ref")
        o = orders.get(ref)
        if o is None:
            o = orders[ref] = Order(ref)
            o.name, o.company, o.house, o.street = g(r, "name"), g(r, "company"), g(r, "house"), g(r, "street")
            o.addr2, o.zip, o.city, o.phone = g(r, "addr2"), g(r, "zip"), g(r, "city"), g(r, "phone")
            # Exportfehler: Land steht in STATE CODE, Ort in COUNTRY CODE
            cc, st = g(r, "country").upper(), g(r, "state").upper()
            if re.fullmatch(r"[A-Z]{2}", cc):
                o.country = cc
            elif re.fullmatch(r"[A-Z]{2}", st):
                o.country = st
                if cc:
                    o.hints.append("Land aus STATE CODE übernommen (Exportfehler)")
            else:
                o.country = COUNTRY_NAMES.get(cc, "DE")
                o.hints.append(f"Land unklar ('{cc or st}'), DE angenommen")
            # Exportfehler: E-Mail nach 30 Zeichen abgeschnitten
            em = g(r, "email")
            if re.search(r"@members\.[a-z.]*$", em, re.I) and not em.lower().endswith("@members.ebay.com"):
                em = em.split("@")[0] + "@members.ebay.com"
                o.hints.append("eBay-E-Mail vervollständigt")
            if em and not EMAIL_RX.match(em):
                o.hints.append(f"E-Mail ungültig, nicht übernommen ({em})")
                em = ""
            o.email = em
            if len(o.company) == 30:
                o.hints.append("Firmenname evtl. abgeschnitten (30 Zeichen)")
            # Hausnummer fehlt -> aus Zusatz oder Straße holen
            if not o.house:
                m = re.fullmatch(r"(.*?[A-Za-zÄÖÜäöüß.])\s*(\d+\s?[a-zA-Z]?(?:\s?-\s?\d+\s?[a-zA-Z]?)?)", o.street)
                m2 = re.fullmatch(r"(.*?[A-Za-zÄÖÜäöüß.])\s*(\d+\s?[a-zA-Z]?)\b[\s,(/-]+(.+)", o.street)
                if m:
                    o.street, o.house = m.group(1).strip(" ,"), m.group(2).replace(" ", "")
                    o.hints.append("Hausnummer aus Straße getrennt")
                elif m2:
                    rest = m2.group(3).strip(" ()")
                    o.street, o.house = m2.group(1).strip(" ,"), m2.group(2).replace(" ", "")
                    o.addr2 = (rest + " " + o.addr2).strip() if rest else o.addr2
                    o.hints.append("Hausnummer und Zusatz aus Straße getrennt – bitte prüfen")
                elif re.fullmatch(r"\d+\s?[a-zA-Z]?(\s?-\s?\d+\s?[a-zA-Z]?)?", o.addr2):
                    o.house, o.addr2 = o.addr2, ""
                    o.hints.append("Hausnummer aus Adresszusatz übernommen")
                else:
                    o.hints.append("Hausnummer fehlt")
            if o.country == "DE" and re.fullmatch(r"\d{4}", o.zip):
                o.zip = "0" + o.zip
                o.hints.append("PLZ um führende 0 ergänzt")
            ow = _num(g(r, "oweight"))
            order_weight[ref] = ow if ow and ow > 0 else None
        qty = _num(g(r, "qty")) or 1
        price = _num(g(r, "price")) or 0.0
        item = {"sku": g(r, "sku") or g(r, "item_id"), "name": g(r, "item_name"), "qty": qty, "price": price,
                "ean": g(r, "ean"), "weight": _to_kg(g(r, "iweight"), g(r, "iunit"))}
        if price < 0:
            o.discounts.append(item)
        else:
            o.items.append(item)

    for ref, o in orders.items():
        if o.discounts:
            o.hints.append(f"{len(o.discounts)} Rabatt-/Gutscheinzeile(n) nicht ins Paket übernommen")
        valid = [i for i in o.items if i["weight"] and i["weight"] >= PLACEHOLDER_WEIGHT_KG]
        total = sum(i["weight"] * i["qty"] for i in valid)
        if order_weight.get(ref):
            o.weight, o.weight_source = order_weight[ref], "Export"
        elif o.items and len(valid) == len(o.items):
            o.weight, o.weight_source = round(total, 3), "Export"
        else:
            o.weight, o.weight_source = max(round(total, 3), default_weight), "Standard"
            o.weight_estimated = True
            o.hints.append(f"Gewicht nicht gepflegt, {o.weight:.2f} kg angesetzt".replace(".", ","))
            if len(o.items) == 1 and o.items[0]["qty"] == 1 and WHEELSET_RX.search(o.items[0]["name"]):
                o.parcels, o.weight, o.weight_source = 4, 20.0, "Regel"
                o.hints[-1] = "Radsatz erkannt: 4 Pakete à 20 kg angesetzt – bitte prüfen"
    return list(orders.values())


def apply_history(orders, history, default_weight=2.01):
    """Paketanzahl und Gewicht aus früheren Sendungen übernehmen, wenn der Export kein Gewicht liefert.
    history: {SKU: {"parcels": n, "weight": kg je Paket, "count": Anzahl Aufträge}}"""
    changed = 0
    for o in orders:
        if o.weight_source not in ("Standard", "Regel", "Historie") or o.tracking:
            continue
        stats = [history.get(i["sku"].upper()) for i in o.items]
        if not o.items or not all(stats):
            continue
        if len(o.items) == 1 and o.items[0]["qty"] == 1:
            parcels, weight = stats[0]["parcels"], stats[0]["weight"]
        elif all(s["parcels"] == 1 for s in stats):
            parcels, weight = 1, round(sum(s["weight"] * i["qty"] for s, i in zip(stats, o.items)), 3)
            if weight > 31.5:
                continue
        else:
            continue
        if parcels == 1 and weight <= default_weight:
            continue  # leichtere Erfahrungswerte (oft Platzhalter wie 1 kg) nicht übernehmen
        o.parcels, o.weight, o.weight_source, o.weight_estimated = parcels, weight, "Historie", True
        o.hints = [h for h in o.hints if not h.startswith(("Gewicht nicht gepflegt", "Aus früheren", "Radsatz erkannt"))]
        o.hints.append(f"Aus früheren Sendungen: {parcels} Paket(e) à {weight:.2f} kg".replace(".", ","))
        changed += 1
    return changed


# --------------------------------------------------------------------------- Sendcloud

class SendcloudError(Exception):
    pass


class Sendcloud:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        token = base64.b64encode(f"{cfg.public_key}:{cfg.secret_key}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token, "Accept": "application/json",
                        "User-Agent": "BOB-Sendcloud-Versand/1.0"}

    def _call(self, method, path, body=None, raw=False, accept=None):
        url = path if path.startswith("http") else API_BASE + path
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = r.read()
                    return data if raw else json.loads(data or b"{}")
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                msg = e.read().decode(errors="replace")
                try:
                    j = json.loads(msg)
                    errs = j.get("errors") or j.get("error") or j
                    msg = "; ".join(f'{x.get("source", {}).get("pointer", "")} {x.get("detail") or x.get("title")}'.strip()
                                    for x in errs) if isinstance(errs, list) else json.dumps(errs, ensure_ascii=False)
                except Exception:
                    pass
                if e.code == 401:
                    msg = "Zugangsdaten ungültig (HTTP 401) – bitte in den Einstellungen prüfen"
                raise SendcloudError(f"HTTP {e.code}: {msg[:500]}")
            except urllib.error.URLError as e:
                raise SendcloudError(f"Keine Verbindung zu Sendcloud: {e.reason}")

    def test(self):
        self._call("GET", "/addresses/sender-addresses")

    def existing(self, order_number):
        """Aktive Sendungen mit Trackingnummer zu dieser Auftragsnummer."""
        d = self._call("GET", "/shipments?" + urllib.parse.urlencode({"order_number": order_number}))
        found = []
        for x in d.get("data", []):
            if str(x.get("order_number")) != str(order_number):
                continue
            for p in x.get("parcels", []):
                if p.get("tracking_number") and p["status"]["code"] not in ("CANCELLED", "CANCELLATION_REQUESTED",
                                                                            "ANNOUNCEMENT_FAILED"):
                    found.append(p)
        return found

    def history(self, days=120, progress=None):
        """Erfahrungswerte je SKU aus den Sendungen der letzten `days` Tage."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = API_BASE + "/shipments?" + urllib.parse.urlencode({"updated_after": since, "page_size": 100})
        per_order = {}
        pages = 0
        while url:
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    data, link = json.loads(r.read()), r.headers.get("Link", "")
            except urllib.error.HTTPError as e:
                raise SendcloudError(f"HTTP {e.code} beim Laden der Historie")
            except urllib.error.URLError as e:
                raise SendcloudError(f"Keine Verbindung zu Sendcloud: {e.reason}")
            for x in data.get("data", []):
                for p in x.get("parcels", []):
                    if not p.get("tracking_number") or p["status"]["code"] in ("CANCELLED", "CANCELLATION_REQUESTED",
                                                                               "ANNOUNCEMENT_FAILED"):
                        continue
                    skus = {(i.get("sku") or "").upper() for i in p.get("parcel_items") or [] if i.get("sku")}
                    if len(skus) != 1:
                        continue
                    w = _num((p.get("weight") or {}).get("value"))
                    if w:
                        per_order.setdefault((str(x.get("order_number")), skus.pop()), []).append(w)
            m = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
            url = m.group(1) if m else None
            pages += 1
            if progress:
                progress(pages)
        stats = {}
        for (_, sku), weights in per_order.items():
            stats.setdefault(sku, []).append((len(weights), max(set(weights), key=weights.count)))
        result = {}
        for sku, lst in stats.items():
            parcels = max(set(n for n, _ in lst), key=[n for n, _ in lst].count)
            ws = [w for n, w in lst if n == parcels]
            result[sku] = {"parcels": parcels, "weight": max(set(ws), key=ws.count), "count": len(lst)}
        return result

    def announce(self, o: Order):
        """Legt eine Sendung (ein Paket) an. Bei mehreren Paketen wird die Methode je Paket aufgerufen."""
        c = self.cfg
        n_units = sum(i["qty"] for i in o.items) or 1
        all_weighted = o.parcels == 1 and all(i["weight"] and i["weight"] >= PLACEHOLDER_WEIGHT_KG for i in o.items)
        items = []
        for i in o.items:
            w = i["weight"] if all_weighted else o.weight / n_units
            it = {"description": (i["name"] or i["sku"])[:255], "quantity": int(i["qty"]) or 1,
                  "sku": i["sku"][:255], "origin_country": "DE",
                  "price": {"value": f'{i["price"] / o.parcels:.2f}', "currency": "EUR"},
                  "weight": {"value": f"{w:.3f}", "unit": "kg"}}
            if re.fullmatch(r"\d{8,14}", i["ean"]):
                it["ean"] = i["ean"]
            items.append(it)
        to = {"name": o.name, "address_line_1": o.street, "house_number": o.house, "postal_code": o.zip,
              "city": o.city, "country_code": o.country}
        if o.company:
            to["company_name"] = o.company
        if o.addr2:
            to["address_line_2"] = o.addr2
        if o.email:
            to["email"] = o.email
        if o.phone:
            to["phone_number"] = o.phone
        option = c["shipping_option_de"] if o.country == c["sender_country"] else c["shipping_option_eu"]
        body = {
            "label_details": {"mime_type": "application/pdf", "dpi": 72},
            "from_address": {"name": c["sender_name"], "company_name": c["sender_company"],
                             "email": c["sender_email"], "phone_number": c["sender_phone"],
                             "address_line_1": c["sender_street"], "house_number": c["sender_house_number"],
                             "postal_code": c["sender_postal_code"], "city": c["sender_city"],
                             "country_code": c["sender_country"]},
            "to_address": to,
            "ship_with": {"type": "shipping_option_code",
                          "properties": {"shipping_option_code": option, "contract_id": int(c["contract_id"])}},
            "order_number": o.ref,
            "total_order_price": {"value": f"{max(o.value, 0) / o.parcels:.2f}", "currency": "EUR"},
            "parcels": [{"weight": {"value": f"{o.weight:.3f}", "unit": "kg"}, "parcel_items": items,
                         "label_notes": [o.ref]}],
        }
        if c["brand_id"].strip():
            body["brand_id"] = int(c["brand_id"])
        res = self._call("POST", "/shipments/announce", body)
        d = res.get("data", res)
        p = d["parcels"][0]
        if not p.get("tracking_number"):
            errs = d.get("errors") or p.get("errors") or p["status"].get("message")
            raise SendcloudError(f"Keine Sendungsnummer erhalten: {errs}")
        return p

    def label_pdf(self, parcel_id):
        """Einzel-Label (natives Format, i. d. R. A6) als PDF."""
        return self._call("GET", f"/parcels/{int(parcel_id)}/documents/label", raw=True, accept="application/pdf")

    def labels_bulk(self, parcel_ids, paper="A4"):
        """Mehrere Labels in einem PDF (bei A4 mehrere Labels pro Seite), max. 20 je Aufruf."""
        q = urllib.parse.urlencode([("parcels", int(p)) for p in parcel_ids] + [("paper_size", paper)])
        return self._call("GET", "/parcel-documents/label?" + q, raw=True, accept="application/pdf")


A6_PT = (297.64, 419.53)  # 105 × 148 mm


def to_a6(pdf: bytes, margin_pt=4.0) -> bytes:
    """Jede Seite maximal groß und zentriert auf eine echte A6-Seite setzen.
    Sendcloud liefert DHL-Labels als ~69 × 140 mm (auf 70 % verkleinertes 99 × 200 mm-Label)."""
    from pypdf import PdfReader, PdfWriter, Transformation
    r, w = PdfReader(io.BytesIO(pdf)), PdfWriter()
    for src in r.pages:
        box = src.cropbox
        sw, sh = float(box.width), float(box.height)
        if abs(sw - A6_PT[0]) < 2 and abs(sh - A6_PT[1]) < 2:
            w.add_page(src)
            continue
        rotate = sw > sh  # Querformat hochkant drehen
        bw, bh = (sh, sw) if rotate else (sw, sh)
        s = min((A6_PT[0] - 2 * margin_pt) / bw, (A6_PT[1] - 2 * margin_pt) / bh)
        page = w.add_blank_page(*A6_PT)
        t = Transformation().translate(-float(box.left), -float(box.bottom))
        if rotate:
            t = t.rotate(90).translate(sh, 0)
        t = t.scale(s).translate((A6_PT[0] - bw * s) / 2, (A6_PT[1] - bh * s) / 2)
        page.merge_transformed_page(src, t)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def store_label(o: Order, tracking, pdf: bytes, cfg):
    try:
        pdf = to_a6(pdf)
    except Exception as e:
        o.hints.append(f"Label-PDF nicht auf A6 umgerechnet: {e}")
    os.makedirs(LABEL_DIR, exist_ok=True)
    fn = re.sub(r"[^\w.-]", "_", f"{o.ref}_{tracking}.pdf")
    with open(os.path.join(LABEL_DIR, fn), "wb") as f:
        f.write(pdf)
    if fn not in o.labels:
        o.labels.append(fn)
    if cfg["save_label_pdf"] == "1" and cfg["label_dir"]:
        try:
            os.makedirs(cfg["label_dir"], exist_ok=True)
            with open(os.path.join(cfg["label_dir"], fn), "wb") as f:
                f.write(pdf)
        except OSError as e:
            o.hints.append(f"Label-PDF nicht in Ordner kopiert: {e}")


def merge_pdfs(parts, target):
    """PDF-Teile (Pfade oder Bytes) zu einer Datei zusammenfügen."""
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in parts:
        r = PdfReader(io.BytesIO(p) if isinstance(p, bytes) else p)
        for page in r.pages:
            w.add_page(page)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        w.write(f)
    return len(w.pages)


# --------------------------------------------------------------------------- Gemeinsamer Ordner (mehrere Arbeitsplätze)

LOCAL_ONLY = ("selected", "version", "updated_by", "updated_at", "local_msg")  # wird nicht geteilt
LOCK_TIMEOUT = 15 * 60  # Reservierung „wird gesendet“ verfällt nach 15 min (z. B. Absturz)


class Conflict(Exception):
    def __init__(self, msg, current=None):
        super().__init__(msg)
        self.current = current


class Gone(Exception):
    pass


def shared_data(o: Order):
    return {k: v for k, v in o.to_dict().items() if k not in LOCAL_ONLY}


def shared_json(o: Order):
    return json.dumps(shared_data(o), sort_keys=True, ensure_ascii=False)


def lock_fresh(data):
    at = data.get("locked_at")
    try:
        return bool(data.get("locked_by")) and time.time() - dt.datetime.fromisoformat(at).timestamp() < LOCK_TIMEOUT
    except (TypeError, ValueError):
        return False


class FolderBackend:
    """Arbeitsliste als Dateien in einem Netzwerkordner – ohne Server, ohne Adminrechte.
    auftraege/<ref>.json  je Auftrag (mit Versionsnummer)   sperren/<ref>.lock  kurze Schreibsperre
    ausgeblendet/<ref>.json  gelöscht/erledigt               labels/*.pdf        protokoll/<benutzer>.csv"""

    def __init__(self, root, user):
        self.root, self.user = root, user
        self.dirs = {k: os.path.join(root, k) for k in ("auftraege", "sperren", "ausgeblendet", "labels", "protokoll")}
        for d in self.dirs.values():
            os.makedirs(d, exist_ok=True)
        self.mutex = threading.Lock()

    @staticmethod
    def _safe(ref):
        return re.sub(r"[^\w.-]", "_", str(ref))

    def _path(self, kind, ref):
        return os.path.join(self.dirs[kind], self._safe(ref) + (".lock" if kind == "sperren" else ".json"))

    def _read(self, path):
        for attempt in range(5):  # Datei kann gerade ersetzt werden
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return None
            except (json.JSONDecodeError, PermissionError):
                time.sleep(0.1)
        raise OSError(f"Datei nicht lesbar: {path}")

    def _write(self, path, obj):
        tmp = f"{path}.{self._safe(self.user)}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Ziel wird gerade von einem anderen Platz gelesen
                time.sleep(0.1)
        os.remove(tmp)
        raise OSError(f"Datei nicht schreibbar: {path}")

    def _locked(self, ref):
        """Kurze exklusive Schreibsperre je Auftrag (atomares Anlegen der Sperrdatei)."""
        backend, lock = self, self._path("sperren", ref)

        class _L:
            def __enter__(self):
                deadline = time.time() + 5
                while True:
                    try:
                        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, f"{backend.user} {dt.datetime.now().isoformat()}".encode())
                        os.close(fd)
                        return
                    except FileExistsError:
                        try:  # verwaiste Sperre (Absturz) nach 30 s brechen
                            if time.time() - os.path.getmtime(lock) > 30:
                                os.remove(lock)
                                continue
                        except FileNotFoundError:
                            continue
                        if time.time() > deadline:
                            raise Conflict("Auftrag ist gerade gesperrt – bitte gleich nochmal versuchen")
                        time.sleep(0.1)

            def __exit__(self, *a):
                try:
                    os.remove(lock)
                except FileNotFoundError:
                    pass
        return _L()

    # ----- Lesen
    def signature(self):
        """Ändert sich, sobald irgendwer etwas schreibt (für den Abgleich)."""
        sig = []
        for kind in ("auftraege", "ausgeblendet"):
            with os.scandir(self.dirs[kind]) as it:
                sig += [(e.name, e.stat().st_mtime_ns, e.stat().st_size) for e in it if e.name.endswith(".json")]
        return hash(tuple(sorted(sig)))

    def load_all(self):
        orders = {}
        with os.scandir(self.dirs["auftraege"]) as it:
            for e in it:
                if e.name.endswith(".json"):
                    rec = self._read(e.path)
                    # Konfliktkopien von Sync-Diensten (z. B. „17588-PC01.json“) ignorieren
                    if not rec or self._safe(rec.get("ref", "")) + ".json" != e.name:
                        continue
                    if rec["ref"] not in orders or rec["version"] > orders[rec["ref"]]["version"]:
                        orders[rec["ref"]] = rec
        orders = list(orders.values())
        orders.sort(key=lambda r: r.get("created", ""))
        hidden = {"deleted": {}, "completed": {}}
        with os.scandir(self.dirs["ausgeblendet"]) as it:
            for e in it:
                if e.name.endswith(".json"):
                    h = self._read(e.path) or {}
                    hidden.setdefault(h.get("kind", "deleted"), {})[h.get("ref", e.name[:-5])] = h.get("at", "")
        return orders, hidden

    # ----- Schreiben
    def put(self, ref, version, data):
        with self.mutex, self._locked(ref):
            cur = self._read(self._path("auftraege", ref))
            if cur is None:
                raise Gone(ref)
            if cur["version"] != version:
                raise Conflict(f"Auftrag {ref} wurde von {cur.get('updated_by')} geändert", cur)
            rec = {**cur, "version": version + 1, "data": data, "updated_by": self.user,
                   "updated_at": dt.datetime.now().isoformat(timespec="seconds")}
            self._write(self._path("auftraege", ref), rec)
            return rec

    def claim(self, ref):
        """Auftrag zum Senden reservieren. Nur einer kann gewinnen."""
        with self.mutex, self._locked(ref):
            cur = self._read(self._path("auftraege", ref))
            if cur is None:
                raise Gone(ref)
            d = cur["data"]
            if d.get("status") in DONE_STATES or d.get("tracking"):
                raise Conflict(f"bereits erledigt ({d.get('status')}, von {cur.get('updated_by')})", cur)
            if lock_fresh(d) and d.get("locked_by") != self.user:
                raise Conflict(f"wird gerade von {d['locked_by']} gesendet", cur)
            d.update(locked_by=self.user, locked_at=dt.datetime.now().isoformat(timespec="seconds"),
                     status="wird gesendet…")
            rec = {**cur, "version": cur["version"] + 1, "data": d, "updated_by": self.user,
                   "updated_at": dt.datetime.now().isoformat(timespec="seconds")}
            self._write(self._path("auftraege", ref), rec)
            return rec

    def add(self, data):
        """Neuen Auftrag anlegen, falls weder vorhanden noch ausgeblendet. Rückgabe: 'added'|'already'|kind."""
        ref = data["ref"]
        with self.mutex, self._locked(ref):
            h = self._read(self._path("ausgeblendet", ref))
            if h:
                return h.get("kind", "deleted")
            if os.path.exists(self._path("auftraege", ref)):
                return "already"
            now = dt.datetime.now().isoformat(timespec="seconds")
            self._write(self._path("auftraege", ref), {"ref": ref, "version": 1, "data": data, "created": now,
                                                       "updated_by": self.user, "updated_at": now})
            return "added"

    def hide(self, ref, kind):
        with self.mutex, self._locked(ref):
            self._write(self._path("ausgeblendet", ref), {"ref": ref, "kind": kind, "by": self.user,
                                                         "at": dt.datetime.now().isoformat(timespec="seconds")})
            try:
                os.remove(self._path("auftraege", ref))
            except FileNotFoundError:
                pass

    def unhide(self, ref):
        try:
            os.remove(self._path("ausgeblendet", ref))
        except FileNotFoundError:
            pass

    def get_settings(self):
        return self._read(os.path.join(self.root, "einstellungen.json")) or {}

    def put_settings(self, settings):
        with self.mutex:
            self._write(os.path.join(self.root, "einstellungen.json"),
                        {**settings, "_geaendert_von": self.user,
                         "_geaendert_am": dt.datetime.now().isoformat(timespec="seconds")})

    def log(self, row):
        p = os.path.join(self.dirs["protokoll"], self._safe(self.user) + ".csv")
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";", lineterminator="\r\n")
            if new:
                w.writerow(["Zeitpunkt", "Benutzer", "Auftragsnummer", "Sendungsnummer", "Parcel-ID", "Empfänger",
                            "Hinweis"])
            w.writerow(row[:1] + [self.user] + row[1:])


# --------------------------------------------------------------------------- Updates über GitHub Releases

def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def check_update(repo):
    """Neueste Version auf GitHub abfragen. Rückgabe: dict oder None (kein Update)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": f"BOB-Sendcloud-Versand/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.loads(r.read())
    if rel.get("draft") or rel.get("prerelease") or _vtuple(rel.get("tag_name")) <= _vtuple(APP_VERSION):
        return None
    asset = next((a for a in rel.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
    if not asset:
        return None
    return {"version": rel["tag_name"].lstrip("vV"), "notes": (rel.get("body") or "").strip(),
            "url": asset["browser_download_url"], "size": asset.get("size"),
            "sha256": (asset.get("digest") or "").removeprefix("sha256:"), "page": rel.get("html_url", "")}


def install_update(info, progress=None):
    """Neue exe neben die laufende laden, prüfen und austauschen. Kein Adminrecht nötig,
    solange der Programmordner beschreibbar ist. Gibt den Pfad der (neuen) exe zurück."""
    import hashlib
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Updates funktionieren nur in der exe-Version.")
    exe = sys.executable
    new, old = exe + ".neu", exe + ".alt"
    h, done = hashlib.sha256(), 0
    req = urllib.request.Request(info["url"], headers={"User-Agent": f"BOB-Sendcloud-Versand/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(new, "wb") as f:
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and info.get("size"):
                progress(done / info["size"])
    try:
        with open(new, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("Download ist keine Windows-Programmdatei.")
        if info.get("size") and done != info["size"]:
            raise RuntimeError("Download unvollständig.")
        if info.get("sha256") and h.hexdigest() != info["sha256"].lower():
            raise RuntimeError("Prüfsumme stimmt nicht – Update verworfen.")
        if os.path.exists(old):
            os.remove(old)
        os.replace(exe, old)          # laufende exe darf umbenannt, aber nicht überschrieben werden
        try:
            os.replace(new, exe)
        except OSError:
            os.replace(old, exe)      # zurückrollen
            raise
    except Exception:
        if os.path.exists(new):
            os.remove(new)
        raise
    return exe


def cleanup_old_update():
    if getattr(sys, "frozen", False):
        for p in (sys.executable + ".alt", sys.executable + ".neu"):
            try:
                os.remove(p)
            except OSError:
                pass


BACKEND = None  # wird gesetzt, wenn ein gemeinsamer Ordner eingestellt ist
USER = os.environ.get("USERNAME") or os.environ.get("USER") or "unbekannt"


def append_log(o: Order, note):
    if BACKEND is not None:
        try:
            BACKEND.log([dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), o.ref, o.tracking, o.parcel_id, o.name,
                         note])
        except OSError:
            pass  # zusätzlich lokal protokolliert
    os.makedirs(APP_DIR, exist_ok=True)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\r\n")
        if new:
            w.writerow(["Zeitpunkt", "Auftragsnummer", "Sendungsnummer", "Parcel-ID", "Empfänger", "Hinweis"])
        w.writerow([dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), o.ref, o.tracking, o.parcel_id, o.name, note])


# --------------------------------------------------------------------------- Oberfläche

COLS = [("sel", "", 34), ("ref", "Auftrag", 70), ("name", "Name", 170), ("company", "Firma", 120),
        ("street", "Straße", 150), ("house", "Nr.", 50), ("zip", "PLZ", 60), ("city", "Ort", 120),
        ("country", "Land", 45), ("items", "Artikel", 200), ("value", "Wert €", 75),
        ("weight", "Gewicht kg", 80), ("status", "Status", 120), ("label", "Label", 95),
        ("tracking", "Sendungsnummer", 170)]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        cleanup_old_update()
        self.geometry("1500x760")
        self.minsize(1000, 500)
        self.cfg = Config()
        self.orders = []
        self.source = ""
        self.deleted = {}            # Auftragsnummer -> Zeitpunkt, aus der Liste gelöscht
        self.completed = {}          # Auftragsnummer -> Zeitpunkt, erledigt und automatisch entfernt
        self.q = queue.Queue()
        self.busy = False
        self._synced = {}            # gemeinsamer Ordner: zuletzt abgeglichener Stand je Auftrag
        self._sig = None
        self._sync_running = False
        self.offline = False
        self._build()
        self._init_backend()
        self.load_session()
        removed = self._cleanup_finished()  # Altlasten: schon gedruckt und exportiert
        if removed:
            self._update_status(f"{len(removed)} erledigte Aufträge (gedruckt und exportiert) aus der Liste entfernt.")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)
        if BACKEND is not None:
            self.after(3000, self._sync_tick)
        self.after(5000, self._update_tick)
        if not self.cfg.public_key:
            self.after(300, lambda: (messagebox.showinfo(APP_NAME, "Bitte zuerst die Sendcloud-Zugangsdaten in den "
                                                                    "Einstellungen hinterlegen."), self.settings()))

    def _build(self):
        apply_theme(self)
        self.configure(bg=C["hall"])

        # ── Kopfleiste (Graphit) mit Aktionen
        bar = tk.Frame(self, bg=C["steel"], padx=14, pady=10)
        bar.pack(fill="x")
        tk.Label(bar, text="BOB Versand", bg=C["steel"], fg="white", font=F["brand"]).pack(side="left", padx=(0, 22))
        self.btn_open = FlatButton(bar, "Importieren…", self.open_file, "bar")
        self.btn_open.pack(side="left")
        tk.Frame(bar, bg=C["steel_line"], width=1).pack(side="left", fill="y", padx=10, pady=4)
        for text, cmd in (("Bearbeiten", self.edit_current), ("Manuell versendet…", self.mark_manual),
                          ("Löschen", self.delete_orders)):
            FlatButton(bar, text, cmd, "bar").pack(side="left", padx=(0, 2))
        FlatButton(bar, "Einstellungen", self.settings, "bar").pack(side="right")
        tk.Frame(bar, bg=C["steel_line"], width=1).pack(side="right", fill="y", padx=10, pady=4)
        self.btn_send = FlatButton(bar, "Ausgewählte senden", self.send_selected, "primary")
        self.btn_send.pack(side="right", padx=(8, 0))
        self.btn_export = FlatButton(bar, "Exportieren…", self.export, "bar")
        self.btn_export.pack(side="right", padx=(2, 0))
        self.btn_print = FlatButton(bar, "Labels drucken…", self.print_labels, "bar")
        self.btn_print.pack(side="right")

        # ── Update-Hinweis (nur sichtbar, wenn eine neue Version bereitsteht)
        self.update_bar = tk.Frame(self, bg=C["signal"], padx=14, pady=7)
        self.update_text = tk.Label(self.update_bar, text="", bg=C["signal"], fg=C["ink"], font=F["body_b"])
        self.update_text.pack(side="left")
        self.update_later = tk.Label(self.update_bar, text="Später", bg=C["signal"], fg=C["ink"], font=F["body"],
                                     cursor="hand2", padx=10)
        self.update_later.pack(side="right")
        self.update_later.bind("<Button-1>", lambda e: self.update_bar.pack_forget())
        self.update_btn = tk.Label(self.update_bar, text="Jetzt aktualisieren", bg=C["steel"], fg="white",
                                   font=F["button"], cursor="hand2", padx=14, pady=4)
        self.update_btn.pack(side="right", padx=(0, 6))
        self.update_btn.bind("<Button-1>", lambda e: self.do_update())
        self._update_anchor = bar

        # ── Suche + Filter-Reiter mit Zählern
        sbar = tk.Frame(self, bg=C["hall"], padx=14, pady=10)
        sbar.pack(fill="x")
        box = tk.Frame(sbar, bg=C["paper"], highlightthickness=1, highlightbackground=C["line_strong"],
                       highlightcolor=C["steel"])
        box.pack(side="left")
        tk.Label(box, text="⌕", bg=C["paper"], fg=C["muted"], font=(F["body"][0], 13)).pack(side="left", padx=(8, 2))
        self.search = tk.StringVar()
        self.search_entry = tk.Entry(box, textvariable=self.search, width=34, relief="flat", bd=0, bg=C["paper"],
                                     fg=C["ink"], insertbackground=C["ink"], font=F["body_l"])
        self.search_entry.pack(side="left", ipady=6)
        self.search_clear = tk.Label(box, text="✕", bg=C["paper"], fg=C["muted"], font=F["body"], cursor="hand2",
                                     padx=8)
        self.search_clear.pack(side="left")
        self.search_clear.bind("<Button-1>", lambda e: (self.search.set(""), self.search_entry.focus_set()))
        self.search_ph = Placeholder(self.search_entry, "Auftrag, Name, Artikel suchen …")
        box.bind("<Button-1>", lambda e: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", lambda e: box.configure(highlightbackground=C["steel"]), add="+")
        self.search_entry.bind("<FocusOut>", lambda e: box.configure(highlightbackground=C["line_strong"]), add="+")

        self.filter = tk.StringVar(value=FILTERS[0])
        self.tabs = FilterTabs(sbar, self.filter, lambda: self.refresh())
        self.tabs.pack(side="left", padx=(22, 0))
        sel = tk.Frame(sbar, bg=C["hall"])
        sel.pack(side="right")
        FlatButton(sel, "Alle auswählen", lambda: self.select_all(True), "ghost").pack(side="left")
        FlatButton(sel, "Keine", lambda: self.select_all(False), "ghost").pack(side="left", padx=(2, 0))
        self.count_label = tk.Label(sbar, text="", bg=C["hall"], fg=C["amber_ink"], font=F["small"])
        self.count_label.pack(side="right", padx=(0, 14))

        self.search.trace_add("write", lambda *a: self.refresh())
        self.search_entry.bind("<Escape>", lambda e: self.search.set(""))
        self.search_entry.bind("<Return>", lambda e: self._focus_first())
        self.search_entry.bind("<Down>", lambda e: self._focus_first())
        self.bind_all("<Control-f>", lambda e: (self.search_entry.focus_set(), self.search_entry.select_range(0, "end")))

        # ── Tabelle
        frm = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        frm.pack(fill="both", expand=True, padx=14)
        self.tree = ttk.Treeview(frm, columns=[c[0] for c in COLS], show="headings", selectmode="browse")
        for key, title, w in COLS:
            self.tree.heading(key, text=title, anchor="w" if key not in ("value", "weight", "sel") else
                              "e" if key != "sel" else "center")
            self.tree.column(key, width=w, minwidth=30, stretch=key in ("name", "street", "items"),
                             anchor="center" if key in ("sel", "country") else
                             "e" if key in ("value", "weight") else "w")
        ys = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(frm, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)
        self.tree.tag_configure("warn", background=C["paper"])
        self.tree.tag_configure("bad", background=C["red_bg"], foreground=C["red_ink"])
        self.tree.tag_configure("review", background=C["amber_bg"])
        self.tree.tag_configure("done", background=C["green_bg"], foreground=C["green_ink"])
        self.tree.tag_configure("exists", background=C["blue_bg"], foreground=C["blue_ink"])
        self.tree.tag_configure("manual", background=C["violet_bg"], foreground=C["violet_ink"])
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Delete>", lambda e: self.delete_orders())
        self.tree.bind("<Double-1>", lambda e: self.edit_current() if self.tree.identify_region(e.x, e.y) == "cell"
                       and self.tree.identify_column(e.x) != "#1" else None)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())

        # ── Detailbereich: ein Panel, Abschnitte durch Haarlinien getrennt
        det = tk.Frame(self, bg=C["paper"], highlightthickness=1, highlightbackground=C["line"])
        det.pack(fill="x", padx=14, pady=(12, 0))
        head = tk.Frame(det, bg=C["paper"], padx=18, pady=12)
        head.pack(fill="x")
        self.det_title = tk.Label(head, text="", bg=C["paper"], fg=C["ink"], font=F["order"])
        self.det_title.pack(side="left")
        self.det_name = tk.Label(head, text="Kein Auftrag ausgewählt", bg=C["paper"], fg=C["muted"], font=F["title"])
        self.det_name.pack(side="left", padx=(12, 0), pady=(3, 0))
        self.det_badge = tk.Label(head, text="", font=F["badge"], padx=10, pady=2, bg=C["paper"])
        self.det_badge.pack(side="left", padx=(14, 0), pady=(3, 0))
        tk.Frame(det, bg=C["line"], height=1).pack(fill="x")
        cards = tk.Frame(det, bg=C["paper"])
        cards.pack(fill="x")
        self.cards = {}
        col = 0
        for key, title, weight in (("to", "Empfänger", 3), ("ship", "Sendung", 3),
                                   ("items", "Artikel", 4), ("hints", "Hinweise", 5)):
            if col:
                tk.Frame(cards, bg=C["line"], width=1).grid(row=0, column=col, sticky="ns", pady=12)
                col += 1
            self.cards[key] = InfoCard(cards, title)
            self.cards[key].grid(row=0, column=col, sticky="nsew")
            cards.columnconfigure(col, weight=weight, uniform="cards")
            col += 1

        # ── Statuszeile mit kompakter Farblegende
        sb = tk.Frame(self, bg=C["hall"], padx=14, pady=8)
        sb.pack(fill="x")
        self.status = tk.StringVar(value="Keine Datei geladen. Mit „Importieren…“ einen S4T-Export laden.")
        self.share_label = tk.Label(sb, text="", bg=C["hall"], fg=C["muted"], font=F["body_b"])
        self.share_label.pack(side="left", padx=(0, 16))
        tk.Label(sb, textvariable=self.status, bg=C["hall"], fg=C["muted"], font=F["small"]).pack(side="left")
        self.progress = ttk.Progressbar(sb, length=180, mode="determinate", style="Signal.Horizontal.TProgressbar")
        self.progress.pack(side="right")
        legend = tk.Frame(sb, bg=C["hall"])
        legend.pack(side="right", padx=(0, 18))
        for bg, fg, txt in ((C["amber_bg"], C["amber_ink"], "prüfen"), (C["red_bg"], C["red_ink"], "nicht sendbar"),
                            (C["green_bg"], C["green_ink"], "Label erstellt"),
                            (C["blue_bg"], C["blue_ink"], "war schon in Sendcloud"),
                            (C["violet_bg"], C["violet_ink"], "manuell versendet")):
            tk.Label(legend, text=txt, bg=bg, fg=fg, font=F["small"], padx=6).pack(side="left", padx=(0, 4))

    # ----- Tabelle
    def _row_values(self, o):
        return ("☑" if o.selected else "☐", o.ref, o.name, o.company, o.street, o.house, o.zip, o.city,
                o.country, o.items_text, f"{o.value:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                o.weight_text, o.status + (f" ({o.locked_by})" if o.busy_elsewhere else ""), self._label_text(o),
                o.tracking)

    @staticmethod
    def _label_text(o):
        if o.printed:
            return f"🖨 {o.printed}"
        if o.labels:
            return f"✓ {len(o.labels)} bereit" if len(o.labels) > 1 else "✓ bereit"
        return ""

    def _tag(self, o):
        if o.status == "Label erstellt":
            return "done"
        if o.status == "bereits vorhanden":
            return "exists"
        if o.status == "manuell versendet":
            return "manual"
        if o.validate() or o.status in ("Fehler", "teilweise"):
            return "bad"
        if o.needs_review:
            return "review"
        return "warn" if o.hints else ""

    def _visible(self, o):
        f = self.filter.get()
        if f == "Offen" and o.done:
            return False
        if f == "Versendet" and not o.done:
            return False
        if f == "Ausgewählt" and not o.selected:
            return False
        if f == "Bitte prüfen" and not (o.needs_review or (not o.done and any(
                "prüfen" in h or h.startswith(("Achtung", "Radsatz", "Hausnummer fehlt", "E-Mail ungültig", "Land unklar"))
                for h in o.hints))):
            return False
        if f == "Nicht sendbar / Fehler" and not ((o.validate() and not o.done) or o.status in ("Fehler", "teilweise")):
            return False
        if f == "Label ungedruckt" and not (o.labels and not o.printed):
            return False
        if f == "Nicht exportiert" and not (o.tracking and not o.exported):
            return False
        words = [] if getattr(self, "_nosearch", False) else self._search_text().lower().split()
        if words:
            hay = " ".join([o.ref, o.name, o.company, o.street, o.house, o.addr2, o.zip, o.city, o.country,
                            o.email, o.phone, o.status, o.tracking, o.note] +
                           [f'{i["sku"]} {i["name"]} {i["ean"]}' for i in o.items]).lower()
            return all(w in hay for w in words)
        return True

    def refresh(self, keep=None):
        keep = keep or self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for i, o in enumerate(self.orders):
            if self._visible(o):
                self.tree.insert("", "end", iid=str(i), values=self._row_values(o), tags=(self._tag(o),))
                shown += 1
        if keep and self.tree.exists(keep):
            self.tree.focus(keep)
            self.tree.selection_set(keep)
            self.tree.see(keep)
        filtered = shown != len(self.orders)
        self.count_label.configure(text=f"{shown} von {len(self.orders)} angezeigt" if filtered else "")
        current = self.filter.get()
        counts = {}
        for f in FILTERS:  # Zähler je Reiter (ohne Suchbegriff)
            self.filter.set(f)
            self._nosearch = True
            counts[f] = sum(self._visible(o) for o in self.orders)
            self._nosearch = False
        self.filter.set(current)
        self.tabs.update_counts(counts)
        self._show_details()
        self._update_status()

    def _search_text(self):
        ph = getattr(self, "search_ph", None)
        return "" if ph is not None and ph.shown else self.search.get()

    def _focus_first(self):
        kids = self.tree.get_children()
        if kids:
            self.tree.focus_set()
            self.tree.focus(kids[0])
            self.tree.selection_set(kids[0])
        return "break"

    def _update_row(self, i):
        if self.tree.exists(str(i)):
            o = self.orders[i]
            self.tree.item(str(i), values=self._row_values(o), tags=(self._tag(o),))

    def _update_status(self, extra=""):
        if not self.orders:
            return
        sel = sum(o.selected for o in self.orders)
        done = sum(o.done for o in self.orders)
        bad = sum(bool(o.validate()) for o in self.orders if not o.done)
        unprinted = sum(bool(o.labels) and not o.printed for o in self.orders)
        self.status.set(f"{os.path.basename(self.source)} – {len(self.orders)} Aufträge, {sel} ausgewählt, "
                        f"{done} versendet, {bad} nicht sendbar, {unprinted} Labels ungedruckt. {extra}")

    def _click(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            self._toggle(self.tree.identify_row(e.y))
            return "break"

    def _toggle(self, iid):
        if not iid or self.busy:
            return
        o = self.orders[int(iid)]
        if o.busy_elsewhere:
            messagebox.showinfo(APP_NAME, f"Auftrag {o.ref} wird gerade von {o.locked_by} gesendet.")
            return
        if o.done or o.tracking:
            messagebox.showinfo(APP_NAME, f"Auftrag {o.ref} ist bereits erledigt ({o.status}"
                                          + (f", {o.tracking}" if o.tracking else "") + ").")
            return
        o.selected = not o.selected
        self._update_row(int(iid))
        self._update_status()
        self.save_session()

    def select_all(self, on):
        if self.busy:
            return
        visible = [o for o in self.orders if self._visible(o)]  # bei aktiver Suche nur die angezeigten
        for o in visible:
            o.selected = on and not o.done and not o.tracking and not o.validate() and not o.needs_review \
                and not o.busy_elsewhere
        self.refresh()
        self.save_session()
        skipped = [o.ref for o in visible if on and o.needs_review and not o.done]
        if skipped:
            self._update_status(f"Nicht automatisch ausgewählt (Gewicht prüfen): {', '.join(skipped)}")

    def _show_details(self):
        iid = self.tree.focus()
        if not iid or not self.tree.exists(iid):
            self.det_title.configure(text="")
            self.det_name.configure(text="Kein Auftrag ausgewählt – Zeile anklicken für Details")
            self.det_badge.configure(text="", bg=C["paper"])
            for c in self.cards.values():
                c.show([])
            return
        o = self.orders[int(iid)]
        err = o.validate()

        self.det_title.configure(text=o.ref)
        self.det_name.configure(text=o.company or o.name)
        if o.status in ("Label erstellt", "bereits vorhanden"):
            badge = (o.status, C["green_ink"], C["green_bg"])
        elif o.status == "manuell versendet":
            badge = (o.status, C["violet_ink"], C["violet_bg"])
        elif o.status in ("Fehler", "teilweise") or err:
            badge = ("Fehler" if o.status in ("Fehler", "teilweise") else "nicht sendbar", C["red_ink"], C["red_bg"])
        elif o.needs_review:
            badge = ("bitte prüfen", C["amber_ink"], C["amber_bg"])
        else:
            badge = ("bereit zum Senden", C["steel"], C["hall"])
        self.det_badge.configure(text=badge[0], fg=badge[1], bg=badge[2])

        # Empfänger
        to = [(o.name, "b")]
        if o.company:
            to += [("\n" + o.company, "")]
        to += [(f"\n{o.street} {o.house}".rstrip(), "")]
        if o.addr2:
            to += [("\n" + o.addr2, "")]
        to += [(f"\n{o.zip} {o.city}", ""), (f"  ({o.country})", "muted"),
               ("\n\n" + (o.email or "keine E-Mail"), "small" if o.email else "small_muted"),
               ("\n" + (o.phone or "kein Telefon"), "small" if o.phone else "small_muted")]
        self.cards["to"].show(to)

        # Sendung
        option = self.cfg["shipping_option_de"] if o.country == self.cfg["sender_country"] else self.cfg["shipping_option_eu"]
        src = {"Export": "aus S4T-Export", "Historie": "aus früheren Sendungen", "Regel": "Radsatz-Regel",
               "Standard": "Standardgewicht", "manuell": "manuell geändert"}.get(o.weight_source, o.weight_source)
        ship = [("Pakete\t", "muted"), (f"{o.parcels} × {fmt_kg(o.weight)}", "b"),
                (f"\n\t{src}", "warn" if o.needs_review else "muted"),
                ("\nWert\t", "muted"), (fmt_eur(o.value), "b"),
                ("\nVersand\t", "muted"), (SHIPPING_LABELS.get(option, option), ""),
                ("\nStatus\t", "muted"), (o.status, "ok" if o.done else "")]
        for n, t in enumerate(o.tracking.split(", ") if o.tracking else []):
            ship += [("\nSdg.-Nr.\t" if n == 0 else "\n\t", "muted"), (t, "mono")]
        if o.labels or o.done:
            ship += [("\nLabel\t", "muted"),
                     (f"gedruckt {o.printed}" if o.printed else f"{len(o.label_paths)} PDF bereit"
                      if o.label_paths else "keins", "ok" if o.label_paths else "muted")]
        if o.note:
            ship += [("\nNotiz\t", "muted"), (o.note, "")]
        if BACKEND is not None and o.updated_by:
            when = o.updated_at[8:10] + "." + o.updated_at[5:7] + ". " + o.updated_at[11:16] if o.updated_at else ""
            ship += [("\nGeändert\t", "muted"), (f"{o.updated_by}, {when}", "small")]
        self.cards["ship"].show(ship)

        # Artikel
        items = []
        for n, i in enumerate(o.items):
            items += [(("\n\n" if n else "") + f'{i["qty"]:g} × {i["sku"]}', "b"),
                      (f'   {fmt_eur(i["price"])}', "muted"), ("\n" + i["name"], "small")]
        for d in o.discounts:
            items += [(f'\n\n{d["name"] or d["sku"]}   {fmt_eur(d["price"])}', "muted"),
                      ("\nnicht im Paket, vom Warenwert abgezogen", "small_muted")]
        self.cards["items"].show(items)

        # Hinweise: Fehler > Prüfen > automatische Korrekturen
        hints = []
        for e in err:
            hints.append(("✖  " + e, "err"))
        if o.error:
            hints.append(("✖  Sendcloud: " + o.error, "err"))
        if o.local_msg:
            hints.append(("⚠  " + o.local_msg, "warn"))
        if o.needs_review:
            hints.append(("⚠  Gewicht nur geschätzt – Pakete/Gewicht per Doppelklick prüfen. "
                          "Wird von „Alle auswählen“ nicht erfasst.", "warn"))
        def is_warn(h):
            return h.startswith(("Achtung", "Radsatz", "Hausnummer fehlt", "E-Mail ungültig", "Land unklar",
                                 "Label-PDF", "Firmenname")) or "prüfen" in h
        hints += [("⚠  " + h, "warn") for h in o.hints if is_warn(h)]
        hints += [("✓  " + h, "info") for h in o.hints if not is_warn(h)
                  and not (o.needs_review and h.startswith("Gewicht nicht gepflegt"))]
        if not hints:
            hints = [("✓  Keine Auffälligkeiten", "ok")]
        segs = []
        for n, (t, tag) in enumerate(hints):
            segs.append((("\n" if n else "") + t, tag))
        self.cards["hints"].show(segs)

    # ----- Aktionen
    def open_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="S4T-Versandexport öffnen",
                                          filetypes=[("CSV-Dateien", "*.csv"), ("Alle Dateien", "*.*")])
        if not path:
            return
        try:
            new = read_export(path, _num(self.cfg["default_weight_kg"]) or 2.01)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Datei konnte nicht gelesen werden:\n{e}")
            return
        # Früher gelöschte Aufträge nicht ungefragt wieder aufnehmen
        hidden = [o.ref for o in new if o.ref in self.deleted]
        if hidden:
            if messagebox.askyesno(APP_NAME, f"{len(hidden)} Aufträge dieser Datei wurden früher aus der Liste "
                                             "gelöscht:\n" + ", ".join(hidden[:25]) +
                                   "\n\nWieder anzeigen? (Nein = weiterhin ausblenden)"):
                for ref in hidden:
                    self.deleted.pop(ref, None)
                    if BACKEND is not None:
                        BACKEND.unhide(ref)
            else:
                new = [o for o in new if o.ref not in self.deleted]
        if BACKEND is not None:
            return self._import_shared(path, new)
        # Erledigte Aufträge (Label gedruckt + Sendungsnummer exportiert) aus der Liste nehmen
        finished = self._cleanup_finished(refresh=False)
        # Neue Aufträge HINZUFÜGEN: vorhandene behalten ihren Stand, erledigte kommen nicht wieder
        existing = {o.ref for o in self.orders}
        skipped_done = [o.ref for o in new if o.ref in self.completed and o.ref not in existing]
        added = [o for o in new if o.ref not in existing and o.ref not in self.completed]
        already = sum(o.ref in existing for o in new)
        self.orders += added
        self.source = path
        if self.cfg["use_history"] == "1":
            self._apply_history_cached()
        self.refresh()
        self._show_details()
        self.save_session()
        parts = [f"{len(added)} neue Aufträge hinzugefügt"]
        if already:
            parts.append(f"{already} bereits in der Liste")
        if skipped_done:
            parts.append(f"{len(skipped_done)} schon erledigt (übersprungen)")
        if finished:
            parts.append(f"{len(finished)} erledigte entfernt")
        self._update_status(", ".join(parts) + ".")
        messagebox.showinfo(APP_NAME, f"Import aus {os.path.basename(path)}:\n\n• " + "\n• ".join(parts))

    def _cleanup_finished(self, refresh=True):
        """Erledigte Aufträge (versendet, Label gedruckt, Sendungsnummer exportiert) sofort aus der Liste nehmen.
        Sie kommen auch bei späteren Importen nicht wieder."""
        finished = [o for o in self.orders if o.finished]
        if not finished:
            return []
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        removed = []
        for o in finished:
            if BACKEND is not None:
                try:
                    BACKEND.hide(o.ref, "completed")
                except (OSError, Conflict):
                    continue  # bleibt stehen, nächster Versuch beim nächsten Drucken/Export/Import
            self.completed[o.ref] = stamp
            self._synced.pop(o.ref, None)
            append_log(o, "erledigt (gedruckt und exportiert), aus Liste entfernt")
            removed.append(o)
        self.orders = [o for o in self.orders if o not in removed]
        if refresh and removed:
            self.tree.delete(*self.tree.get_children())
            self.refresh()
            self.save_session()
        return removed

    def _import_shared(self, path, new):
        """Import in den gemeinsamen Ordner: nur hinzufügen; Erledigte werden für alle entfernt."""
        self._push()
        res = {"added": 0, "already": 0, "completed": 0, "deleted": 0}
        try:
            finished = self._cleanup_finished(refresh=False)
            if self.cfg["use_history"] == "1":  # Erfahrungswerte vor dem Teilen anwenden
                try:
                    cache = json.load(open(HISTORY_PATH, encoding="utf-8"))
                    apply_history(new, cache.get("data", {}), _num(self.cfg["default_weight_kg"]) or 2.01)
                except Exception:
                    pass
            for o in new:
                res[BACKEND.add(shared_data(o))] += 1
            records, hidden = BACKEND.load_all()
        except (OSError, Conflict) as e:
            messagebox.showerror(APP_NAME, f"Import in den gemeinsamen Ordner fehlgeschlagen:\n{e}")
            return
        self.source = path
        self._sig = None
        self.tree.delete(*self.tree.get_children())
        self._merge_remote(None, records, hidden)
        self.refresh()
        self.save_session()
        parts = [f"{res['added']} neue Aufträge hinzugefügt"]
        if res["already"]:
            parts.append(f"{res['already']} bereits in der Liste")
        if res["completed"]:
            parts.append(f"{res['completed']} schon erledigt (übersprungen)")
        if res["deleted"]:
            parts.append(f"{res['deleted']} früher gelöscht (ausgeblendet)")
        if finished:
            parts.append(f"{len(finished)} erledigte entfernt")
        self._update_status(", ".join(parts) + ".")
        messagebox.showinfo(APP_NAME, f"Import aus {os.path.basename(path)} in die gemeinsame Liste:\n\n• "
                            + "\n• ".join(parts))

    # ----- Erfahrungswerte aus früheren Sendungen
    def _apply_history_cached(self):
        """Cache sofort anwenden; ist er älter als 12 h, im Hintergrund neu laden."""
        cache = {}
        try:
            cache = json.load(open(HISTORY_PATH, encoding="utf-8"))
        except Exception:
            pass
        if cache.get("data"):
            apply_history(self.orders, cache["data"], _num(self.cfg["default_weight_kg"]) or 2.01)
        age = time.time() - cache.get("ts", 0)
        if age > 12 * 3600 and self.cfg.public_key and not self.busy:
            self.reload_history()

    def reload_history(self, notify=False):
        if self.busy:
            return
        self.busy = True
        for b in (self.btn_send, self.btn_open, self.btn_export):
            b.state(["disabled"])
        self.status.set("Lade Erfahrungswerte (Gewicht/Pakete) aus früheren Sendcloud-Sendungen…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)

        def run():
            try:
                data = Sendcloud(self.cfg).history()
                os.makedirs(APP_DIR, exist_ok=True)
                json.dump({"ts": time.time(), "data": data}, open(HISTORY_PATH, "w", encoding="utf-8"))
                self.q.put(("history", (data, notify)))
            except Exception as e:
                self.q.put(("history", (e, notify)))
        threading.Thread(target=run, daemon=True).start()

    def edit_current(self):
        iid = self.tree.focus()
        if not iid or self.busy:
            return
        o = self.orders[int(iid)]
        if o.done or o.tracking or o.busy_elsewhere:
            messagebox.showinfo(APP_NAME, f"Dieser Auftrag wird gerade von {o.locked_by} gesendet." if o.busy_elsewhere
                                else "Dieser Auftrag ist bereits versendet und kann nicht mehr bearbeitet werden.")
            return
        EditDialog(self, o)
        self._after_change(iid)

    def settings(self):
        SettingsDialog(self, self.cfg)

    def send_selected(self):
        if self.busy:
            return
        sel = [o for o in self.orders if o.selected and not o.tracking and not o.busy_elsewhere]
        if not sel:
            messagebox.showinfo(APP_NAME, "Keine Aufträge ausgewählt (Häkchen in der ersten Spalte).")
            return
        bad = [o for o in sel if o.validate()]
        if bad:
            messagebox.showwarning(APP_NAME, "Diese Aufträge sind nicht sendbar und werden übersprungen:\n\n" +
                                   "\n".join(f"{o.ref}: {'; '.join(o.validate())}" for o in bad[:15]))
            sel = [o for o in sel if not o.validate()]
            if not sel:
                return
        if not self.cfg.public_key or not self.cfg.secret_key:
            messagebox.showerror(APP_NAME, "Keine Sendcloud-Zugangsdaten hinterlegt.")
            return
        missing = [label for k, label in REQUIRED_SENDER if not self.cfg[k].strip()]
        if missing:
            messagebox.showerror(APP_NAME, "Absenderdaten fehlen – bitte unter Einstellungen eintragen:\n\n• "
                                 + "\n• ".join(missing))
            self.settings()
            return
        high = [o for o in sel if o.value / o.parcels > 500]
        n_parcels = sum(o.parcels for o in sel)
        msg = (f"{len(sel)} Auftrag/Aufträge mit insgesamt {n_parcels} Paket(en) bei Sendcloud anlegen und bei DHL "
               f"anmelden?\n\nGesamtgewicht {sum(o.weight * o.parcels for o in sel):.2f} kg. "
               f"Die Labels sind kostenpflichtig.").replace(".", ",", 1)
        if high:
            msg += (f"\n\nHinweis: {len(high)} Sendung(en) über 500 € Warenwert (DHL-Standardhaftung): "
                    + ", ".join(o.ref for o in high[:10]))
        if not messagebox.askyesno(APP_NAME, msg):
            return
        self.busy = True
        for b in (self.btn_send, self.btn_open, self.btn_export):
            b.state(["disabled"])
        self.progress.configure(maximum=len(sel), value=0)
        threading.Thread(target=self._worker, args=(sel,), daemon=True).start()

    def _fetch_labels(self, sc, o, parcels):
        """Label-PDFs zu (tracking, parcel_id)-Paaren herunterladen und im Programm ablegen."""
        for tracking, pid in parcels:
            try:
                store_label(o, tracking, sc.label_pdf(pid), self.cfg)
            except Exception as e:
                o.hints.append(f"Label-PDF für {tracking} nicht geladen: {e}")

    def _worker(self, sel):
        sc = Sendcloud(self.cfg)
        for n, o in enumerate(sel, 1):
            i = self.orders.index(o)
            if BACKEND is not None:  # erst im gemeinsamen Ordner reservieren – nur einer kann gewinnen
                try:
                    self._adopt(o, BACKEND.claim(o.ref), keep_lock=True)
                except Conflict as c:
                    if c.current:
                        self._adopt(o, c.current)
                    o.selected, o.local_msg = False, f"Nicht gesendet: {c}"
                    self.q.put(("row", i))
                    self.q.put(("progress", n))
                    continue
                except Gone:
                    o.selected, o.status, o.error = False, "Fehler", "Auftrag wurde von einem Kollegen gelöscht"
                    self.q.put(("row", i))
                    self.q.put(("progress", n))
                    continue
                except OSError as e:
                    o.status, o.error = "Fehler", f"Gemeinsamer Ordner nicht erreichbar – nicht gesendet: {e}"
                    self.q.put(("row", i))
                    self.q.put(("progress", n))
                    continue
            try:
                o.status, o.error = "wird gesendet…", ""
                self.q.put(("row", i))
                ex = sc.existing(o.ref)
                if ex:
                    o.tracking = ", ".join(p["tracking_number"] for p in ex)
                    o.parcel_id = ", ".join(str(p["id"]) for p in ex)
                    o.status = "bereits vorhanden"
                    if len(ex) < o.parcels:
                        o.hints.append(f"Achtung: in Sendcloud nur {len(ex)} von {o.parcels} Paketen vorhanden – "
                                       "fehlende bitte manuell anlegen")
                    append_log(o, "bereits in Sendcloud vorhanden, nicht neu angelegt")
                    self._fetch_labels(sc, o, [(p["tracking_number"], p["id"]) for p in ex])
                else:
                    tracks, ids = [], []
                    try:
                        for k in range(o.parcels):
                            p = sc.announce(o)
                            tracks.append(p["tracking_number"])
                            ids.append(str(p["id"]))
                            o.tracking, o.parcel_id = ", ".join(tracks), ", ".join(ids)
                            self.q.put(("save", None))
                            self._fetch_labels(sc, o, [(p["tracking_number"], p["id"])])
                            time.sleep(0.7)  # Sendcloud-Limit: 100 POST/min
                    finally:
                        o.tracking, o.parcel_id = ", ".join(tracks), ", ".join(ids)
                        if tracks:
                            append_log(o, f"Label erstellt ({len(tracks)} von {o.parcels} Paketen)")
                    o.status = "Label erstellt"
                o.selected = False
            except Exception as e:
                o.status, o.error = "Fehler", str(e)
                if o.tracking:  # Mehrpaket-Sendung nur teilweise angelegt
                    o.status = "teilweise"
                    o.error = f"Nur {len(o.tracking.split(', '))} von {o.parcels} Paketen angelegt: {e}"
            o.locked_by = o.locked_at = ""  # Reservierung freigeben (wird mit dem Ergebnis gespeichert)
            self.q.put(("row", i))
            self.q.put(("progress", n))
        self.q.put(("done", None))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "row":
                    self._update_row(val)
                    if self.tree.focus() == str(val):
                        self._show_details()
                    self.save_session()
                elif kind == "save":
                    self.save_session()
                elif kind == "remote":
                    if self.offline:
                        self.offline = False
                        self._set_share_info()
                    self._merge_remote(*val)
                elif kind == "remote_ok":
                    self._sync_running = False
                    if self.offline:
                        self.offline = False
                        self._set_share_info()
                    self._push()
                elif kind == "remote_err":
                    self._sync_running = False
                    if not self.offline:
                        self.offline = True
                        self._set_share_info()
                elif kind == "printjob":
                    self._print_finished(*val)
                elif kind == "update":
                    self._show_update(*val)
                elif kind == "update_progress":
                    self.update_text.configure(text=f"Update wird geladen … {val:.0%}")
                elif kind == "update_done":
                    self._update_finished(*val)
                elif kind == "history":
                    data, notify = val
                    self.busy = False
                    for b in (self.btn_send, self.btn_open, self.btn_export):
                        b.state(["!disabled"])
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    if isinstance(data, Exception):
                        self._update_status(f"Erfahrungswerte nicht geladen: {data}")
                        if notify:
                            messagebox.showerror(APP_NAME, f"Erfahrungswerte konnten nicht geladen werden:\n{data}")
                    else:
                        n = apply_history(self.orders, data, _num(self.cfg["default_weight_kg"]) or 2.01) if self.cfg["use_history"] == "1" else 0
                        self.save_session()
                        self.refresh()
                        self._show_details()
                        self._update_status(f"Erfahrungswerte: {len(data)} Artikel bekannt, {n} Aufträge angepasst.")
                        if notify:
                            messagebox.showinfo(APP_NAME, f"Erfahrungswerte für {len(data)} Artikel geladen.\n"
                                                          f"{n} Aufträge der aktuellen Liste angepasst.")
                elif kind == "progress":
                    self.progress.configure(value=val)
                    self._update_status()
                elif kind == "done":
                    self.busy = False
                    for b in (self.btn_send, self.btn_open, self.btn_export):
                        b.state(["!disabled"])
                    self.save_session()
                    ok = sum(o.status == "Label erstellt" for o in self.orders)
                    ex = sum(o.status == "bereits vorhanden" for o in self.orders)
                    err = [o for o in self.orders if o.status in ("Fehler", "teilweise")]
                    skipped = [o for o in self.orders if o.local_msg]
                    self._update_status()
                    messagebox.showinfo(APP_NAME, f"Fertig.\n\nLabels erstellt: {ok}\nBereits vorhanden: {ex}\n"
                                                  f"Fehler: {len(err)}" + ("\n\n" + "\n".join(
                                                      f"{o.ref}: {o.error[:150]}" for o in err[:8]) if err else "")
                                        + ("\n\nÜbersprungen (Kollegen):\n" + "\n".join(
                                            f"{o.ref}: {o.local_msg}" for o in skipped[:8]) if skipped else ""))
                    for o in skipped:
                        o.local_msg = ""
        except queue.Empty:
            pass
        self.after(150, self._poll)

    # ----- Updates
    def _update_repo(self):
        return (self.cfg["update_repo"].strip() or UPDATE_REPO).strip("/ ")

    def check_updates(self, manual=False):
        repo = self._update_repo()
        if not repo:
            if manual:
                messagebox.showinfo(APP_NAME, "Es ist kein GitHub-Repository für Updates eingetragen.")
            return

        def run():
            try:
                self.q.put(("update", (check_update(repo), manual)))
            except Exception as e:
                self.q.put(("update", (e, manual)))
        threading.Thread(target=run, daemon=True).start()

    def _update_tick(self):
        self.check_updates()
        self.after(6 * 3600 * 1000, self._update_tick)

    def _show_update(self, info, manual):
        if isinstance(info, Exception):
            if manual:
                messagebox.showerror(APP_NAME, f"Updateprüfung fehlgeschlagen:\n{info}")
            return
        if info is None:
            if manual:
                messagebox.showinfo(APP_NAME, f"Du hast die neueste Version ({APP_VERSION}).")
            return
        self.update_info = info
        self.update_text.configure(text=f"Neue Version {info['version']} verfügbar (installiert: {APP_VERSION})")
        self.update_bar.pack(fill="x", after=self._update_anchor)

    def do_update(self):
        info = getattr(self, "update_info", None)
        if not info:
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "Bitte warten, bis der laufende Vorgang fertig ist.")
            return
        notes = info["notes"][:900] + ("…" if len(info["notes"]) > 900 else "")
        if not messagebox.askyesno(APP_NAME, f"Auf Version {info['version']} aktualisieren?\n\n"
                                             + (f"Neu:\n{notes}\n\n" if notes else "")
                                             + "Das Tool startet danach neu. Deine Arbeitsliste bleibt erhalten."):
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo(APP_NAME, f"Im Entwicklungsmodus bitte manuell aktualisieren:\n{info['page']}")
            return
        self.save_session()
        self.busy = True
        self.update_text.configure(text="Update wird geladen …")
        self.update_btn.pack_forget()
        self.update_later.pack_forget()

        def run():
            try:
                exe = install_update(info, lambda p: self.q.put(("update_progress", p)))
                self.q.put(("update_done", (exe, None)))
            except Exception as e:
                self.q.put(("update_done", (None, e)))
        threading.Thread(target=run, daemon=True).start()

    def _update_finished(self, exe, error):
        self.busy = False
        if error:
            self.update_bar.pack_forget()
            messagebox.showerror(APP_NAME, f"Update fehlgeschlagen:\n{error}\n\nDie bisherige Version läuft weiter. "
                                           f"Manuell herunterladen:\n{self.update_info.get('page', '')}")
            return
        import subprocess
        self.save_session()
        subprocess.Popen([exe], close_fds=True)
        self.destroy()

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                APP_NAME, "Es läuft gerade ein Vorgang (Senden/Laden). Trotzdem beenden?\n"
                          "Bereits erzeugte Sendungsnummern sind gespeichert."):
            return
        self.save_session()
        self.destroy()

    # ----- Arbeitsliste speichern / laden
    def save_session(self):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            tmp = SESSION_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"source": self.source, "saved": dt.datetime.now().isoformat(timespec="seconds"),
                           "orders": [o.to_dict() for o in self.orders], "deleted": self.deleted,
                           "completed": self.completed},
                          f, ensure_ascii=False)
            os.replace(tmp, SESSION_PATH)
        except Exception as e:
            self.status.set(f"Arbeitsliste konnte nicht gespeichert werden: {e}")
        self._push()

    def load_session(self):
        try:
            d = json.load(open(SESSION_PATH, encoding="utf-8"))
        except FileNotFoundError:
            d = None
        except Exception as e:
            messagebox.showwarning(APP_NAME, f"Gespeicherte Arbeitsliste konnte nicht gelesen werden:\n{e}")
            d = None
        if BACKEND is not None:
            self._load_shared(local=d)
            return
        if not d:
            return
        self.orders = [Order.from_dict(x) for x in d.get("orders", [])]
        self.source = d.get("source", "")
        self.deleted = d.get("deleted", {})
        self.completed = d.get("completed", {})
        self.refresh()
        saved = d.get("saved", "")[:16].replace("T", " ")
        self._update_status(f"Arbeitsliste vom {saved} wiederhergestellt.")

    # ----- Gemeinsamer Ordner
    def _init_backend(self):
        global BACKEND, LABEL_DIR
        path = self.cfg["shared_dir"].strip()
        if not path:
            self._set_share_info()
            return
        try:
            BACKEND = FolderBackend(path, USER)
            LABEL_DIR = BACKEND.dirs["labels"]
            shared = BACKEND.get_settings()
            if shared:  # gemeinsame Versandeinstellungen gelten für alle Arbeitsplätze
                for k in SHARED_SETTINGS:
                    if shared.get(k) not in (None, ""):
                        self.cfg[k] = shared[k]
                self.cfg.save()
            elif self.cfg["sender_company"].strip():  # erster Platz: eigene Einstellungen teilen
                BACKEND.put_settings({k: self.cfg[k] for k in SHARED_SETTINGS})
        except OSError as e:
            BACKEND = None
            messagebox.showerror(APP_NAME, f"Der gemeinsame Ordner ist nicht erreichbar:\n{path}\n\n{e}\n\n"
                                           "Das Tool arbeitet in dieser Sitzung nur lokal. Änderungen sehen die "
                                           "Kollegen nicht. Bitte Netzlaufwerk prüfen und das Tool neu starten.")
        self._set_share_info()

    def _set_share_info(self):
        if BACKEND is None:
            text, fg = ("Nur lokal" if not self.cfg["shared_dir"].strip() else "Gemeinsamer Ordner NICHT verbunden – nur lokal"), \
                (C["muted"] if not self.cfg["shared_dir"].strip() else C["red_ink"])
        elif self.offline:
            text, fg = "Gemeinsamer Ordner nicht erreichbar – Änderungen werden nachgereicht", C["red_ink"]
        else:
            text, fg = f"Gemeinsame Liste · angemeldet als {USER}", C["green_ink"]
        self.share_label.configure(text=text, fg=fg)

    def _adopt(self, o, rec, keep_lock=False):
        """Stand aus dem gemeinsamen Ordner übernehmen (eigene Auswahl bleibt erhalten)."""
        tmp = Order.from_dict({**rec["data"], "version": rec["version"], "updated_by": rec.get("updated_by", ""),
                               "updated_at": rec.get("updated_at", "")}, keep_lock=keep_lock)
        for k, v in tmp.__dict__.items():
            if k != "selected":
                setattr(o, k, v)
        self._synced[o.ref] = json.dumps({k: v for k, v in rec["data"].items() if k not in LOCAL_ONLY},
                                         sort_keys=True, ensure_ascii=False)

    def _load_shared(self, local=None):
        try:
            records, hidden = BACKEND.load_all()
        except OSError as e:
            self.offline = True
            self._set_share_info()
            messagebox.showerror(APP_NAME, f"Gemeinsame Liste konnte nicht geladen werden:\n{e}")
            return
        if not records and local and local.get("orders") and messagebox.askyesno(
                APP_NAME, f"Der gemeinsame Ordner ist noch leer.\n\nDeine lokale Arbeitsliste "
                          f"({len(local['orders'])} Aufträge) dorthin übernehmen, damit die Kollegen sie sehen?"):
            for x in local["orders"]:
                o = Order.from_dict(x)
                BACKEND.add(shared_data(o))
                for fn in o.labels:  # vorhandene Label-PDFs mitnehmen
                    src = os.path.join(APP_DIR, "labels", fn)
                    if os.path.exists(src) and not os.path.exists(os.path.join(LABEL_DIR, fn)):
                        with open(src, "rb") as a, open(os.path.join(LABEL_DIR, fn), "wb") as b:
                            b.write(a.read())
            for ref, at in local.get("deleted", {}).items():
                BACKEND.hide(ref, "deleted")
            for ref, at in local.get("completed", {}).items():
                BACKEND.hide(ref, "completed")
            records, hidden = BACKEND.load_all()
        if local:
            self.source = local.get("source", "")
        sel = {x["ref"] for x in (local or {}).get("orders", []) if x.get("selected")}
        self.orders = []
        for rec in records:
            o = Order(rec["ref"])
            self._adopt(o, rec)
            o.selected = o.ref in sel and not o.done
            self.orders.append(o)
        self.deleted, self.completed = hidden.get("deleted", {}), hidden.get("completed", {})
        self._sig = None
        self.refresh()
        self._update_status(f"Gemeinsame Liste geladen: {len(self.orders)} Aufträge.")

    def _push(self):
        """Geänderte Aufträge in den gemeinsamen Ordner schreiben (mit Konflikterkennung)."""
        if BACKEND is None:
            return
        conflicts, gone = [], []
        for o in list(self.orders):
            s = shared_json(o)
            if self._synced.get(o.ref) == s:
                continue
            try:
                rec = BACKEND.put(o.ref, o.version, shared_data(o))
                o.version, o.updated_by, o.updated_at = rec["version"], rec["updated_by"], rec["updated_at"]
                self._synced[o.ref] = s
            except Conflict as c:
                if c.current:
                    self._adopt(o, c.current)
                conflicts.append(f"{o.ref} ({c.current.get('updated_by') if c.current else '?'})")
            except Gone:
                gone.append(o)
            except OSError:
                if not self.offline:
                    self.offline = True
                    self._set_share_info()
                return
        if self.offline:
            self.offline = False
            self._set_share_info()
        if gone:
            self.orders = [o for o in self.orders if o not in gone]
        if conflicts or gone:
            self.tree.delete(*self.tree.get_children())
            self.refresh()
            msg = ""
            if conflicts:
                msg += "Gleichzeitig geändert, Stand des Kollegen übernommen: " + ", ".join(conflicts) + ". "
            if gone:
                msg += f"{len(gone)} Aufträge wurden von Kollegen entfernt."
            self._update_status(msg)

    def _sync_tick(self):
        """Alle 4 s im Hintergrund prüfen, ob Kollegen etwas geändert haben."""
        if BACKEND is not None and not self.busy and not self._sync_running:
            self._sync_running = True

            def run():
                try:
                    sig = BACKEND.signature()
                    if sig != self._sig:
                        self.q.put(("remote", (sig, *BACKEND.load_all())))
                    else:
                        self.q.put(("remote_ok", None))
                except OSError:
                    self.q.put(("remote_err", None))
            threading.Thread(target=run, daemon=True).start()
        self.after(4000, self._sync_tick)

    def _merge_remote(self, sig, records, hidden):
        self._sync_running = False
        if self.busy:
            return  # nach dem Senden erneut abgleichen
        self._push()  # eigene, noch nicht übertragene Änderungen zuerst
        focus = self.tree.focus()
        focus_ref = self.orders[int(focus)].ref if focus and int(focus) < len(self.orders) else None
        local = {o.ref: o for o in self.orders}
        merged, changed = [], False
        for rec in records:
            o = local.get(rec["ref"])
            if o is None:
                o = Order(rec["ref"])
                self._adopt(o, rec)
                changed = True
            elif o.version != rec["version"] and shared_json(o) == self._synced.get(o.ref):
                self._adopt(o, rec)
                changed = True
            merged.append(o)
        if len(merged) != len(self.orders):
            changed = True
        self.orders = merged
        self.deleted, self.completed = hidden.get("deleted", {}), hidden.get("completed", {})
        self._sig = sig
        if changed:
            self.tree.delete(*self.tree.get_children())
            keep = next((str(i) for i, o in enumerate(self.orders) if o.ref == focus_ref), None)
            self.refresh(keep=keep)

    # ----- Manuell als versendet
    def mark_manual(self):
        iid = self.tree.focus()
        if not iid or self.busy:
            return
        o = self.orders[int(iid)]
        if o.busy_elsewhere:
            messagebox.showinfo(APP_NAME, f"Auftrag {o.ref} wird gerade von {o.locked_by} gesendet.")
            return
        if o.status == "manuell versendet":
            if messagebox.askyesno(APP_NAME, f"Markierung „manuell versendet“ für Auftrag {o.ref} zurücknehmen?"):
                append_log(o, "Markierung manuell versendet zurückgenommen")
                o.status, o.tracking, o.note = "neu", "", ""
                self._after_change(iid)
            return
        if o.done:
            messagebox.showinfo(APP_NAME, f"Auftrag {o.ref} ist bereits erledigt ({o.status}).")
            return
        dlg = ManualDialog(self, o)
        if dlg.result is None:
            return
        o.tracking = ", ".join(dlg.result["tracking"])
        o.note = dlg.result["note"]
        o.status, o.selected, o.error = "manuell versendet", False, ""
        append_log(o, "manuell als versendet markiert" + (f" ({o.note})" if o.note else ""))
        self._after_change(iid)

    def reset_printed(self):
        iid = self.tree.focus()
        if iid:
            self.orders[int(iid)].printed = ""
            self._after_change(iid)

    def open_label(self):
        iid = self.tree.focus()
        if not iid:
            return
        paths = self.orders[int(iid)].label_paths
        if not paths:
            messagebox.showinfo(APP_NAME, "Für diesen Auftrag ist kein Label im Programm gespeichert.")
            return
        for p in paths:
            os.startfile(p)

    def _after_change(self, iid):
        self._update_row(int(iid))
        self._show_details()
        self._update_status()
        self.save_session()

    def _context_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.focus(iid)
        self.tree.selection_set(iid)
        o = self.orders[int(iid)]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Bearbeiten…", command=self.edit_current, state="disabled" if o.done else "normal")
        m.add_command(label="Markierung „manuell versendet“ zurücknehmen" if o.status == "manuell versendet"
                      else "Manuell als versendet markieren…", command=self.mark_manual,
                      state="normal" if o.status == "manuell versendet" or not o.done else "disabled")
        m.add_separator()
        m.add_command(label="Label öffnen", command=self.open_label, state="normal" if o.label_paths else "disabled")
        m.add_command(label="Als „nicht gedruckt“ markieren", command=self.reset_printed,
                      state="normal" if o.printed else "disabled")
        m.add_separator()
        m.add_command(label="Aus Liste löschen…", command=lambda: self.delete_orders([o]))
        m.tk_popup(e.x_root, e.y_root)

    # ----- Löschen
    def delete_orders(self, targets=None):
        """Aufträge aus der Arbeitsliste entfernen (nicht in Sendcloud!). Ohne Angabe: angehakte, sonst markierte."""
        if self.busy:
            return
        if targets is None:
            targets = [o for o in self.orders if o.selected]
            if not targets and self.tree.focus():
                targets = [self.orders[int(self.tree.focus())]]
        if not targets:
            messagebox.showinfo(APP_NAME, "Bitte Aufträge anhaken oder eine Zeile markieren.")
            return
        refs = ", ".join(o.ref for o in targets[:25]) + (" …" if len(targets) > 25 else "")
        msg = f"{len(targets)} Auftrag/Aufträge aus der Liste löschen?\n\n{refs}"
        open_tracking = [o.ref for o in targets if o.tracking and not o.exported]
        open_labels = [o.ref for o in targets if o.labels and not o.printed]
        if open_tracking:
            msg += "\n\n⚠ Sendungsnummern noch nicht exportiert: " + ", ".join(open_tracking)
        if open_labels:
            msg += "\n⚠ Labels noch nicht gedruckt: " + ", ".join(open_labels)
        if any(o.status in ("Label erstellt", "bereits vorhanden", "teilweise") for o in targets):
            msg += ("\n\nHinweis: Das Label wird dadurch NICHT in Sendcloud storniert. "
                    "Sendungsnummern bleiben im Protokoll.")
        msg += "\n\nBeim erneuten Laden einer Exportdatei werden gelöschte Aufträge nur auf Nachfrage wieder angezeigt."
        if not (messagebox.askyesno if not (open_tracking or open_labels) else messagebox.askokcancel)(
                APP_NAME, msg, icon="warning"):
            return
        busy = [o.ref for o in targets if o.busy_elsewhere]
        if busy:
            messagebox.showwarning(APP_NAME, "Diese Aufträge werden gerade von Kollegen gesendet und bleiben "
                                             "stehen: " + ", ".join(busy))
            targets = [o for o in targets if not o.busy_elsewhere]
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        for o in targets:
            if BACKEND is not None:
                try:
                    BACKEND.hide(o.ref, "deleted")
                except (OSError, Conflict) as e:
                    messagebox.showerror(APP_NAME, f"Auftrag {o.ref} konnte im gemeinsamen Ordner nicht "
                                                   f"gelöscht werden:\n{e}")
                    return
            self.deleted[o.ref] = stamp
            self._synced.pop(o.ref, None)
            append_log(o, "aus Liste gelöscht")
        ids = {id(o) for o in targets}
        self.orders = [o for o in self.orders if id(o) not in ids]
        self.tree.delete(*self.tree.get_children())  # alte Zeilen-IDs verwerfen, damit kein falscher Fokus bleibt
        self.refresh()
        self._show_details()
        self.save_session()
        self._update_status(f"{len(targets)} Auftrag/Aufträge gelöscht.")

    # ----- Sammeldruck
    def print_labels(self):
        if self.busy:
            return
        with_label = [o for o in self.orders if o.labels or (o.parcel_id and o.status in ("Label erstellt",
                                                                                         "bereits vorhanden"))]
        if not with_label:
            messagebox.showinfo(APP_NAME, "Es sind noch keine Labels vorhanden.")
            return
        dlg = PrintDialog(self, with_label, self.cfg["label_paper"])
        if not dlg.result:
            return
        orders, paper = dlg.result
        self.cfg["label_paper"] = paper
        self.cfg.save()
        self.busy = True
        self.status.set("Bereite Sammeldruck vor…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)

        def run():
            try:
                sc = Sendcloud(self.cfg)
                target = os.path.join(PRINT_DIR, f"sammeldruck_{dt.datetime.now():%Y%m%d_%H%M%S}.pdf")
                if paper == "A6":
                    parts = []
                    for o in orders:  # fehlende Labels nachladen
                        if not o.label_paths and o.parcel_id:
                            self._fetch_labels(sc, o, zip(o.tracking.split(", "), o.parcel_id.split(", ")))
                        parts += o.label_paths
                    if not parts:
                        raise RuntimeError("Keine Label-PDFs gefunden.")
                    parts = [to_a6(open(p, "rb").read()) for p in parts]  # auch ältere, nicht umgerechnete Labels
                else:
                    ids = [pid for o in orders for pid in o.parcel_id.split(", ") if pid.strip().isdigit()]
                    if not ids:
                        raise RuntimeError("Für A4 werden Sendcloud-Paket-IDs benötigt (keine vorhanden).")
                    parts = [sc.labels_bulk(ids[k:k + 20], "A4") for k in range(0, len(ids), 20)]
                pages = merge_pdfs(parts, target)
                self.q.put(("printjob", (orders, target, pages, None)))
            except Exception as e:
                self.q.put(("printjob", (orders, None, 0, e)))
        threading.Thread(target=run, daemon=True).start()

    def _print_finished(self, orders, target, pages, error):
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if error:
            messagebox.showerror(APP_NAME, f"Sammeldruck fehlgeschlagen:\n{error}")
            self._update_status()
            return
        printed = False
        if self.cfg["print_direct"] == "1":
            try:
                os.startfile(target, "print")
                printed = True
            except OSError:
                pass
        if not printed:
            os.startfile(target)
        stamp = dt.datetime.now().strftime("%d.%m. %H:%M")
        for o in orders:
            o.printed = stamp
        self.refresh()
        self._show_details()
        self.save_session()
        removed = self._cleanup_finished()
        self._update_status(f"Sammeldruck: {len(orders)} Aufträge, {pages} Seite(n) – "
                            + ("an Standarddrucker gesendet." if printed else "PDF geöffnet, bitte drucken (Strg+P).")
                            + (f" {len(removed)} erledigte Aufträge entfernt." if removed else ""))

    def export(self):
        rows = [o for o in self.orders if o.tracking and not o.exported]  # nur neue Sendungsnummern
        no_tracking = [o.ref for o in self.orders if o.status == "manuell versendet" and not o.tracking]
        if not rows:
            again = [o for o in self.orders if o.tracking]
            if not again:
                messagebox.showinfo(APP_NAME, "Noch keine Sendungsnummern vorhanden.")
                return
            if not messagebox.askyesno(APP_NAME, "Es gibt keine neuen Sendungsnummern – alle wurden schon "
                                                 f"exportiert.\n\nDie {len(again)} bereits exportierten erneut "
                                                 "exportieren?", default="no"):
                return
            rows = again
        name = f"sendungsnummern_{dt.datetime.now():%Y%m%d_%H%M}.csv"
        path = filedialog.asksaveasfilename(title="Sendungsnummern exportieren", initialfile=name,
                                            initialdir=os.path.dirname(self.source) or None, defaultextension=".csv",
                                            filetypes=[("CSV (Semikolon)", "*.csv")])
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";", lineterminator="\r\n")
            w.writerow(["Auftragsnummer", "Sendungsnummer"])
            for o in rows:
                for tn in o.tracking.split(", "):
                    w.writerow([o.ref, tn])
                o.exported = True
        self.save_session()
        removed = self._cleanup_finished()
        waiting = [o.ref for o in rows if o in self.orders and o.labels and not o.printed]
        msg = f"{len(rows)} Aufträge exportiert:\n{path}"
        if removed:
            msg += f"\n\n{len(removed)} erledigte Aufträge (gedruckt und exportiert) aus der Liste entfernt."
        if waiting:
            msg += f"\n\nNoch nicht gedruckt, bleiben in der Liste: {', '.join(waiting[:15])}"
        if no_tracking:
            msg += ("\n\nManuell versendet ohne Sendungsnummer (nicht in der Datei): " + ", ".join(no_tracking))
        messagebox.showinfo(APP_NAME, msg)


SHIPPING_LABELS = {"dhl_de:dhl_paket": "DHL Paket", "dhl_de:weltpaket": "DHL Weltpaket",
                   "dhl_de:warenpost": "DHL Warenpost"}

# ─────────────────────────────────────────────────────────────── Gestaltung
# Teilelager: Graphit-Stahl für die Kopfleiste, kühles Hallengrau als Fläche,
# Signalgelb (Bodenmarkierung) als einziger kräftiger Akzent – nur für „Senden“ und den aktiven Filter.
C = {
    "steel": "#22303C", "steel_hover": "#2F404F", "steel_line": "#3A4B5B", "steel_text": "#D5DEE6",
    "hall": "#EEF1F4", "paper": "#FFFFFF", "ink": "#18212B", "muted": "#637282", "faint": "#94A1AE",
    "line": "#DCE2E8", "line_strong": "#C3CCD5", "hover": "#E2E7EC",
    "signal": "#F5B700", "signal_hover": "#FFC61F",
    "amber_bg": "#FFF1CC", "amber_ink": "#8A5300",
    "red_bg": "#FDE7E5", "red_ink": "#A3261B",
    "green_bg": "#E3F2E8", "green_ink": "#22663F",
    "blue_bg": "#E4EDF7", "blue_ink": "#24538A",
    "violet_bg": "#EEE8F8", "violet_ink": "#5B3FA8",
}
_BODY, _DIN, _MONO = "Segoe UI Variable Text", "Bahnschrift", "Cascadia Mono"
F = {
    "brand": ("Bahnschrift SemiBold", 14), "order": ("Bahnschrift SemiBold", 20), "title": (_DIN, 14),
    "tab": (_DIN, 11), "tab_count": ("Bahnschrift Light", 11), "section": ("Bahnschrift SemiBold", 11),
    "body": (_BODY, 10), "body_l": (_BODY, 11), "body_b": ("Segoe UI Variable Text Semibold", 10),
    "small": (_BODY, 9), "badge": ("Bahnschrift SemiBold", 10), "button": ("Segoe UI Variable Text Semibold", 10),
    "mono": (_MONO, 10), "head": ("Segoe UI Variable Text Semibold", 9),
}


def apply_theme(root):
    import tkinter.font as tkfont
    fams = set(tkfont.families(root))
    for k, (fam, *rest) in list(F.items()):  # Fallback, falls eine Schrift fehlt (ältere Windows-Versionen)
        if fam not in fams:
            F[k] = ("Segoe UI", *rest, *(["bold"] if "Semi" in fam else []))
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=C["hall"], foreground=C["ink"], font=F["body"], bordercolor=C["line_strong"],
                focuscolor=C["steel"], troughcolor=C["hall"])
    s.configure("TFrame", background=C["hall"])
    s.configure("TLabel", background=C["hall"], foreground=C["ink"])
    s.configure("TLabelframe", background=C["hall"])
    s.configure("TCheckbutton", background=C["hall"])
    s.configure("TRadiobutton", background=C["hall"])
    s.configure("TEntry", fieldbackground=C["paper"], bordercolor=C["line_strong"], lightcolor=C["paper"],
                padding=4)
    s.map("TEntry", bordercolor=[("focus", C["steel"])], lightcolor=[("focus", C["steel"])])
    s.configure("TButton", background=C["paper"], foreground=C["ink"], bordercolor=C["line_strong"],
                lightcolor=C["paper"], darkcolor=C["paper"], padding=(12, 5), font=F["button"])
    s.map("TButton", background=[("active", C["hover"]), ("pressed", C["line"])])
    s.configure("Treeview", background=C["paper"], fieldbackground=C["paper"], foreground=C["ink"],
                rowheight=30, borderwidth=0, font=F["body"])
    s.map("Treeview", background=[("selected", C["steel"])], foreground=[("selected", "white")])
    s.configure("Treeview.Heading", background=C["hall"], foreground=C["muted"], font=F["head"],
                relief="flat", borderwidth=0, padding=(6, 7))
    s.map("Treeview.Heading", background=[("active", C["hover"])])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    for o in ("Vertical", "Horizontal"):
        s.configure(f"{o}.TScrollbar", background=C["line"], troughcolor=C["paper"], bordercolor=C["paper"],
                    arrowcolor=C["muted"], lightcolor=C["line"], darkcolor=C["line"], gripcount=0)
        s.map(f"{o}.TScrollbar", background=[("active", C["line_strong"])])
    s.configure("Signal.Horizontal.TProgressbar", background=C["signal"], troughcolor=C["line"],
                bordercolor=C["hall"], lightcolor=C["signal"], darkcolor=C["signal"], thickness=6)
    root.option_add("*Toplevel.background", C["hall"])
    root.option_add("*Menu.background", C["paper"])
    root.option_add("*Menu.activeBackground", C["steel"])
    root.option_add("*Menu.activeForeground", "white")
    root.option_add("*Menu.font", F["body"])
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Text.font", F["body"])


class FlatButton(tk.Label):
    """Flacher Button: 'bar' (auf Graphit), 'primary' (Signalgelb), 'ghost' (auf Hallengrau)."""
    VARIANTS = {
        "bar": dict(bg=C["steel"], fg=C["steel_text"], hover=C["steel_hover"], hfg="white", font="button"),
        "primary": dict(bg=C["signal"], fg=C["ink"], hover=C["signal_hover"], hfg=C["ink"], font="button"),
        "ghost": dict(bg=C["hall"], fg=C["ink"], hover=C["hover"], hfg=C["ink"], font="body"),
    }

    def __init__(self, master, text, command, variant="ghost"):
        v = self.VARIANTS[variant]
        super().__init__(master, text=text, bg=v["bg"], fg=v["fg"], font=F[v["font"]], cursor="hand2",
                         padx=16 if variant == "primary" else 11, pady=6)
        self.v, self.command, self._disabled = v, command, False
        self.bind("<Enter>", lambda e: self._disabled or self.configure(bg=v["hover"], fg=v["hfg"]))
        self.bind("<Leave>", lambda e: self._disabled or self.configure(bg=v["bg"], fg=v["fg"]))
        self.bind("<Button-1>", lambda e: self._disabled or self.command())

    def state(self, flags):
        self._disabled = "disabled" in flags
        self.configure(fg=C["faint"] if self._disabled else self.v["fg"], bg=self.v["bg"],
                       cursor="arrow" if self._disabled else "hand2")


class FilterTabs(tk.Frame):
    """Reiter mit Live-Zähler; der aktive trägt eine Signalgelb-Unterstreichung."""

    def __init__(self, master, var, on_change):
        super().__init__(master, bg=C["hall"])
        self.var, self.on_change, self.tabs = var, on_change, {}
        for f in FILTERS:
            cell = tk.Frame(self, bg=C["hall"], cursor="hand2")
            cell.pack(side="left", padx=(0, 4))
            row = tk.Frame(cell, bg=C["hall"])
            row.pack(padx=8, pady=(4, 2))
            name = tk.Label(row, text=FILTER_TABS[f], bg=C["hall"], font=F["tab"])
            name.pack(side="left")
            cnt = tk.Label(row, text="", bg=C["hall"], font=F["tab_count"])
            cnt.pack(side="left", padx=(5, 0))
            bar = tk.Frame(cell, height=3, bg=C["hall"])
            bar.pack(fill="x")
            for w in (cell, row, name, cnt):
                w.bind("<Button-1>", lambda e, f=f: self.select(f))
                w.bind("<Enter>", lambda e, f=f: self._hover(f, True))
                w.bind("<Leave>", lambda e, f=f: self._hover(f, False))
            self.tabs[f] = (cell, row, name, cnt, bar)
        self._paint()

    def select(self, f):
        self.var.set(f)
        self._paint()
        self.on_change()

    def _hover(self, f, on):
        if f != self.var.get():
            for w in self.tabs[f][:4]:
                w.configure(bg=C["hover"] if on else C["hall"])

    def _paint(self):
        for f, (cell, row, name, cnt, bar) in self.tabs.items():
            active = f == self.var.get()
            for w in (cell, row, name, cnt):
                w.configure(bg=C["hall"])
            name.configure(fg=C["ink"] if active else C["muted"],
                           font=("Bahnschrift SemiBold", F["tab"][1]) if active else F["tab"])
            cnt.configure(fg=C["ink"] if active else C["faint"])
            bar.configure(bg=C["signal"] if active else C["hall"])

    def update_counts(self, counts):
        for f, n in counts.items():
            self.tabs[f][3].configure(text=str(n))
            # leere Problem-Reiter abdunkeln, damit die gefüllten auffallen
            if f in ("Bitte prüfen", "Nicht sendbar / Fehler") and f != self.var.get():
                self.tabs[f][2].configure(fg=C["amber_ink"] if n and f == "Bitte prüfen" else
                                          C["red_ink"] if n else C["faint"])


class Placeholder:
    """Grauer Hinweistext in einem leeren tk.Entry."""

    def __init__(self, entry, text):
        self.e, self.text, self.var = entry, text, entry.cget("textvariable")
        entry.bind("<FocusIn>", self._in, add="+")
        entry.bind("<FocusOut>", self._out, add="+")
        self.shown = False
        self._out()

    def _in(self, _=None):
        if self.shown:
            self.e.delete(0, "end")
            self.e.configure(fg=C["ink"])
            self.shown = False

    def _out(self, _=None):
        if not self.e.get():
            self.shown = True
            self.e.insert(0, self.text)
            self.e.configure(fg=C["faint"])


def fmt_eur(v):
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_kg(v):
    return f"{v:.2f} kg".replace(".", ",")


class InfoCard(tk.Frame):
    """Abschnitt im Detail-Panel: Überschrift in DIN, darunter formatierter Text."""

    def __init__(self, master, title):
        super().__init__(master, bg=C["paper"])
        tk.Label(self, text=title, bg=C["paper"], fg=C["ink"], font=F["section"],
                 anchor="w").pack(fill="x", padx=18, pady=(12, 4))
        self.text = tk.Text(self, height=8, wrap="word", relief="flat", bd=0, bg=C["paper"], fg=C["ink"],
                            font=F["body"], padx=18, pady=0, cursor="arrow", spacing1=2, spacing3=2,
                            tabs=("72p",), highlightthickness=0)
        self.text.pack(fill="both", expand=True, pady=(0, 12))
        t = self.text
        t.tag_configure("b", font=F["body_b"])
        t.tag_configure("muted", foreground=C["muted"])
        t.tag_configure("small", font=F["small"], foreground=C["muted"])
        t.tag_configure("small_muted", font=F["small"], foreground=C["faint"])
        t.tag_configure("mono", font=F["mono"], foreground=C["green_ink"])
        t.tag_configure("ok", foreground=C["green_ink"])
        t.tag_configure("err", foreground=C["red_ink"], font=F["body_b"], lmargin2=22)
        t.tag_configure("warn", foreground=C["amber_ink"], lmargin2=22)
        t.tag_configure("info", foreground=C["muted"], lmargin2=22)
        t.configure(state="disabled")

    def show(self, segments):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for text, tag in segments:
            t.insert("end", text, (tag,) if tag else ())
        t.configure(state="disabled")


class ManualDialog(tk.Toplevel):
    """Auftrag ohne Sendcloud als versendet markieren (z. B. Abholung, anderer Versandweg)."""

    def __init__(self, master, order: Order):
        super().__init__(master)
        self.result = None
        self.title(f"Auftrag {order.ref} manuell als versendet markieren")
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both")
        ttk.Label(f, text=f"Auftrag {order.ref}: {order.company or order.name}", font=F["section"]).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Sendungsnummer(n) – eine pro Zeile, leer lassen wenn keine:").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.tn = tk.Text(f, width=46, height=4, font=F["mono"], relief="flat", highlightthickness=1,
                          highlightbackground=C["line_strong"], highlightcolor=C["steel"])
        self.tn.grid(row=2, column=0, columnspan=2, sticky="we")
        self.tn.focus_set()
        ttk.Label(f, text="Notiz (optional, z. B. „Abholung“, „GLS manuell“):").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.note = tk.StringVar()
        ttk.Entry(f, textvariable=self.note, width=50).grid(row=4, column=0, columnspan=2, sticky="we")
        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Als versendet markieren", command=self.ok).pack(side="left")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=(6, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def ok(self):
        nums = [t.strip() for t in re.split(r"[\s,;]+", self.tn.get("1.0", "end")) if t.strip()]
        bad = [t for t in nums if not re.fullmatch(r"[A-Za-z0-9-]{6,40}", t)]
        if bad:
            messagebox.showerror(APP_NAME, "Ungültige Sendungsnummer: " + ", ".join(bad), parent=self)
            return
        if not nums and not messagebox.askyesno(APP_NAME, "Ohne Sendungsnummer markieren? Der Auftrag erscheint "
                                                          "dann nicht im Export.", parent=self):
            return
        self.result = {"tracking": nums, "note": self.note.get().strip()}
        self.destroy()


class PrintDialog(tk.Toplevel):
    def __init__(self, master, orders, paper):
        super().__init__(master)
        self.result = None
        self.orders = orders
        self.title("Labels drucken")
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both")
        unprinted = [o for o in orders if not o.printed]
        checked = [o for o in orders if o.ref in {x.ref for x in master.orders if x.selected}]
        focus = master.tree.focus()
        current = [master.orders[int(focus)]] if focus and master.orders[int(focus)] in orders else []
        self.choices = [("Alle noch nicht gedruckten", unprinted), ("Erneut drucken: alle Labels", orders)]
        if current:
            self.choices.insert(1, (f"Nur markierte Zeile ({current[0].ref})", current))
        if checked:
            self.choices.insert(1, ("Nur angehakte Aufträge", checked))
        ttk.Label(f, text="Welche Labels?", font=F["section"]).grid(row=0, column=0, sticky="w")
        # Nie automatisch „erneut drucken“ vorauswählen – sonst laufen alte Labels mit durch
        self.choice = tk.IntVar(value=0 if unprinted else -1)
        if not unprinted:
            ttk.Label(f, text="Alle Labels sind bereits gedruckt. Für einen Nachdruck bitte bewusst auswählen.",
                      foreground=C["amber_ink"]).grid(row=len(self.choices) + 1, column=0, sticky="w", pady=(6, 0))
        for n, (label, lst) in enumerate(self.choices):
            parcels = sum(max(len(o.labels), len(o.parcel_id.split(", ")) if o.parcel_id else 0) for o in lst)
            ttk.Radiobutton(f, text=f"{label}:  {len(lst)} Aufträge, {parcels} Labels", value=n,
                            variable=self.choice, state="normal" if lst else "disabled").grid(
                row=n + 1, column=0, sticky="w", pady=1)
        r = len(self.choices) + 2
        ttk.Label(f, text="Format", font=F["section"]).grid(row=r, column=0, sticky="w", pady=(12, 0))
        self.paper = tk.StringVar(value=paper)
        ttk.Radiobutton(f, text="A6 – Etikettendrucker (ein Label pro Seite)", value="A6",
                        variable=self.paper).grid(row=r + 1, column=0, sticky="w")
        ttk.Radiobutton(f, text="A4 – Bürodrucker (mehrere Labels pro Seite, lädt von Sendcloud)", value="A4",
                        variable=self.paper).grid(row=r + 2, column=0, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=r + 3, column=0, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Sammel-PDF erstellen", command=self.ok).pack(side="left")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=(6, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def ok(self):
        if self.choice.get() < 0:
            messagebox.showinfo(APP_NAME, "Bitte auswählen, welche Labels gedruckt werden sollen.", parent=self)
            return
        lst = self.choices[self.choice.get()][1]
        if not lst:
            return
        self.result = (lst, self.paper.get())
        self.destroy()


class EditDialog(tk.Toplevel):
    FIELDS = [("name", "Name"), ("company", "Firma"), ("street", "Straße"), ("house", "Hausnummer"),
              ("addr2", "Adresszusatz"), ("zip", "PLZ"), ("city", "Ort"), ("country", "Land (ISO)"),
              ("email", "E-Mail"), ("phone", "Telefon"), ("parcels", "Anzahl Pakete"),
              ("weight", "Gewicht je Paket (kg)")]

    def __init__(self, master, order: Order):
        super().__init__(master)
        self.o = order
        self.title(f"Auftrag {order.ref} bearbeiten")
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both")
        self.vars = {}
        for r, (k, label) in enumerate(self.FIELDS):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            v = getattr(order, k)
            self.vars[k] = tk.StringVar(value=(f"{v:.2f}".replace(".", ",") if k == "weight" else str(v)))
            e = ttk.Entry(f, textvariable=self.vars[k], width=45)
            e.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=2)
            if r == 0:
                e.focus_set()
        b = ttk.Frame(f)
        b.grid(row=len(self.FIELDS), column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Übernehmen", command=self.ok).pack(side="left")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=(6, 0))
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def ok(self):
        w = _num(self.vars["weight"].get())
        if w is None or not (0 < w <= 31.5):
            messagebox.showerror(APP_NAME, "Gewicht muss zwischen 0 und 31,5 kg liegen.", parent=self)
            return
        n = self.vars["parcels"].get().strip()
        if not n.isdigit() or not (1 <= int(n) <= 20):
            messagebox.showerror(APP_NAME, "Anzahl Pakete muss zwischen 1 und 20 liegen.", parent=self)
            return
        em = self.vars["email"].get().strip()
        if em and not EMAIL_RX.match(em):
            messagebox.showerror(APP_NAME, "E-Mail-Adresse ist ungültig.", parent=self)
            return
        for k, _ in self.FIELDS:
            if k == "parcels":
                continue
            if k == "weight":
                if abs(w - self.o.weight) > 1e-9 or int(n) != self.o.parcels:
                    self.o.weight, self.o.parcels, self.o.weight_estimated = w, int(n), False
                    self.o.weight_source = "manuell"
                    self.o.hints = [h for h in self.o.hints if not h.startswith("Gewicht nicht gepflegt")
                                    and not h.startswith("Aus früheren")]
            else:
                val = self.vars[k].get().strip()
                setattr(self.o, k, val.upper() if k == "country" else val)
        self.o.hints = [h for h in self.o.hints if h != "Hausnummer fehlt" or not self.o.house]
        self.o.error = ""
        if self.o.status == "Fehler":
            self.o.status = "neu"
        self.destroy()


class SettingsDialog(tk.Toplevel):
    FIELDS = [("default_weight_kg", "Standardgewicht (kg)"), ("shipping_option_de", "Versandart Inland"),
              ("shipping_option_eu", "Versandart EU-Ausland"), ("contract_id", "DHL-Vertrags-ID (Sendcloud)"),
              ("brand_id", "Brand-ID (Sendcloud)"), ("sender_name", "Absender Name"),
              ("sender_company", "Absender Firma"), ("sender_street", "Absender Straße"),
              ("sender_house_number", "Absender Hausnr."), ("sender_postal_code", "Absender PLZ"),
              ("sender_city", "Absender Ort"), ("sender_country", "Absender Land"),
              ("sender_email", "Absender E-Mail"), ("sender_phone", "Absender Telefon")]

    def __init__(self, master, cfg: Config):
        super().__init__(master)
        self.cfg = cfg
        self.shared_changed = False
        self.title("Einstellungen")
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both")
        ttk.Label(f, text="Sendcloud API", font=F["section"]).grid(row=0, column=0, sticky="w")
        self.pub = tk.StringVar(value=cfg.public_key)
        self.sec = tk.StringVar(value=cfg.secret_key)
        ttk.Label(f, text="Public Key").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(f, textvariable=self.pub, width=50).grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Label(f, text="Secret Key").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(f, textvariable=self.sec, width=50, show="•").grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Button(f, text="Verbindung testen", command=self.test).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(2, 8))
        self.vars = {}
        for r, (k, label) in enumerate(self.FIELDS, 4):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            self.vars[k] = tk.StringVar(value=cfg[k])
            ttk.Entry(f, textvariable=self.vars[k], width=50).grid(row=r, column=1, sticky="w", padx=(8, 0))
        r = len(self.FIELDS) + 4
        self.use_hist = tk.BooleanVar(value=cfg["use_history"] == "1")
        ttk.Checkbutton(f, text="Fehlende Gewichte/Paketanzahl aus früheren Sendungen übernehmen",
                        variable=self.use_hist).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(f, text="Erfahrungswerte jetzt neu laden", command=self.reload_hist).grid(
            row=r + 1, column=1, sticky="w", padx=(8, 0), pady=(2, 0))
        r += 2
        self.print_direct = tk.BooleanVar(value=cfg["print_direct"] == "1")
        ttk.Checkbutton(f, text="Sammeldruck direkt an Standarddrucker senden (sonst PDF öffnen)",
                        variable=self.print_direct).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        r += 1
        ttk.Label(f, text="Gemeinsamer Ordner", font=F["section"]).grid(row=r, column=0, sticky="w", pady=(14, 0))
        ttk.Label(f, text="Für mehrere Arbeitsplätze: Netzwerkordner, auf den alle schreiben dürfen "
                          "(z. B. \\\\server\\freigabe\\Versand). Leer = nur lokal.", foreground=C["muted"],
                  wraplength=520).grid(row=r + 1, column=0, columnspan=2, sticky="w")
        self.shared_dir = tk.StringVar(value=cfg["shared_dir"])
        sf = ttk.Frame(f)
        sf.grid(row=r + 2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Entry(sf, textvariable=self.shared_dir, width=62).pack(side="left")
        ttk.Button(sf, text="…", width=3, command=self._pick_shared).pack(side="left", padx=(4, 0))
        r += 3
        ttk.Label(f, text=f"Updates (installiert: Version {APP_VERSION})", font=F["section"]).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.update_repo = tk.StringVar(value=cfg["update_repo"] or UPDATE_REPO)
        uf = ttk.Frame(f)
        uf.grid(row=r + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(uf, text="GitHub-Repository (besitzer/repo):").pack(side="left")
        ttk.Entry(uf, textvariable=self.update_repo, width=30).pack(side="left", padx=(6, 6))
        ttk.Button(uf, text="Jetzt nach Updates suchen", command=self._check_now).pack(side="left")
        r += 2
        self.save_pdf = tk.BooleanVar(value=cfg["save_label_pdf"] == "1")
        ttk.Checkbutton(f, text="Label-PDFs zusätzlich kopieren in:", variable=self.save_pdf).grid(
            row=r, column=0, sticky="w", pady=(8, 2))
        self.label_dir = tk.StringVar(value=cfg["label_dir"])
        lf = ttk.Frame(f)
        lf.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=(8, 2))
        ttk.Entry(lf, textvariable=self.label_dir, width=41).pack(side="left")
        ttk.Button(lf, text="…", width=3, command=lambda: self.label_dir.set(
            filedialog.askdirectory(parent=self) or self.label_dir.get())).pack(side="left", padx=(4, 0))
        ttk.Label(f, text=f"Einstellungen & Protokoll: {APP_DIR}", foreground=C["muted"]).grid(
            row=r + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=r + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Protokoll öffnen", command=self.open_log).pack(side="left", padx=(0, 16))
        ttk.Button(b, text="Speichern", command=self.ok).pack(side="left")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=(6, 0))
        self.grab_set()
        self.wait_window()

    def _apply(self):
        self.cfg.set_keys(self.pub.get(), self.sec.get())
        for k, v in self.vars.items():
            self.cfg[k] = v.get().strip()
        self.cfg["save_label_pdf"] = "1" if self.save_pdf.get() else "0"
        self.cfg["print_direct"] = "1" if self.print_direct.get() else "0"
        self.cfg["use_history"] = "1" if self.use_hist.get() else "0"
        self.cfg["label_dir"] = self.label_dir.get().strip()
        new_shared = self.shared_dir.get().strip()
        if new_shared != self.cfg["shared_dir"].strip():
            self.cfg["shared_dir"] = new_shared
            self.shared_changed = True
        repo = self.update_repo.get().strip().strip("/")
        repo = re.sub(r"^https?://github\.com/", "", repo)  # auch komplette GitHub-Adresse akzeptieren
        self.cfg["update_repo"] = "" if repo == UPDATE_REPO else repo

    def _check_now(self):
        self._apply()
        self.cfg.save()
        self.master.check_updates(manual=True)

    def reload_hist(self):
        self._apply()
        self.cfg.save()
        self.destroy()
        self.master.reload_history(notify=True)

    def test(self):
        self._apply()
        try:
            Sendcloud(self.cfg).test()
            messagebox.showinfo(APP_NAME, "Verbindung zu Sendcloud erfolgreich.", parent=self)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)

    def open_log(self):
        if BACKEND is not None:
            os.startfile(BACKEND.dirs["protokoll"])
        elif os.path.exists(LOG_PATH):
            os.startfile(LOG_PATH)
        else:
            messagebox.showinfo(APP_NAME, "Noch kein Protokoll vorhanden.", parent=self)

    def ok(self):
        if _num(self.vars["default_weight_kg"].get()) is None:
            messagebox.showerror(APP_NAME, "Standardgewicht ist keine Zahl.", parent=self)
            return
        for k in ("contract_id", "brand_id"):
            if self.vars[k].get().strip() and not self.vars[k].get().strip().isdigit():
                messagebox.showerror(APP_NAME, f"{k} muss eine Zahl sein.", parent=self)
                return
        new_shared = self.shared_dir.get().strip()
        if new_shared and new_shared != self.cfg["shared_dir"].strip():
            try:  # Schreibrecht prüfen, bevor der Ordner übernommen wird
                os.makedirs(new_shared, exist_ok=True)
                probe = os.path.join(new_shared, f".schreibtest_{USER}.tmp")
                with open(probe, "w") as fh:
                    fh.write("ok")
                os.remove(probe)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"In diesen Ordner kann nicht geschrieben werden:\n{new_shared}\n\n{e}",
                                     parent=self)
                return
        self._apply()
        self.cfg.save()
        if BACKEND is not None and not self.shared_changed:
            try:
                BACKEND.put_settings({k: self.cfg[k] for k in SHARED_SETTINGS})
            except OSError as e:
                messagebox.showwarning(APP_NAME, f"Einstellungen lokal gespeichert, aber nicht im gemeinsamen "
                                                 f"Ordner:\n{e}", parent=self)
        self.destroy()
        if self.shared_changed:
            messagebox.showinfo(APP_NAME, "Der gemeinsame Ordner wurde geändert.\n\nBitte das Tool jetzt schließen "
                                          "und neu starten, damit die gemeinsame Liste geladen wird.")

    def _pick_shared(self):
        d = filedialog.askdirectory(parent=self, title="Gemeinsamen Ordner wählen")
        if d:
            self.shared_dir.set(os.path.normpath(d))


def selftest(path):
    """Konsolentest ohne Oberfläche und ohne Sendcloud-Aufrufe: python versand_tool.py --selftest datei.csv"""
    sys.stdout.reconfigure(encoding="utf-8")
    orders = read_export(path, 2.01)
    if "--history" in sys.argv:
        data = Sendcloud(Config()).history(progress=lambda n: print(f"\rHistorie: Seite {n}", end=""))
        print(f"\n{len(data)} Artikel bekannt, {apply_history(orders, data)} Aufträge angepasst")
    for o in orders:
        print(o.ref, "|", o.name, "|", o.street, "/", o.house, "/", o.addr2, "|", o.country, o.zip, o.city, "|",
              o.weight_text, "kg", o.weight_source, "|", f"{o.value:.2f} €", "|", o.items_text, "|", o.validate())
    print(len(orders), "Aufträge,", sum(not o.validate() for o in orders), "sendbar")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
    else:
        App().mainloop()
