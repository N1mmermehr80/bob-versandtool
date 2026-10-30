"""
S4T Versandzentrale
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
import hashlib
import http.client
import io
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "S4T Versandzentrale"
APP_VERSION = "1.0.0"  # wird beim Release-Build aus dem Git-Tag (v1.2.3) gesetzt
# GitHub-Repository für die Updateprüfung, Format "besitzer/repo". Leer = keine Updateprüfung.
UPDATE_REPO = "N1mmermehr80/bob-versandtool"
UPDATE_ASSET = "BOB-Sendcloud-Versand.exe"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BOB-Sendcloud-Versand")
CONFIG_PATH = os.path.join(APP_DIR, "config.ini")
LOG_PATH = os.path.join(APP_DIR, "sendungen_log.csv")
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
    "printer_a6": "",          # zuletzt gewählter Drucker je Format (pro PC)
    "printer_a4": "",
    "save_label_pdf": "0",
    "label_dir": "",
    # Speed4Trade CONNECT (Webservice) – ebenfalls firmenspezifisch, daher leer
    "s4t_host": "",            # Adresse des Servers, z. B. IP
    "s4t_user": "",
    "s4t_cert_sha256": "",     # Fingerabdruck des bestätigten Server-Zertifikats
    "s4t_tag": "",             # optional: nur Aufträge mit diesem Auftragskennzeichen (leer = alle im Status)
    "s4t_status_id": "130",    # paid shipping
    "s4t_status_extra": "240",  # weitere Status (z. B. 240 aus dem Shop), nur noch nicht versendete Shop-Aufträge
    "s4t_mandator": "1",
    "s4t_report": "1",         # Versand mit Sendungsnummer an S4T zurückmelden
    # Versandweg: "sendcloud" oder "dhl" (direkt über den eigenen DHL-Geschäftskundenvertrag, nur national)
    "carrier": "sendcloud",
    "dhl_sandbox": "1",        # DHL-Testumgebung (keine echten, kostenpflichtigen Labels)
    "dhl_user": "",            # GKP-Benutzer (Systembenutzer), Passwort/API-Key/Secret verschlüsselt pro PC
    "dhl_billing": "",         # 14-stellige Abrechnungsnummer DHL Paket (EKP + 01 + Teilnahme)
    "dhl_profile": "STANDARD_GRUPPENPROFIL",
    "dhl_print_format": "910-300-400",  # 100 × 150 mm, Thermodruck (Zebra), randlos
    "dhl_return_receiver": "",  # Retouren-Empfänger-ID aus dem GKP, gehört zur Retouren-Abrechnungsnummer
    "dhl_return_billing": "",   # Abrechnungsnummer DHL Retoure (Verfahren 07), nur zum Abrufen der Empfänger-ID
    "dhl_track_user": "",      # GKP-Benutzer mit Recht „Sendungsverfolgung“ (leer = dhl_user), Passwort verschlüsselt
    "color_theme": "Standard",  # Farbschema (THEMES), pro PC
    "color_bar": "",           # eigene Leistenfarbe #RRGGBB (leer = aus dem Schema)
    "color_accent": "",        # eigene Akzentfarbe #RRGGBB (leer = aus dem Schema)
}

# Diese Einstellungen gelten für alle Arbeitsplätze (im gemeinsamen Ordner); Zugangsdaten bleiben pro PC.
SHARED_SETTINGS = ("default_weight_kg", "shipping_option_de", "shipping_option_eu", "contract_id", "brand_id",
                   "sender_name", "sender_company", "sender_email", "sender_phone", "sender_street",
                   "sender_house_number", "sender_postal_code", "sender_city", "sender_country", "use_history",
                   "s4t_host", "s4t_cert_sha256", "s4t_tag", "s4t_status_id", "s4t_status_extra", "s4t_mandator", "s4t_report",
                   "carrier", "dhl_sandbox", "dhl_billing", "dhl_profile", "dhl_print_format",
                   "dhl_return_receiver", "dhl_return_billing")
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
        if not self.public_key or not self["s4t_user"]:
            self._import_env()

    def _import_env(self):
        """Beim ersten Start Zugangsdaten aus einer .env neben dem Programm übernehmen."""
        base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
        for p in (os.path.join(base, ".env"), os.path.join(base, "..", ".env"), os.path.join(os.getcwd(), ".env")):
            if os.path.isfile(p):
                env = {}
                for line in open(p, encoding="utf-8", errors="replace"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip("\"'")
                changed = False
                if not self.public_key and env.get("SENDCLOUD_PUBLIC_KEY") and env.get("SENDCLOUD_SECRET_KEY"):
                    self.set_keys(env["SENDCLOUD_PUBLIC_KEY"], env["SENDCLOUD_SECRET_KEY"])
                    changed = True
                if not self["s4t_user"] and env.get("S4T_Benutzername") and env.get("S4T_Passwort"):
                    self["s4t_user"] = env["S4T_Benutzername"]
                    self.s4t_password = env["S4T_Passwort"]
                    if env.get("S4T_IP") and not self["s4t_host"]:
                        self["s4t_host"] = env["S4T_IP"]
                    changed = True
                if changed:
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

    @property
    def s4t_password(self):
        return unprotect(self.cp["settings"].get("s4t_password_enc", ""))

    @s4t_password.setter
    def s4t_password(self, pw):
        self.cp["settings"]["s4t_password_enc"] = protect(pw)

    @property
    def dhl_password(self):
        return unprotect(self.cp["settings"].get("dhl_password_enc", ""))

    @property
    def dhl_api_key(self):
        return unprotect(self.cp["settings"].get("dhl_api_key_enc", ""))

    @property
    def dhl_api_secret(self):
        return unprotect(self.cp["settings"].get("dhl_api_secret_enc", ""))

    @property
    def dhl_track_password(self):
        return unprotect(self.cp["settings"].get("dhl_track_password_enc", ""))

    @dhl_track_password.setter
    def dhl_track_password(self, pw):
        self.cp["settings"]["dhl_track_password_enc"] = protect(pw)

    def set_dhl(self, api_key, api_secret, password):
        self.cp["settings"]["dhl_api_key_enc"] = protect(api_key.strip())
        self.cp["settings"]["dhl_api_secret_enc"] = protect(api_secret.strip())
        self.cp["settings"]["dhl_password_enc"] = protect(password)

    @property
    def carrier_missing(self):
        """Fehlende Zugangsdaten für den eingestellten Versandweg (leer = bereit)."""
        if self["carrier"] != "dhl":
            return [] if self.public_key and self.secret_key else ["Sendcloud-Zugangsdaten"]
        miss = [] if self.dhl_api_key else ["DHL API-Key"]
        if self["dhl_sandbox"] != "1":
            miss += [n for v, n in ((self["dhl_user"].strip(), "DHL GKP-Benutzer"), (self.dhl_password, "DHL Passwort"),
                                    (re.fullmatch(r"\w{10}\d{2}\w{2}", self["dhl_billing"].strip()),
                                     "DHL Abrechnungsnummer (14-stellig)")) if not v]
        return miss

    @property
    def s4t_ready(self):
        return all(self[k].strip() for k in ("s4t_host", "s4t_user", "s4t_cert_sha256")) \
            and bool(self.s4t_password)

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
        self.pick = 0                # Reihenfolge des Anhakens (Senden und Sammeldruck), nur lokal
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
        self.s4t_id = ""             # Auftrags-ID in Speed4Trade (nur bei Abholung über die API)
        self.ebay = ""               # eBay-Bestellnummer
        self.s4t_reported = ""       # Zeitpunkt der Versandmeldung an Speed4Trade
        self.s4t_error = ""

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
            _fix_address(o)
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
        _set_weight(o, order_weight.get(ref), default_weight)
    return list(orders.values())


def _fix_address(o):
    """Hausnummer aus Straße/Zusatz trennen, deutsche PLZ mit führender 0 ergänzen."""
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


def _set_weight(o, order_weight, default_weight):
    """Paketgewicht aus Artikelgewichten bzw. Auftragsgewicht, sonst Standardgewicht (oder Radsatz-Regel)."""
    if o.discounts:
        o.hints.append(f"{len(o.discounts)} Rabatt-/Gutscheinzeile(n) nicht ins Paket übernommen")
    valid = [i for i in o.items if i["weight"] and i["weight"] >= PLACEHOLDER_WEIGHT_KG]
    total = sum(i["weight"] * i["qty"] for i in valid)
    # Auftragsgewicht ist die Summe der Artikelgewichte – fehlt eins, ist es zu niedrig
    if o.items and len(valid) == len(o.items):
        o.weight, o.weight_source = order_weight or round(total, 3), "Export"
    else:
        o.weight = max(order_weight or 0.0, round(total, 3), default_weight)
        o.weight_source, o.weight_estimated = "Standard", True
        part = f" ({len(o.items) - len(valid)} von {len(o.items)} Artikeln)" if valid else ""
        o.hints.append(f"Gewicht nicht gepflegt{part}, {o.weight:.2f} kg angesetzt".replace(".", ","))
        if len(o.items) == 1 and o.items[0]["qty"] == 1 and WHEELSET_RX.search(o.items[0]["name"]):
            o.parcels, o.weight, o.weight_source = 4, 20.0, "Regel"
            o.hints[-1] = "Radsatz erkannt: 4 Pakete à 20 kg angesetzt – bitte prüfen"


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


def weight_stats(obs, recent=10):
    """Erfahrungswerte je SKU aus Einzelwerten {"Auftrag|SKU": {"parcels", "weight", "ts"}}: häufigste
    Paketanzahl und dazu häufigstes Gewicht der letzten `recent` Aufträge (neue Verpackung schlägt schnell durch)."""
    per_sku = {}
    for key, x in obs.items():
        per_sku.setdefault(key.split("|", 1)[-1], []).append(x)
    result = {}
    for sku, lst in per_sku.items():
        lst = sorted(lst, key=lambda x: x.get("ts", 0))[-recent:]
        ns = [x["parcels"] for x in lst]
        parcels = max(set(ns), key=ns.count)
        ws = [x["weight"] for x in lst if x["parcels"] == parcels]
        result[sku] = {"parcels": parcels, "weight": max(set(ws), key=ws.count), "count": len(lst)}
    return result


class WeightStore:
    """Eigene Ablage der versendeten Gewichte (Ordner „gewichte“), damit die Erfahrungswerte nicht an
    Sendcloud hängen. Jeder Arbeitsplatz schreibt nur seine eigene Datei (keine Konflikte im gemeinsamen
    Ordner), der Sendcloud-Import liegt in sendcloud.json. Gelesen werden alle Dateien zusammen."""
    SENDCLOUD = "sendcloud.json"

    def __init__(self, root, user):
        self.dir = os.path.join(root, "gewichte")
        self.own = os.path.join(self.dir, re.sub(r"[^\w.-]", "_", user) + ".json")

    def _read(self, path):
        for attempt in range(5):  # Datei kann gerade ersetzt werden
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return {}
            except (json.JSONDecodeError, PermissionError):
                time.sleep(0.1)
        raise OSError(f"Datei nicht lesbar: {path}")

    def _write(self, path, obs):
        os.makedirs(self.dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obs, f, ensure_ascii=False)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Ziel wird gerade von einem anderen Platz gelesen
                time.sleep(0.1)
        os.remove(tmp)
        raise OSError(f"Datei nicht schreibbar: {path}")

    def load(self):
        obs = {}
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".json"))
        except FileNotFoundError:
            return obs
        for n in names:  # sendcloud.json zuerst, eigene Werte überschreiben gleiche Aufträge
            if n == self.SENDCLOUD:
                obs = {**self._read(os.path.join(self.dir, n)), **obs}
            else:
                obs.update(self._read(os.path.join(self.dir, n)))
        return obs

    def sendcloud_age(self):
        try:
            return time.time() - os.path.getmtime(os.path.join(self.dir, self.SENDCLOUD))
        except OSError:
            return None

    def put_sendcloud(self, obs):
        """Sendcloud-Werte ergänzen – ältere Einträge bleiben, auch wenn Sendcloud sie nicht mehr liefert."""
        path = os.path.join(self.dir, self.SENDCLOUD)
        self._write(path, {**self._read(path), **obs})

    def add(self, o):
        """Versendetes Gewicht merken, wenn der Auftrag nur einen Artikel (SKU) enthält."""
        skus = {(i["sku"] or "").upper() for i in o.items}
        if len(skus) != 1 or not next(iter(skus)) or not o.weight:
            return
        own = self._read(self.own)
        own[f"{o.ref}|{skus.pop()}"] = {"parcels": o.parcels, "weight": round(o.weight, 3), "ts": int(time.time())}
        self._write(self.own, own)


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
        """Gewichte je Auftrag und SKU aus den Sendungen der letzten `days` Tage (Format wie WeightStore)."""
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
                        rec = per_order.setdefault(f"{x.get('order_number')}|{skus.pop()}", {"w": [], "ts": 0})
                        rec["w"].append(w)
                        try:
                            ts = dt.datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = 0
                        rec["ts"] = max(rec["ts"], int(ts))
            m = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
            url = m.group(1) if m else None
            pages += 1
            if progress:
                progress(pages)
        return {k: {"parcels": len(r["w"]), "weight": max(set(r["w"]), key=r["w"].count), "ts": r["ts"]}
                for k, r in per_order.items()}

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


# --------------------------------------------------------------------------- DHL direkt (Parcel DE Shipping API v2)

DHL_API = {False: "https://api-eu.dhl.com", True: "https://api-sandbox.dhl.com"}
DHL_SANDBOX_LOGIN = ("user-valid", "SandboxPasswort2023!", "33333333330102")  # öffentliche Testdaten von DHL
DHL_PREFIX = "dhl:"  # parcel_id-Kennung für direkt bei DHL angelegte Sendungen (Sendcloud-IDs sind reine Zahlen)
DHL_RETURN_PREFIX = "dhlret:"  # parcel_id-Kennung für DHL-Retourenlabels (nur im Protokoll)
DHL_SHIPPING_API, DHL_RETURNS_API = "/parcel/de/shipping/v2", "/parcel/de/shipping/returns/v1"
PACKSTATION_RX = re.compile(r"packstation\s*(\d{3})\b", re.I)
FILIALE_RX = re.compile(r"(?:post)?filiale\s*(\d{3})\b", re.I)


class DHLError(SendcloudError):
    pass


class DHL:
    """Labels direkt bei DHL (Geschäftskundenvertrag), nur DHL Paket national (V01PAK).
    Gleiche Schnittstelle wie Sendcloud: test, existing, announce, label_pdf."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sandbox = cfg["dhl_sandbox"] == "1"
        self.base = DHL_API[self.sandbox]
        self.user, self.password, self.billing = DHL_SANDBOX_LOGIN if self.sandbox else \
            (cfg["dhl_user"].strip(), cfg.dhl_password, cfg["dhl_billing"].strip())
        # API-Key + GKP-Benutzer per Basic Auth; ein OAuth-Token wird für /orders abgelehnt („no apiproduct match“)
        self._basic = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        self._pdfs = {}  # Labels aus der Anlage-Antwort, spart den zweiten Abruf

    def _call(self, method, path, body=None, api=DHL_SHIPPING_API):
        req = urllib.request.Request(self.base + api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Basic " + self._basic,
                                              "dhl-api-key": self.cfg.dhl_api_key, "Accept": "application/json",
                                              "Accept-Language": "de-DE", "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                if e.code == 401:
                    try:
                        detail = json.loads(e.read()).get("detail") or ""
                    except Exception:
                        detail = ""
                    hint = " – der API-Key ist für die Testumgebung nicht freigeschaltet"                         if self.sandbox and "ApiKey" in detail else " – API-Key, GKP-Benutzer und Passwort prüfen"
                    raise DHLError(f"DHL hat die Anmeldung abgelehnt (HTTP 401{': ' + detail if detail else ''}){hint}")
                raw = e.read().decode(errors="replace")
                try:
                    msg = self._errors(json.loads(raw))
                except Exception:
                    msg = raw
                raise DHLError(f"HTTP {e.code}: {msg[:500]}")
            except urllib.error.URLError as e:
                raise DHLError(f"Keine Verbindung zu DHL: {e.reason}")

    @staticmethod
    def _errors(d, states=("Error",)):
        msgs = [f'{m.get("property", "")}: {m.get("validationMessage", "")}'.strip(": ")
                for it in d.get("items") or [] for m in it.get("validationMessages") or []
                if m.get("validationState") in states]
        st = d.get("status") if isinstance(d.get("status"), dict) else d  # Retouren-API: Fehler auf oberster Ebene
        st = st.get("sstatus") if isinstance(st.get("sstatus"), dict) else st
        return "; ".join(msgs) or st.get("detail") or st.get("title") or json.dumps(d, ensure_ascii=False)

    def _shipper(self):
        c = self.cfg
        s = {"name1": c["sender_company"][:50], "addressStreet": c["sender_street"][:50],
             "addressHouse": c["sender_house_number"][:10], "postalCode": c["sender_postal_code"],
             "city": c["sender_city"][:40], "country": "DEU"}
        if c["sender_name"].strip():
            s["contactName"] = c["sender_name"][:80]
        if c["sender_email"].strip():
            s["email"] = c["sender_email"][:80]
        return s

    @staticmethod
    def consignee(o: Order):
        """Hausadresse, Packstation oder Postfiliale. Die Postnummer steht je nach Kunde im Zusatz, Firmen- oder
        Namensfeld."""
        text = " ".join((o.street, o.house, o.addr2, o.company))
        postnr = re.search(r"\b(\d{6,10})\b", " ".join((o.addr2, o.company, o.name, o.street, o.house)))
        m = PACKSTATION_RX.search(text)
        if m:
            if not postnr:
                raise DHLError("Packstation ohne Postnummer – bitte im Adresszusatz ergänzen")
            return {"name": o.name[:50], "lockerID": int(m.group(1)), "postNumber": postnr.group(1),
                    "postalCode": o.zip, "city": o.city[:40], "country": "DEU"}
        m = FILIALE_RX.search(text)
        if m and 401 <= int(m.group(1)) <= 999:
            p = {"name": o.name[:50], "retailID": int(m.group(1)), "postalCode": o.zip, "city": o.city[:80],
                 "country": "DEU"}
            if postnr:
                p["postNumber"] = postnr.group(1)
            elif o.email:
                p["email"] = o.email[:80]
            else:
                raise DHLError("Postfiliale ohne Postnummer oder E-Mail – bitte ergänzen")
            return p
        a = {"name1": (o.company or o.name)[:50], "addressStreet": o.street[:50], "postalCode": o.zip,
             "city": o.city[:40], "country": "DEU"}
        if o.company and o.name:
            a["name2"] = o.name[:50]
        if o.house:
            a["addressHouse"] = o.house[:10]
        if o.addr2:
            a["additionalAddressInformation1"] = o.addr2[:60]
        if o.email:
            a["email"] = o.email[:80]
        if o.phone:
            a["phone"] = o.phone[:20]
        return a

    def _shipment(self, o: Order):
        if o.country != "DE":
            raise DHLError("Über DHL direkt ist nur Inlandsversand eingerichtet – bitte über Sendcloud senden")
        if o.weight > 31.5:
            raise DHLError(f"{o.weight:.2f} kg je Paket – DHL Paket erlaubt höchstens 31,5 kg")
        return {"product": "V01PAK", "billingNumber": self.billing, "refNo": f"Auftrag {o.ref}"[:35],
                "shipper": self._shipper(), "consignee": self.consignee(o),
                "details": {"weight": {"uom": "kg", "value": round(max(o.weight, 0.01), 3)}},
                # bucht DHL bei manchen Verträgen ohnehin automatisch hinzu – ohne Angabe scheitert die Sendung am Hinweis
                **({} if self.sandbox else {"services": {"goGreenPlus": True}})}

    def _query(self, **extra):
        q = {"printFormat": self.cfg["dhl_print_format"] or "910-300-400", "includeDocs": "include", **extra}
        return "?" + urllib.parse.urlencode(q)

    def test(self):
        """Anmeldung und Abrechnungsnummer prüfen: Probe-Sendung an die eigene Adresse, nur validiert."""
        me = Order("00000000")
        c = self.cfg
        me.name, me.street, me.house = c["sender_company"], c["sender_street"], c["sender_house_number"]
        me.zip, me.city, me.country, me.weight = c["sender_postal_code"], c["sender_city"], "DE", 1.0
        d = self._call("POST", "/orders" + self._query(validate="true"),
                       {"profile": c["dhl_profile"] or "STANDARD_GRUPPENPROFIL", "shipments": [self._shipment(me)]})
        if any((it.get("sstatus") or {}).get("status", 200) >= 400 for it in d.get("items") or []):
            raise DHLError(self._errors(d))

    def return_receivers(self, billing=""):
        """Retouren-Empfänger des Kontos: [(receiverId, Abrechnungsnummer, Beschreibung)]. Nur Empfänger mit
        Abrechnungsnummer können Labels erzeugen."""
        # DHL beantwortet kombinierte Filter (countryCode + billingNumber) mit HTTP 500 – deshalb einzeln und
        # notfalls ganz ohne Filter; die passende Abrechnungsnummer sucht der Aufrufer aus der Liste heraus
        tries = ([{"billingNumber": billing}] if billing else []) + [{"countryCode": "deu"}, {}]
        for n, q in enumerate(tries):
            try:
                d = self._call("GET", "/locations" + ("?" + urllib.parse.urlencode(q) if q else ""),
                               api=DHL_RETURNS_API)
            except DHLError as e:
                if n == len(tries) - 1 or "401" in str(e) or "403" in str(e):
                    raise
                continue
            found = self._receivers(d)
            if found or n == len(tries) - 1:
                return found
        return []

    @staticmethod
    def _receivers(d):
        found = []

        def walk(x):
            if isinstance(x, dict):
                if x.get("receiverId"):
                    a = x.get("address") if isinstance(x.get("address"), dict) else x
                    desc = " ".join(str(a.get(k) or "") for k in ("name1", "name", "postalCode", "city")).strip()
                    found.append((str(x["receiverId"]), str(x.get("billingNumber") or ""), desc))
                for v in x.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(d)
        return found

    def create_return(self, o: Order, weight, value, reference):
        """Retourenlabel: der Kunde (o) ist Absender, Empfänger ist die Retouren-Empfänger-ID aus dem GKP.
        Rückgabe: (Sendungsnummer, Label-PDF, QR-Code-PNG oder None)."""
        if PACKSTATION_RX.search(" ".join((o.street, o.addr2, o.company))):
            raise DHLError("Retouren können nicht von einer Packstation aus beauftragt werden – bitte die "
                           "Hausadresse des Kunden eintragen")
        street, house = o.street.strip(), o.house.strip()
        if not house:  # Hausnummer steht oft mit in der Straße
            m = re.match(r"^(.*?)[\s,]+(\d+\s*[a-zA-Z]?(?:\s*[-/]\s*\d+\s*[a-zA-Z]?)?)$", street)
            if m:
                street, house = m.group(1), m.group(2)
        shipper = {"name1": (o.company or o.name)[:50], "addressStreet": street[:50], "addressHouse": house[:10],
                   "postalCode": o.zip.strip(), "city": o.city.strip()[:40]}
        if o.company and o.name:
            shipper["name2"] = o.name[:50]
        if o.addr2:
            shipper["name3"] = o.addr2[:50]
        if o.email:
            shipper["email"] = o.email[:80]
        if o.phone:
            shipper["phone"] = o.phone[:20]
        receiver = "deu" if self.sandbox else self.cfg["dhl_return_receiver"].strip()
        if not receiver:
            raise DHLError("Die Retouren-Empfänger-ID fehlt – bitte unter Einstellungen → DHL die "
                           "Retouren-Abrechnungsnummer eintragen und „Bei DHL abrufen“ klicken")
        body = {"receiverId": receiver,
                "customerReference": reference[:30], "shipmentReference": reference[:30], "shipper": shipper,
                "itemWeight": {"uom": "kg", "value": round(max(weight, 0.01), 3)},
                "itemValue": {"currency": "EUR", "value": round(max(value, 0), 2)}}
        if not self.sandbox:  # wie beim Versand: der Vertrag verlangt GoGreen Plus
            body["services"] = {"goGreenPlus": True}
        try:
            d = self._call("POST", "/orders?labelType=BOTH", body, api=DHL_RETURNS_API)
        except DHLError as e:
            if "recipient" in str(e).lower() or "receiver" in str(e).lower():
                raise DHLError(f"DHL kennt die Retouren-Empfänger-ID „{receiver}“ nicht ({e}). Bitte unter "
                               "Einstellungen → DHL „Bei DHL abrufen“ klicken")
            raise
        if not d.get("shipmentNo") or not (d.get("label") or {}).get("b64"):
            raise DHLError(f"Keine Retouren-Sendungsnummer erhalten: {self._errors(d)}")
        qr = (d.get("qrLabel") or {}).get("b64")
        return d["shipmentNo"], base64.b64decode(d["label"]["b64"]), base64.b64decode(qr) if qr else None

    def existing(self, o: Order):
        """DHL kennt keine Suche nach Auftragsnummer – deshalb in Speed4Trade nachsehen, ob schon eine
        Sendungsnummer am Auftrag steht (schützt vor doppelten Labels, auch von anderen Arbeitsplätzen)."""
        if not (o.s4t_id and self.cfg.s4t_ready):
            return []
        x = S4T(self.cfg).order(o.s4t_id)
        tracks = [(t.text or "").strip() for t in x.iter("tracking_number")] if x is not None else []
        return [{"tracking_number": t, "id": DHL_PREFIX + t} for t in tracks if t]

    def announce(self, o: Order):
        d = self._call("POST", "/orders" + self._query(mustEncode="false"),
                       {"profile": self.cfg["dhl_profile"] or "STANDARD_GRUPPENPROFIL", "shipments": [self._shipment(o)]})
        it = (d.get("items") or [{}])[0]
        if not it.get("shipmentNo"):
            raise DHLError(f"Keine Sendungsnummer erhalten: {self._errors(d)}")
        hints = self._errors({"items": [it]}, states=("Warning",)) if it.get("validationMessages") else ""
        if hints and not hints.startswith("{"):
            o.hints.append("DHL: " + hints)
        if (it.get("label") or {}).get("b64"):
            self._pdfs[it["shipmentNo"]] = base64.b64decode(it["label"]["b64"])
        return {"tracking_number": it["shipmentNo"], "id": DHL_PREFIX + it["shipmentNo"]}

    def label_pdf(self, parcel_id):
        """Label erneut abrufen – bei DHL nur bis zum Tagesabschluss möglich."""
        no = str(parcel_id).removeprefix(DHL_PREFIX)
        if no in self._pdfs:
            return self._pdfs[no]
        d = self._call("GET", "/orders" + self._query() + "&" + urllib.parse.urlencode({"shipment": no}))
        b64 = ((d.get("items") or [{}])[0].get("label") or {}).get("b64")
        if not b64:
            raise DHLError(f"Label nicht mehr abrufbar (nach dem Tagesabschluss): {self._errors(d)}")
        return base64.b64decode(b64)


def carrier(cfg: Config):
    """Versandweg laut Einstellungen."""
    return DHL(cfg) if cfg["carrier"] == "dhl" else Sendcloud(cfg)


def label_client(cfg: Config, parcel_id):
    """Zum Nachladen eines Labels: Herkunft steht an der ID, unabhängig vom eingestellten Versandweg."""
    return DHL(cfg) if str(parcel_id).startswith(DHL_PREFIX) else Sendcloud(cfg)


# --------------------------------------------------------------------------- DHL-Sendungsverfolgung

DHL_TRACK_SANDBOX_LOGIN = ("zt12345", "geheim")  # öffentliche Testdaten von DHL
DHL_TRACK_PAGE = "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode={}"
TRACK_FILE = "sendungsverfolgung.json"
TRACK_RECHECK_MIN = 30   # nicht zugestellte Sendungen frühestens nach so vielen Minuten erneut abfragen
TRACK_STALL_DAYS = 3     # so lange ohne neues Ereignis -> „hängt“
TRACK_PROBLEM_RX = re.compile(r"rücksend|zurückgesandt|zurück an den absender|retour|nicht zustellbar|unzustellbar|"
                              r"adresse (nicht|unvollständig|fehlerhaft|unbekannt)|adressfehler|empfänger unbekannt|"
                              r"verzogen|beschädig|verweigert|nicht angenommen|nicht abgeholt|verlust", re.I)
TRACK_PICKUP_RX = re.compile(r"zur abholung|abholbereit|abholung bereit|in die packstation|benachrichtigt|"
                             r"nicht angetroffen", re.I)


class DHLTracking:
    """Sendungsverfolgung für Geschäftskunden (DHL Parcel DE Tracking, d-get-piece-detail): XML im GET-Parameter,
    bis 15 Sendungsnummern je Abfrage, höchstens 1000 Abfragen am Tag und 3 je Sekunde. Zugestellte Sendungen
    sollen laut DHL nicht erneut abgefragt werden. Eine Liste aller Sendungen bietet DHL nicht an – die Nummern
    kommen aus dem Versandprotokoll (tracking_archive)."""
    BATCH = 15

    def __init__(self, cfg: Config):
        self.sandbox = cfg["dhl_sandbox"] == "1"
        self.url = DHL_API[self.sandbox] + "/parcel/de/tracking/v0/shipments"
        self.key, self.secret = cfg.dhl_api_key, cfg.dhl_api_secret
        self.user, self.password = DHL_TRACK_SANDBOX_LOGIN if self.sandbox else \
            (cfg["dhl_track_user"].strip() or cfg["dhl_user"].strip(), cfg.dhl_track_password or cfg.dhl_password)
        self._basic = False  # Basic Auth mit API-Key/Secret nur, falls das Gateway sie verlangt

    @property
    def missing(self):
        return [n for v, n in ((self.key, "DHL API-Key"), (self.user, "Benutzer für die Sendungsverfolgung"),
                               (self.password, "Passwort für die Sendungsverfolgung")) if not v]

    def query(self, numbers):
        """{Sendungsnummer: Ergebnis} für bis zu 15 Nummern. Nummern ohne Daten bei DHL: Ergebnis mit 'error'."""
        attrs = {"appname": self.user, "password": self.password, "request": "d-get-piece-detail",
                 "language-code": "de", "piece-code": ";".join(numbers)}
        quote = {'"': "&quot;"}
        xml = ('<?xml version="1.0" encoding="UTF-8" standalone="no"?><data '
               + " ".join(f'{k}="{xml_escape(v, quote)}"' for k, v in attrs.items()) + "/>")
        url = self.url + "?xml=" + urllib.parse.quote(xml)
        for attempt in range(4):
            headers = {"DHL-API-Key": self.key, "Accept": "application/xml",
                       "User-Agent": f"BOB-Sendcloud-Versand/{APP_VERSION}"}
            if self._basic:
                headers["Authorization"] = "Basic " + base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    return self._parse(r.read(), numbers)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not self._basic and self.secret:
                    self._basic = True
                    continue
                if e.code == 429 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                if e.code in (401, 403):
                    raise DHLError(f"DHL hat die Abfrage abgelehnt (HTTP {e.code}) – ist die App im DHL Developer "
                                   "Portal für „DHL Parcel DE Tracking“ freigeschaltet?")
                raise DHLError(f"HTTP {e.code} bei der Sendungsverfolgung: {e.read().decode(errors='replace')[:300]}")
            except urllib.error.URLError as e:
                raise DHLError(f"DHL nicht erreichbar: {e.reason}")
        raise DHLError("DHL antwortet nicht (zu viele Anfragen) – bitte später erneut versuchen.")

    @staticmethod
    def _parse(raw, numbers):
        root = ET.fromstring(raw)
        code = root.get("code", "0")
        if code in ("5", "6"):
            raise DHLError("Anmeldung bei der Sendungsverfolgung fehlgeschlagen – Benutzer und Passwort prüfen "
                           "(der Benutzer braucht im Geschäftskundenportal das Recht „Sendungsverfolgung“)."
                           + (" Zu viele Fehlversuche, Zugang vorübergehend gesperrt." if code == "6" else ""))
        result = {n: {"error": root.get("error") or "Keine Daten bei DHL"} for n in numbers}
        for s in root.iter("data"):
            if s.get("name") not in ("pieceshipment", "piece-shipment"):
                continue
            num = s.get("piece-code") or s.get("searched-piece-code") or ""
            if s.get("error-status", "0") not in ("0", ""):
                result[num] = {"error": s.get("error") or "Keine Daten bei DHL"}
                continue
            events = [{"time": e.get("event-timestamp", ""), "text": e.get("event-text") or e.get("event-status", ""),
                       "place": ", ".join(x for x in (e.get("event-location", ""), e.get("event-country", "")) if x),
                       "code": e.get("standard-event-code", "")}
                      for e in s.iter("data") if e.get("name") in ("pieceevent", "piece-event")]
            result[num] = {"status": s.get("status") or s.get("short-status", ""),
                           "short": s.get("short-status") or s.get("status", ""),
                           "time": s.get("status-timestamp") or s.get("last-event-timestamp")
                           or (events[-1]["time"] if events else ""),
                           "delivered": s.get("delivery-event-flag") == "1",
                           "return": s.get("ruecksendung") == "true",
                           "recipient": s.get("recipient-name") or s.get("pan-recipient-name", ""),
                           "city": s.get("recipient-city") or s.get("pan-recipient-city", ""),
                           "events": events}
        return result


def _dhl_time(s):
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s.strip()[:19], fmt)
        except ValueError:
            pass
    return None


def tracking_archive(days):
    """Alle Sendungsnummern aus dem Versandprotokoll (gemeinsamer Ordner und lokal) der letzten `days` Tage:
    {Sendungsnummer: {"sent", "ref", "name", "via"}} – mehrere Zeilen je Nummer werden zusammengefasst."""
    paths = [LOG_PATH]
    if BACKEND is not None:
        try:
            with os.scandir(BACKEND.dirs["protokoll"]) as it:
                paths += [e.path for e in it if e.name.endswith(".csv")]
        except OSError:
            pass
    since = dt.datetime.now() - dt.timedelta(days=days)
    out = {}
    for p in paths:
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f, delimiter=";"))[1:]
        except OSError:
            continue
        for row in rows:
            if len(row) < 5 or not row[2].strip():
                continue
            try:
                sent = dt.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if sent < since:
                continue
            via = "Retoure" if row[3].startswith(DHL_RETURN_PREFIX) else "DHL direkt" \
                if row[3].startswith(DHL_PREFIX) else "Sendcloud" if row[3].strip() else "manuell"
            for num in re.split(r"[,;\s]+", row[2]):
                if not num:
                    continue
                cur = out.get(num)
                if cur is None or sent < cur["sent"]:
                    out[num] = {"sent": sent, "ref": row[1], "name": row[4] or (cur or {}).get("name", ""),
                                "via": via if via != "manuell" or cur is None else cur["via"]}
    return out


def tracking_state(info, sent):
    """Einordnung für Liste und Farben: (Kategorie, Text). Kategorien: zugestellt, problem, achtung, unterwegs."""
    now = dt.datetime.now()
    if not info:
        return "unterwegs", "noch nicht abgefragt"
    if info.get("error"):
        if (now - sent).days >= 2:
            return "achtung", "Keine Daten bei DHL"
        return "unterwegs", "Noch nicht bei DHL erfasst"
    if info.get("delivered"):
        return "zugestellt", info.get("short") or "Zugestellt"
    texts = " ".join([info.get("status", ""), info.get("short", "")] + [e["text"] for e in info["events"][-3:]])
    if info.get("return") or TRACK_PROBLEM_RX.search(texts):
        return "problem", info.get("short") or info.get("status") or "Problem"
    if TRACK_PICKUP_RX.search(info.get("status", "") + " " + info.get("short", "")):
        return "achtung", info.get("short") or info.get("status")
    last = _dhl_time(info.get("time", "")) or sent
    if (now - last).days >= TRACK_STALL_DAYS:
        return "achtung", f"Seit {(now - last).days} Tagen keine Bewegung"
    return "unterwegs", info.get("short") or info.get("status") or "unterwegs"


class TrackingStore:
    """Letzte DHL-Antworten je Sendungsnummer, im gemeinsamen Ordner für alle Plätze (spart Abfragen)."""

    def __init__(self):
        self.path = os.path.join(BACKEND.root if BACKEND is not None else APP_DIR, TRACK_FILE)

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, updates):
        """Neue Ergebnisse einmischen – ein anderer Platz kann inzwischen eigene geschrieben haben."""
        data = self.load()
        for num, rec in updates.items():
            if rec.get("checked", "") >= data.get(num, {}).get("checked", ""):
                data[num] = rec
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{re.sub(r'[^\w.-]', '_', USER)}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)
                return data
            except PermissionError:
                time.sleep(0.1)
        os.remove(tmp)
        return data


# --------------------------------------------------------------------------- Speed4Trade CONNECT

class S4TError(Exception):
    pass


S4T_PATH = "/services/ecs-ws.ecs-wsHttpsSoap11Endpoint/"
S4T_INTERVAL_MS = 5 * 60 * 1000  # automatischer Abgleich im Hintergrund
S4T_NS = "http://webservice.web.speed4trade.com"
S4T_PARAM = {"updateOrderStatus": "argRequestData"}  # alle anderen Methoden: requestData (siehe WSDL)


def s4t_fingerprint(host):
    """SHA-256 des Server-Zertifikats. Wird ohne Zugangsdaten abgerufen und vom Benutzer bestätigt."""
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    c = http.client.HTTPSConnection(host, 443, context=ctx, timeout=20)
    try:
        c.connect()
        return hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
    finally:
        c.close()


class S4T:
    """SOAP-Zugriff auf den Webservice. Das Server-Zertifikat ist selbst ausgestellt und lautet nicht auf die
    Adresse, über die der Server erreichbar ist. Deshalb wird statt der üblichen Prüfung der bestätigte
    Fingerabdruck verglichen – Zugangsdaten gehen nur an genau diesen Server."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.host = cfg["s4t_host"].strip()
        self.pin = cfg["s4t_cert_sha256"].strip().upper()
        self.auth = "Basic " + base64.b64encode(f'{cfg["s4t_user"]}:{cfg.s4t_password}'.encode()).decode()
        self.pickups = set()  # Auftragsnummern der übersprungenen Selbstabholer (letztes fetch_orders)

    def _call(self, method, request_xml):
        param = S4T_PARAM.get(method, "requestData")
        envelope = (f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="{S4T_NS}">'
                    f'<soapenv:Body><ws:{method}><ws:{param}>{xml_escape(request_xml)}</ws:{param}></ws:{method}>'
                    '</soapenv:Body></soapenv:Envelope>')
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # ersetzt durch den Fingerabdruck-Vergleich
        c = http.client.HTTPSConnection(self.host, 443, context=ctx, timeout=120)
        try:
            try:
                c.connect()
            except OSError as e:
                raise S4TError(f"Speed4Trade nicht erreichbar ({self.host}): {e}")
            fp = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
            if not self.pin or fp != self.pin:
                raise S4TError("Das Zertifikat des Speed4Trade-Servers stimmt nicht mit dem bestätigten überein. "
                               "Es wurde nichts gesendet. Bitte unter Einstellungen → Speed4Trade prüfen.")
            c.request("POST", S4T_PATH, body=envelope.encode("utf-8"),
                      headers={"Authorization": self.auth, "Content-Type": "text/xml; charset=utf-8",
                               "SOAPAction": "urn:" + method})
            r = c.getresponse()
            body = r.read()
        finally:
            c.close()
        if r.status == 401:
            raise S4TError("Speed4Trade hat die Anmeldung abgelehnt (Benutzer/Passwort prüfen).")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise S4TError(f"Speed4Trade: unerwartete Antwort (HTTP {r.status})")
        fault = root.find(".//{*}Fault/faultstring")
        if fault is not None:
            raise S4TError(f"Speed4Trade: {fault.text}")
        ret = root.find(".//{*}return")
        if ret is None or not ret.text:
            raise S4TError(f"Speed4Trade: leere Antwort (HTTP {r.status})")
        resp = ET.fromstring(ret.text.encode("utf-8"))
        rep = resp.find("report")
        if rep is not None and int(rep.get("return_code", "0")) < 0:
            raise S4TError(f"Speed4Trade meldet Fehler {rep.get('return_code')}: "
                           f"{(rep.findtext('.//error_description') or '').strip()}")
        return resp

    def test(self):
        resp = self._call("fetchMandators", '<request method="fetchMandators" version="1.0.0"></request>')
        return [(m.findtext("mandator_id"), m.findtext("identifier")) for m in resp.findall("mandator")]

    def fetch_orders(self, default_weight):
        """Aufträge im eingestellten Status (130 = bezahlt, Versand offen), optional nur mit einem Kennzeichen.
        Dazu Shop-Aufträge in den weiteren Status (240), sofern noch nicht versendet. Selbstabholer werden
        übersprungen (kein Versand, Abholung und Status pflegt das Team in CONNECT)."""
        main, *extra = s4t_statuses(self.cfg)
        orders, seen = [], set()
        self.pickups = set()
        for status in (main, *extra):
            for x in self._fetch_status(status):
                if x.findtext("order_id") in seen:
                    continue
                if status != main and not s4t_shop_open(x):
                    continue
                if s4t_pickup(x):
                    self.pickups.add((x.findtext("order_number") or "").strip())
                    continue
                seen.add(x.findtext("order_id"))
                orders.append(s4t_order(x, default_weight))
        return orders

    def _fetch_status(self, status):
        c = self.cfg
        flt = (f'<mandator_id filter_method="=" filter_value="{int(c["s4t_mandator"] or 1)}"/>'
               f'<order_status_id filter_method="=" filter_value="{status}"/>')
        if c["s4t_tag"].strip():  # Filter erwartet den Namen des Kennzeichens, nicht die ID
            flt += (f'<order_tags filter_method="IN"><filter_values><filter_value>{xml_escape(c["s4t_tag"].strip())}'
                    '</filter_value></filter_values></order_tags>')
        found_all, page = [], 1
        while True:
            resp = self._call("fetchOrders", f'<request method="fetchOrders" version="1.0.0"><filter>{flt}</filter>'
                                             '<use_modification_context_filter>false</use_modification_context_filter>'
                                             f'<orders_per_page>100</orders_per_page><page>{page}</page></request>')
            found = resp.findall("order")
            found_all += found
            pages = int(resp.findtext("report/total_number_of_pages") or 1)
            if page >= pages or not found:
                return found_all
            page += 1

    def order(self, order_id):
        resp = self._call("fetchOrders", '<request method="fetchOrders" version="1.0.0"><filter>'
                                         f'<order_ids filter_method="IN"><filter_values><filter_value>{int(order_id)}'
                                         '</filter_value></filter_values></order_ids></filter>'
                                         '<use_modification_context_filter>false</use_modification_context_filter></request>')
        return resp.find("order")

    def report_shipped(self, o: Order):
        """Versand mit Sendungsnummer(n) melden – S4T setzt den Status im passenden Workflow auf versendet.
        Bei Shop-Aufträgen (COMMERCE) übernimmt S4T mit den Paketdaten nur die Sendungsnummer und lässt den Status
        stehen; erst ein zweites SHIPPED ohne Paketdaten setzt ihn. Deshalb wird nachgelesen. Rückgabe: Warnungen."""
        open_states = {str(x) for x in s4t_statuses(self.cfg)}
        before = self.order(o.s4t_id)
        if before is not None and before.findtext("order_status/order_status_id") not in open_states:
            return []  # schon weitergesetzt (z. B. von Hand in CONNECT) – erneutes SHIPPED liefert sonst Fehler -9
        # bereits eingetragene Sendungsnummern nicht noch einmal senden, S4T legte sonst weitere Pakete an
        known = {(t.text or "").strip() for t in before.iter("tracking_number")} if before is not None else set()
        infos = "".join(f"<shipping_information><tracking_number>{xml_escape(t)}</tracking_number>"
                        "<shipping_provider_identifier>DHL</shipping_provider_identifier>"
                        f"<shipping_date>{dt.datetime.now():%Y-%m-%dT%H:%M:%S}</shipping_date></shipping_information>"
                        for t in o.tracking.split(", ") if t and t not in known)
        def shipped(infos=""):
            resp = self._call("updateOrderStatus",
                              f'<request method="updateOrderStatus" version="1.0.0"><order significant_key="order_id">'
                              f'<mandator_id>{int(self.cfg["s4t_mandator"] or 1)}</mandator_id>'
                              f'<order_id>{int(o.s4t_id)}</order_id><status>SHIPPED</status>'
                              + (f'<shipping_informations>{infos}</shipping_informations>' if infos else '')
                              + '</order></request>')
            return [w.text.strip() for w in resp.iter("warning_message") if (w.text or "").strip()]
        warnings = shipped(infos)
        after = self.order(o.s4t_id)
        if infos and after is not None and after.findtext("order_status/order_status_id") in open_states:
            warnings += shipped()
            after = self.order(o.s4t_id)
        if after is not None and after.findtext("order_status/order_status_id") in open_states:
            name = after.findtext("order_status/order_status_identifier") or ""
            raise S4TError("Sendungsnummer ist eingetragen, der Auftrag steht aber weiter auf "
                           f"{after.findtext('order_status/order_status_id')} „{name}“ "
                           "statt versendet. Bitte in CONNECT auf versendet setzen"
                           + (f" (S4T: {'; '.join(warnings)})" if warnings else ""))
        return warnings


def s4t_statuses(cfg):
    """Abzuholende Status: Hauptstatus zuerst, dann die weiteren (Komma-getrennt), ohne Doppelte."""
    out = [int(cfg["s4t_status_id"] or 130)]
    for v in re.split(r"[,;\s]+", cfg["s4t_status_extra"]):
        if v.isdigit() and int(v) not in out:
            out.append(int(v))
    return out


def s4t_shop_open(x):
    """Weitere Status (240) enthalten auch alte Marktplatz-Aufträge (Kaufland) – dort nur Aufträge aus dem eigenen
    Shop (COMMERCE), die noch nicht versendet sind (keine Sendungsnummer, kein Paket)."""
    if "COMMERCE" not in (x.findtext(".//sub_channel/sub_channel_identifier") or "").upper():
        return False
    return not any((e.text or "").strip() for tag in ("tracking_number", "packet_id") for e in x.iter(tag))


def s4t_pickup(x):
    """Selbstabholer: Versandart „Selbstabholung“ oder eBay-Kennzeichen ISPU (In-Store-Pickup)."""
    if "abhol" in (x.findtext("dispatch_type/identifier") or "").lower():
        return True
    return any((t.text or "").strip().upper() == "ISPU" for t in x.iter("tag_identifier"))


def s4t_order(x, default_weight):
    """fetchOrders-<order> in einen Auftrag des Tools umwandeln (gleiche Prüfungen wie beim CSV-Import)."""
    t = lambda el, tag: (el.findtext(tag) or "").strip() if el is not None else ""
    o = Order(t(x, "order_number"))
    o.s4t_id, o.ebay = t(x, "order_id"), t(x, "specific_order_data/ebay_order_data/ebay_order_number")
    a = x.find("shipping_address")
    o.name = " ".join(p for p in (t(a, "first_name"), t(a, "last_name")) if p)
    o.company, o.street, o.house = t(a, "firm"), t(a, "street"), t(a, "house_number")
    o.addr2, o.zip, o.city = t(a, "street_addition"), t(a, "postal_code"), t(a, "city")
    o.country, o.phone = t(a, "country_code_iso").upper(), t(a, "phone_number")
    if not o.street and t(a, "street_and_house_number"):
        o.street = t(a, "street_and_house_number")
    if not o.name:
        o.name = o.company
    em = next((e.text.strip() for e in x.iter("email_address") if e.text and e.text.strip()), "")
    if em and not EMAIL_RX.match(em):
        o.hints.append(f"E-Mail ungültig, nicht übernommen ({em})")
        em = ""
    o.email = em
    if not re.fullmatch(r"[A-Z]{2}", o.country):
        o.hints.append(f"Land unklar ('{o.country}'), DE angenommen")
        o.country = "DE"
    _fix_address(o)
    for it in x.findall("order_items/order_item"):
        kind = t(it, "type") or "0"
        qty = (_num(t(it, "quantity")) or 0) - (_num(t(it, "canceled_quantity")) or 0)
        if qty <= 0 or kind in ("3", "4"):  # storniert, Serviceleistung, Retoure
            continue
        item = {"sku": t(it, "internal_item_number") or t(it, "item_id"), "name": t(it, "order_item_identifier"),
                "qty": qty, "price": _num(t(it, "price")) or 0.0, "ean": "", "weight": None}
        if kind in ("1", "2") or item["price"] < 0:
            item["price"] = -abs(item["price"])
            o.discounts.append(item)
        else:
            o.items.append(item)
    grams = _num(t(x, "weight_in_gram"))
    _set_weight(o, grams / 1000 if grams else None, default_weight)
    o.hints.insert(0, "Aus Speed4Trade abgeholt" + (f", eBay-Bestellung {o.ebay}" if o.ebay else ""))
    return o


# --------------------------------------------------------------------------- Direkter Druck (ohne PDF-Programm)

class PrintCancelled(Exception):
    pass


def _gdi():
    """Windows-Druck-APIs mit korrekten 64-bit-Typen (sonst werden Handles abgeschnitten)."""
    from ctypes import wintypes as W
    k32, gdi, cdlg = ctypes.windll.kernel32, ctypes.windll.gdi32, ctypes.windll.comdlg32
    vp = ctypes.c_void_p
    k32.GlobalAlloc.argtypes, k32.GlobalAlloc.restype = [W.UINT, ctypes.c_size_t], vp
    k32.GlobalLock.argtypes, k32.GlobalLock.restype = [vp], vp
    k32.GlobalUnlock.argtypes, k32.GlobalFree.argtypes = [vp], [vp]
    gdi.StartDocW.argtypes = [vp, vp]
    for f in (gdi.StartPage, gdi.EndPage, gdi.EndDoc, gdi.DeleteDC):
        f.argtypes = [vp]
    gdi.GetDeviceCaps.argtypes = [vp, ctypes.c_int]
    gdi.StretchDIBits.argtypes = [vp] + [ctypes.c_int] * 8 + [vp, vp, W.UINT, W.DWORD]
    cdlg.PrintDlgW.argtypes = [vp]
    return k32, gdi, cdlg


class _PRINTDLGW(ctypes.Structure):
    _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
    _fields_ = [("lStructSize", ctypes.c_uint32), ("hwndOwner", ctypes.c_void_p), ("hDevMode", ctypes.c_void_p),
                ("hDevNames", ctypes.c_void_p), ("hDC", ctypes.c_void_p), ("Flags", ctypes.c_uint32),
                ("nFromPage", ctypes.c_uint16), ("nToPage", ctypes.c_uint16), ("nMinPage", ctypes.c_uint16),
                ("nMaxPage", ctypes.c_uint16), ("nCopies", ctypes.c_uint16), ("hInstance", ctypes.c_void_p),
                ("lCustData", ctypes.c_void_p), ("lpfnPrintHook", ctypes.c_void_p),
                ("lpfnSetupHook", ctypes.c_void_p), ("lpPrintTemplateName", ctypes.c_void_p),
                ("lpSetupTemplateName", ctypes.c_void_p), ("hPrintTemplate", ctypes.c_void_p),
                ("hSetupTemplate", ctypes.c_void_p)]


def _devnames(k32, printer):
    """DEVNAMES-Block, damit der Druckdialog den zuletzt benutzten Drucker vorauswählt."""
    text = "\0" + printer + "\0\0"
    raw = ctypes.create_string_buffer(8 + len(text) * 2)
    ctypes.memmove(ctypes.addressof(raw), (ctypes.c_uint16 * 4)(4, 5, 6 + len(printer), 0), 8)
    ctypes.memmove(ctypes.addressof(raw) + 8, text.encode("utf-16-le"), len(text) * 2)
    h = k32.GlobalAlloc(0x0042, len(raw))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    ctypes.memmove(k32.GlobalLock(h), raw, len(raw))
    k32.GlobalUnlock(h)
    return h


def print_pdf(pdf: bytes, owner_hwnd=0, printer="", ask=True, title="Labels", output=None):
    """PDF über GDI drucken. ask=True: Windows-Druckdialog (Drucker, Eigenschaften, Exemplare).
    Jede Seite wird in Druckerauflösung gerendert und formatfüllend, zentriert auf den bedruckbaren Bereich
    gesetzt. Rückgabe: Name des verwendeten Druckers. Abbruch im Dialog -> PrintCancelled.
    output: in Datei drucken (nur für Tests mit „Microsoft Print to PDF“)."""
    import pypdfium2 as pdfium
    k32, gdi, cdlg = _gdi()
    pd = _PRINTDLGW(lStructSize=ctypes.sizeof(_PRINTDLGW), hwndOwner=owner_hwnd)
    name, hdc = printer, None
    if not ask and printer:  # ohne Dialog auf den gespeicherten Drucker
        gdi.CreateDCW.argtypes, gdi.CreateDCW.restype = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                                         ctypes.c_void_p], ctypes.c_void_p
        hdc = gdi.CreateDCW(None, printer, None, None)
        if not hdc:
            return print_pdf(pdf, owner_hwnd, printer, True, title, output)  # Drucker weg -> doch fragen
    else:
        # PD_RETURNDC | PD_NOSELECTION | PD_NOPAGENUMS | PD_USEDEVMODECOPIESANDCOLLATE
        pd.Flags = 0x100 | 0x4 | 0x8 | 0x40000
        if not ask:
            pd.Flags |= 0x400  # PD_RETURNDEFAULT: Standarddrucker ohne Dialog
        elif printer:
            pd.hDevNames = _devnames(k32, printer)
        if not cdlg.PrintDlgW(ctypes.byref(pd)):
            err = cdlg.CommDlgExtendedError()
            if err == 0x1008 and printer:  # PDERR_PRINTERNOTFOUND: gespeicherter Drucker existiert nicht mehr
                return print_pdf(pdf, owner_hwnd, "", True, title, output)
            if err:
                raise RuntimeError(f"Druckdialog-Fehler {err:#x}")
            raise PrintCancelled()
        hdc = pd.hDC
    try:
        if pd.hDevNames:
            p = k32.GlobalLock(pd.hDevNames)
            name = ctypes.wstring_at(p + 2 * ctypes.c_uint16.from_address(p + 2).value)
            k32.GlobalUnlock(pd.hDevNames)
        if not hdc:
            raise RuntimeError("Der Drucker konnte nicht geöffnet werden.")
        try:
            hres, vres = gdi.GetDeviceCaps(hdc, 8), gdi.GetDeviceCaps(hdc, 10)   # HORZRES/VERTRES (bedruckbar)
            dpi_x, dpi_y = gdi.GetDeviceCaps(hdc, 88), gdi.GetDeviceCaps(hdc, 90)
            doc_name = ctypes.create_unicode_buffer(title)

            class DOCINFOW(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_int), ("lpszDocName", ctypes.c_wchar_p), ("lpszOutput", ctypes.c_wchar_p),
                            ("lpszDatatype", ctypes.c_wchar_p), ("fwType", ctypes.c_uint32)]

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                            ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                            ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                            ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                            ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

            di = DOCINFOW(ctypes.sizeof(DOCINFOW), doc_name.value, output, None, 0)
            if gdi.StartDocW(hdc, ctypes.byref(di)) <= 0:
                raise RuntimeError("Druckauftrag konnte nicht gestartet werden.")
            doc = pdfium.PdfDocument(pdf)
            try:
                for page in doc:
                    w_pt, h_pt = page.get_size()
                    # Pixel je Punkt so wählen, dass die Seite den bedruckbaren Bereich ausfüllt (Seitenverhältnis
                    # bleibt); bei ungleicher Auflösung X/Y streckt StretchDIBits die Höhe passend
                    s = min(hres / w_pt, vres / h_pt * dpi_x / dpi_y)
                    bm = page.render(scale=s, prefer_bgrx=True)
                    bw, bh = bm.width, bm.height
                    dw, dh = bw, round(bh * dpi_y / dpi_x)
                    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), bw, -bh, 1, 32, 0, 0, 0, 0, 0, 0)
                    gdi.StartPage(hdc)
                    gdi.StretchDIBits(hdc, (hres - dw) // 2, (vres - dh) // 2, dw, dh, 0, 0, bw, bh,
                                      ctypes.cast(bm.buffer, ctypes.c_void_p), ctypes.byref(bih), 0, 0x00CC0020)
                    gdi.EndPage(hdc)
                    bm.close()
                    page.close()
            finally:
                doc.close()
            gdi.EndDoc(hdc)
        finally:
            gdi.DeleteDC(hdc)
    finally:
        for h in (pd.hDevMode, pd.hDevNames):
            if h:
                k32.GlobalFree(h)
    return name


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

LOCAL_ONLY = ("selected", "pick", "version", "updated_by", "updated_at", "local_msg")  # wird nicht geteilt
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
    ausgeblendet/<ref>.json  gelöscht/erledigt (erledigte mit Auftragsdaten)  labels/*.pdf  protokoll/<benutzer>.csv"""

    def __init__(self, root, user):
        self.root, self.user = root, user
        self.dirs = {k: os.path.join(root, k) for k in ("auftraege", "sperren", "ausgeblendet", "labels", "protokoll")}
        for d in self.dirs.values():
            os.makedirs(d, exist_ok=True)
        self.mutex = threading.Lock()
        self._cache = {}  # Pfad -> ((mtime_ns, Größe), Inhalt)

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

    def _read_many(self, kind):
        """Alle JSON-Dateien eines Unterordners lesen. Auf Netzlaufwerken dauert jedes Öffnen ~0,3 s (Latenz),
        daher parallel und nur geänderte Dateien (Größe/Änderungszeit) erneut lesen."""
        from concurrent.futures import ThreadPoolExecutor
        with os.scandir(self.dirs[kind]) as it:
            entries = [(e.name, e.path, e.stat()) for e in it if e.name.endswith(".json")]
        cache, result, todo = self._cache, {}, []
        for name, path, st in entries:
            hit = cache.get(path)
            if hit and hit[0] == (st.st_mtime_ns, st.st_size):
                result[name] = hit[1]
            else:
                todo.append((name, path, (st.st_mtime_ns, st.st_size)))
        if todo:
            with ThreadPoolExecutor(min(32, len(todo))) as ex:
                for (name, path, stamp), data in zip(todo, ex.map(lambda t: self._read(t[1]), todo)):
                    result[name] = data
                    if data is not None:
                        cache[path] = (stamp, data)
        return result

    def load_all(self):
        orders = {}
        for name, rec in self._read_many("auftraege").items():
            # Konfliktkopien von Sync-Diensten (z. B. „17588-PC01.json“) ignorieren
            if not rec or self._safe(rec.get("ref", "")) + ".json" != name:
                continue
            if rec["ref"] not in orders or rec["version"] > orders[rec["ref"]]["version"]:
                orders[rec["ref"]] = rec
        orders = list(orders.values())
        orders.sort(key=lambda r: r.get("created", ""))
        hidden = {"deleted": {}, "completed": {}}
        for name, h in self._read_many("ausgeblendet").items():
            h = h or {}
            hidden.setdefault(h.get("kind", "deleted"), {})[h.get("ref", name[:-5])] = h.get("at", "")
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

    def hide(self, ref, kind, data=None):
        rec = {"ref": ref, "kind": kind, "by": self.user, "at": dt.datetime.now().isoformat(timespec="seconds")}
        if data:
            rec["data"] = data
        with self.mutex, self._locked(ref):
            self._write(self._path("ausgeblendet", ref), rec)
            try:
                os.remove(self._path("auftraege", ref))
            except FileNotFoundError:
                pass

    def archived(self, ref):
        """Auftragsdaten eines erledigten Auftrags (seit 1.0.19 gespeichert), sonst leer."""
        return self._read(self._path("ausgeblendet", ref)).get("data") or {}

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


UPDATE_ZIP = "BOB-Sendcloud-Versand.zip"  # Ordner-Variante: exe + _internal (startet ohne Entpacken)


def is_onefile():
    """True bei der alten Ein-Datei-Variante, die sich bei jedem Start in einen Temp-Ordner entpackt."""
    return getattr(sys, "frozen", False) and os.path.normcase(os.path.dirname(getattr(sys, "_MEIPASS", ""))) \
        != os.path.normcase(os.path.dirname(sys.executable))


def check_update(repo):
    """Neueste Version auf GitHub abfragen. Rückgabe: dict oder None (kein Update).
    Läuft noch die Ein-Datei-Variante und gibt es das ZIP, wird auch bei gleicher Version die Umstellung
    auf die Ordner-Variante angeboten."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": f"BOB-Sendcloud-Versand/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.loads(r.read())
    if rel.get("draft") or rel.get("prerelease"):
        return None
    assets = {a.get("name"): a for a in rel.get("assets", [])}
    newer = _vtuple(rel.get("tag_name")) > _vtuple(APP_VERSION)
    migrate = is_onefile() and UPDATE_ZIP in assets and _vtuple(rel.get("tag_name")) >= _vtuple(APP_VERSION)
    if not (newer or migrate):
        return None
    if UPDATE_ZIP in assets:
        asset = assets[UPDATE_ZIP]
    elif is_onefile() or not getattr(sys, "frozen", False):
        asset = assets.get(UPDATE_ASSET)
    else:
        asset = None  # Ordner-Variante lässt sich nicht mit einer einzelnen exe aktualisieren
    if not asset:
        return None
    return {"version": rel["tag_name"].lstrip("vV"), "notes": (rel.get("body") or "").strip(),
            "url": asset["browser_download_url"], "size": asset.get("size"),
            "sha256": (asset.get("digest") or "").removeprefix("sha256:"), "page": rel.get("html_url", ""),
            "kind": "zip" if asset["name"] == UPDATE_ZIP else "exe", "migrate": not newer}


def _download(info, path, progress=None):
    h, done = hashlib.sha256(), 0
    req = urllib.request.Request(info["url"], headers={"User-Agent": f"BOB-Sendcloud-Versand/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and info.get("size"):
                progress(done / info["size"])
    if info.get("size") and done != info["size"]:
        raise RuntimeError("Download unvollständig.")
    if info.get("sha256") and h.hexdigest() != info["sha256"].lower():
        raise RuntimeError("Prüfsumme stimmt nicht – Update verworfen.")


def _lp(path):
    """Pfad mit \\\\?\\-Präfix: erlaubt mehr als 260 Zeichen (Bibliotheken im Paket haben tiefe Unterordner)."""
    path = os.path.abspath(path)
    return path if path.startswith("\\\\?\\") else "\\\\?\\" + path


def install_update(info, progress=None):
    """Update laden. exe (alte Variante): laufende exe umbenennen und ersetzen, Rückgabe der exe.
    zip (Ordner-Variante): nach <Programmordner>\\_update\\neu entpacken, Rückgabe der neuen exe dort –
    sie übernimmt nach dem Beenden dieses Prozesses die eigentliche Installation (run_installer)."""
    import shutil
    import zipfile
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Updates funktionieren nur in der exe-Version.")
    exe = sys.executable
    if info.get("kind") == "zip":
        stage = _lp(os.path.join(os.path.dirname(exe), "_update"))
        shutil.rmtree(stage, ignore_errors=True)
        new_dir = os.path.join(stage, "neu")
        os.makedirs(new_dir)
        pkg = os.path.join(stage, "paket.zip")
        _download(info, pkg, progress)
        with zipfile.ZipFile(pkg) as z:
            root = os.path.realpath(new_dir)
            for name in z.namelist():  # keine Pfade außerhalb des Zielordners
                if not os.path.realpath(os.path.join(new_dir, name)).startswith(root + os.sep):
                    raise RuntimeError(f"Ungültiger Pfad im Update-Paket: {name}")
            z.extractall(new_dir)
        os.remove(pkg)
        new_exe = os.path.join(new_dir[4:], UPDATE_ASSET)  # ohne \\?\ – wird als normaler Programmpfad gestartet
        internal = os.path.join(new_dir, "_internal")
        if not (os.path.isfile(new_exe) and os.path.isdir(internal)
                and any(re.fullmatch(r"python3\d+\.dll", f, re.I) for f in os.listdir(internal))):
            raise RuntimeError("Update-Paket unvollständig – es wurde nichts verändert.")
        return new_exe
    new, old = exe + ".neu", exe + ".alt"
    try:
        _download(info, new, progress)
        with open(new, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("Download ist keine Windows-Programmdatei.")
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


def run_installer(target, pid):
    """Läuft als neue exe aus <target>\\_update\\neu: wartet, bis die alte Version beendet ist, sichert sie
    (*.alt), kopiert sich nach <target> und startet dort. Bei Fehlern wird die alte Version wiederhergestellt."""
    import shutil
    import subprocess
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes, k32.CloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_uint32], [ctypes.c_void_p]
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        k32.WaitForSingleObject(h, 60000)
        k32.CloseHandle(h)
    src = _lp(os.path.dirname(sys.executable))
    start_exe = os.path.join(os.path.abspath(target), UPDATE_ASSET)
    t_exe, t_int = _lp(start_exe), _lp(os.path.join(target, "_internal"))
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    moved, created = [], []
    try:
        shutil.rmtree(t_int + ".alt", ignore_errors=True)
        if os.path.exists(t_exe + ".alt"):
            os.remove(t_exe + ".alt")
        for p in (t_int, t_exe):
            if os.path.exists(p):
                for attempt in range(40):  # die alte exe kann noch kurz gesperrt sein
                    try:
                        os.replace(p, p + ".alt")
                        break
                    except PermissionError:
                        if attempt == 39:
                            raise
                        time.sleep(0.25)
                moved.append(p)
        created.append(t_int)
        shutil.copytree(os.path.join(src, "_internal"), t_int)
        created.append(t_exe)
        shutil.copy2(os.path.join(src, UPDATE_ASSET), t_exe)
    except Exception as e:
        for p in created:  # halb kopierte neue Version entfernen, alte zurückholen
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        for p in moved:
            if os.path.exists(p + ".alt"):
                os.replace(p + ".alt", p)
        reason = ("Eine Programmdatei ist noch geöffnet oder gesperrt (z. B. ein zweites Fenster des Tools oder der "
                  "Virenscanner)." if isinstance(e, PermissionError) else str(e))
        ctypes.windll.user32.MessageBoxW(None, f"Das Update konnte nicht installiert werden.\n\n{reason}\n\n"
                                               "Die bisherige Version bleibt erhalten und wird wieder gestartet. "
                                               "Das Update wird später erneut angeboten.", APP_NAME, 0x10)
        if os.path.exists(t_exe):
            subprocess.Popen([start_exe], cwd=target, env=env, close_fds=True)
        return 1
    subprocess.Popen([start_exe], cwd=target, env=env, close_fds=True)
    return 0


def cleanup_old_update():
    """Reste früherer Updates entfernen (nicht, wenn diese exe selbst der Installer in _update ist)."""
    import shutil
    if not getattr(sys, "frozen", False):
        return
    base = os.path.dirname(sys.executable)
    if os.path.basename(os.path.dirname(base)).lower() == "_update":
        return
    for p in (sys.executable + ".alt", sys.executable + ".neu"):
        try:
            os.remove(p)
        except OSError:
            pass
    for d in ("_internal.alt", "_update"):
        shutil.rmtree(_lp(os.path.join(base, d)), ignore_errors=True)


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


def return_mail(o: Order, number, files, cfg, folder):
    from email.message import EmailMessage
    m = EmailMessage()
    m["To"] = o.email
    m["Subject"] = f"Ihr Retourenlabel zu Auftrag {o.ref}"
    m["X-Unsent"] = "1"
    qr = len(files) > 1
    m.set_content(
        f"Guten Tag {o.name or o.company},\n\n"
        f"anbei erhalten Sie das DHL-Retourenlabel für Ihre Rücksendung zu Auftrag {o.ref}.\n\n"
        "Bitte drucken Sie das Label (PDF) aus und kleben Sie es gut sichtbar auf das Paket. "
        + ("Ohne Drucker können Sie stattdessen den QR-Code (Bild im Anhang) in einer DHL-Filiale oder "
           "Packstation vorzeigen – dort wird das Label für Sie gedruckt. " if qr else "")
        + f"Die Rücksendung ist für Sie kostenlos.\n\nSendungsnummer der Rücksendung: {number}\n\n"
        f"Mit freundlichen Grüßen\n{cfg['sender_name'] or ''}\n{cfg['sender_company'] or ''}".rstrip() + "\n")
    for path in files:
        with open(path, "rb") as f:
            data = f.read()
        kind = ("application", "pdf") if path.endswith(".pdf") else ("image", "png")
        m.add_attachment(data, maintype=kind[0], subtype=kind[1], filename=os.path.basename(path))
    path = os.path.join(folder, f"Retoure_{re.sub(r'[^A-Za-z0-9_-]', '', o.ref)}_{number}.eml")
    with open(path, "wb") as f:
        f.write(bytes(m))
    return path


# --------------------------------------------------------------------------- Oberfläche

COLS = [("send", "", 96), ("ref", "Auftrag", 70), ("name", "Name", 170), ("company", "Firma", 120),
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
        apply_colors(self.cfg)
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
        if self.cfg["use_history"] == "1":
            self.after(8000, self._apply_history_cached)  # Erfahrungswerte laden, Sendcloud-Import ergänzen
        self._s4t_running = False
        self.after(3000, self._s4t_tick)
        if self.cfg.carrier_missing:
            self.after(300, lambda: (messagebox.showinfo(APP_NAME, "Bitte zuerst die Zugangsdaten für den Versand in den "
                                                                    "Einstellungen hinterlegen."), self.settings()))

    def _build(self):
        apply_theme(self)
        self.configure(bg=C["hall"])

        # ── Kopfleiste (Graphit) mit Aktionen
        bar = tk.Frame(self, bg=C["steel"], padx=14, pady=10)
        bar.pack(fill="x")
        tk.Label(bar, text=APP_NAME, bg=C["steel"], fg=C["on_steel"], font=F["brand"]).pack(side="left", padx=(0, 22))
        self.btn_s4t = FlatButton(bar, "Aus S4T abholen", self.fetch_s4t, "bar")
        self.btn_s4t.pack(side="left")
        self.btn_open = FlatButton(bar, "CSV importieren…", self.open_file, "bar")
        self.btn_open.pack(side="left", padx=(6, 0))
        self.btn_new = FlatButton(bar, "Sendung erfassen…", self.new_shipment, "bar")
        self.btn_new.pack(side="left", padx=(6, 0))
        # Bearbeiten/Manuell/Löschen gibt es auch per Doppelklick, Rechtsklick und Entf – bei schmalem Fenster
        # werden sie ausgeblendet, damit Senden/Drucken/Exportieren rechts nie abgeschnitten werden
        edit_box = tk.Frame(bar, bg=C["steel"])
        tk.Frame(edit_box, bg=C["steel_line"], width=1).pack(side="left", fill="y", padx=10, pady=4)
        for text, cmd in (("Bearbeiten", self.edit_current), ("Manuell versendet…", self.mark_manual),
                          ("Löschen", self.delete_orders)):
            FlatButton(edit_box, text, cmd, "bar").pack(side="left", padx=(0, 6))

        def fit_bar():
            # nach dem Layout ausführen, sonst sind die benötigten Breiten beim Start noch nicht berechnet
            shown = edit_box.winfo_ismapped()
            show = bar.winfo_width() >= bar.winfo_reqwidth() + (0 if shown else edit_box.winfo_reqwidth())
            if show and not shown:
                edit_box.pack(side="left", fill="y", after=self.btn_new)
            elif not show and shown:
                edit_box.pack_forget()
        edit_box.pack(side="left", fill="y")
        bar.bind("<Configure>", lambda e: self.after_idle(fit_bar))
        FlatButton(bar, "Einstellungen", self.settings, "bar").pack(side="right")
        tk.Frame(bar, bg=C["steel_line"], width=1).pack(side="right", fill="y", padx=10, pady=4)
        self.btn_send = FlatButton(bar, "Ausgewählte senden", self.send_selected, "primary")
        self.btn_send.pack(side="right", padx=(8, 0))
        self.btn_export = FlatButton(bar, "Exportieren…", self.export, "bar")
        self.btn_export.pack(side="right", padx=(6, 0))
        self.btn_print = FlatButton(bar, "Labels drucken…", self.print_labels, "bar")
        self.btn_print.pack(side="right")
        FlatButton(bar, "Sendungsverfolgung", self.tracking, "bar").pack(side="right", padx=(0, 6))

        # ── Update-Hinweis (nur sichtbar, wenn eine neue Version bereitsteht)
        self.update_bar = tk.Frame(self, bg=C["signal"], padx=14, pady=7)
        self.update_text = tk.Label(self.update_bar, text="", bg=C["signal"], fg=C["ink"], font=F["body_b"])
        self.update_text.pack(side="left")
        self.update_later = tk.Label(self.update_bar, text="Später", bg=C["signal"], fg=C["ink"], font=F["body"],
                                     cursor="hand2", padx=10)
        self.update_later.pack(side="right")
        self.update_later.bind("<Button-1>", lambda e: self.update_bar.pack_forget())
        self.update_btn = tk.Label(self.update_bar, text="Jetzt aktualisieren", bg=C["steel"], fg=C["on_steel"],
                                   font=F["button"], cursor="hand2", padx=14, pady=4)
        self.update_btn.pack(side="right", padx=(0, 6))
        self.update_btn.bind("<Button-1>", lambda e: self.do_update())
        self._update_anchor = bar

        # ── Suche + Filter-Reiter mit Zählern
        sbar = tk.Frame(self, bg=C["hall"], padx=14, pady=10)
        sbar.pack(fill="x")
        box = tk.Frame(sbar, bg=C["paper"], highlightthickness=1, highlightbackground=C["line_strong"],
                       highlightcolor=C["focus"])
        box.grid(row=0, column=0, sticky="w")
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
        self.search_entry.bind("<FocusIn>", lambda e: box.configure(highlightbackground=C["focus"]), add="+")
        self.search_entry.bind("<FocusOut>", lambda e: box.configure(highlightbackground=C["line_strong"]), add="+")

        self.filter = tk.StringVar(value=FILTERS[0])
        self.tabs = FilterTabs(sbar, self.filter, lambda: self.refresh())
        sbar.columnconfigure(2, weight=1)
        self.count_label = tk.Label(sbar, text="", bg=C["hall"], fg=C["amber_ink"], font=F["small"])
        self.count_label.grid(row=0, column=3, sticky="e", padx=(0, 14))
        sel = tk.Frame(sbar, bg=C["hall"])
        sel.grid(row=0, column=4, sticky="e")
        FlatButton(sel, "Alle auswählen", lambda: self.select_all(True), "ghost").pack(side="left")
        FlatButton(sel, "Keine", lambda: self.select_all(False), "ghost").pack(side="left", padx=(2, 0))

        def place_tabs(e=None, state={"inline": None}):
            # Reiter neben der Suche, wenn alles passt (inkl. Platz für „n ausgewählt“), sonst in eigener Zeile
            need = box.winfo_reqwidth() + self.tabs.winfo_reqwidth() + sel.winfo_reqwidth() + 22 + 14 + 120
            inline = sbar.winfo_width() - 28 >= need
            if inline == state["inline"]:
                return
            state["inline"] = inline
            if inline:
                self.tabs.grid(row=0, column=1, sticky="w", padx=(22, 0), pady=0)
            else:
                self.tabs.grid(row=1, column=0, columnspan=5, sticky="w", padx=0, pady=(8, 0))
        sbar.bind("<Configure>", place_tabs)
        place_tabs()

        self.search.trace_add("write", lambda *a: self.refresh())
        self.search_entry.bind("<Escape>", lambda e: self.search.set(""))
        self.search_entry.bind("<Return>", lambda e: self._focus_first())
        self.search_entry.bind("<Down>", lambda e: self._focus_first())
        self.bind_all("<Control-f>", lambda e: (self.search_entry.focus_set(), self.search_entry.select_range(0, "end")))

        # ── Tabelle
        frm = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        frm.pack(fill="both", expand=True, padx=14)
        # Spalte #0 (Baumspalte) trägt das Auswahl-Icon – nur sie kann Bilder anzeigen
        self.icons = {k: tk.PhotoImage(data=icon_png(k)) for k in ("box", "box_on", "box_off", "done")}
        self.tree = ttk.Treeview(frm, columns=[c[0] for c in COLS], show="tree headings", selectmode="browse")
        self.tree.column("#0", width=38, minwidth=38, stretch=False, anchor="center")
        self.tree.heading("#0", image=self.icons["box"], command=self._toggle_all)
        self._sort = (None, False)  # (Spalte, absteigend) – Klick auf Überschrift: auf-, absteigend, aus
        for key, title, w in COLS:
            self.tree.heading(key, text=title, anchor="w" if key not in ("value", "weight") else "e",
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=w, minwidth=30, stretch=key in ("name", "street", "items"),
                             anchor="center" if key == "country" else
                             "e" if key in ("value", "weight") else "w")
        ys = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(frm, orient="horizontal", command=self.tree.xview)
        # Senden-Knöpfe sind echte Widgets über Spalte 1 (Treeview-Zellen können nur Text) – ihre Lage wird
        # nach Scrollen, Größenänderung, Spaltenziehen und Neuaufbau nachgeführt
        self._send_btns, self._send_job = [], None
        self.tree.configure(yscrollcommand=lambda *a: (ys.set(*a), self._place_send_buttons_later()),
                            xscrollcommand=lambda *a: (xs.set(*a), self._place_send_buttons_later()))
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
                       and self.tree.identify_column(e.x) not in ("#0", "#1") else None)
        self.tree.bind("<Motion>", self._hover)
        for ev in ("<Configure>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.tree.bind(ev, lambda e: self._place_send_buttons_later(), add="+")
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
        self.det_send = FlatButton(head, "Diesen Auftrag senden",
                                   lambda: self.tree.focus() and self.send_one(self.orders[int(self.tree.focus())]),
                                   "primary")
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

        # ── Statuszeile mit kompakter Farblegende (bei schmalem Fenster in eigener Zeile darunter)
        sb = tk.Frame(self, bg=C["hall"], padx=14, pady=8)
        sb.pack(side="bottom", fill="x", before=frm)  # vor der Tabelle: bleibt auch bei wenig Höhe sichtbar
        sb.columnconfigure(1, weight=1)
        self.status = tk.StringVar(value="Keine Datei geladen. Mit „Importieren…“ einen S4T-Export laden.")
        self.share_label = tk.Label(sb, text="", bg=C["hall"], fg=C["muted"], font=F["body_b"])
        self.share_label.grid(row=0, column=0, sticky="w", padx=(0, 16))
        tk.Label(sb, textvariable=self.status, bg=C["hall"], fg=C["muted"], font=F["small"],
                 anchor="w").grid(row=0, column=1, sticky="ew")
        self.progress = ttk.Progressbar(sb, length=180, mode="determinate", style="Signal.Horizontal.TProgressbar")
        self.progress.grid(row=0, column=3, sticky="e")
        legend = tk.Frame(sb, bg=C["hall"])
        tk.Label(legend, text="Farben:", bg=C["hall"], fg=C["muted"], font=F["small"]).pack(side="left", padx=(0, 6))
        for bg, fg, txt in ((C["amber_bg"], C["amber_ink"], "prüfen"), (C["red_bg"], C["red_ink"], "nicht sendbar"),
                            (C["green_bg"], C["green_ink"], "Label erstellt"),
                            (C["blue_bg"], C["blue_ink"], "war schon in Sendcloud"),
                            (C["violet_bg"], C["violet_ink"], "manuell versendet")):
            tk.Label(legend, text=txt, bg=bg, fg=fg, font=F["small"], padx=6).pack(side="left", padx=(0, 4))

        def place_legend(e=None, state={"inline": None}):
            # Platz für Freigabe-Info, Legende, Fortschritt und mind. 420 px Statustext
            need = (self.share_label.winfo_reqwidth() + legend.winfo_reqwidth() + self.progress.winfo_reqwidth()
                    + 420 + 60)
            inline = sb.winfo_width() >= need
            if inline == state["inline"]:
                return
            state["inline"] = inline
            if inline:
                legend.grid(row=0, column=2, sticky="e", padx=(18, 18), pady=0)
            else:
                legend.grid(row=1, column=0, columnspan=4, sticky="w", padx=0, pady=(6, 0))
        sb.bind("<Configure>", place_legend)
        place_legend()

    # ----- Tabelle
    @staticmethod
    def _can_send(o):
        return not (o.done or o.tracking or o.busy_elsewhere or o.status == "wird gesendet…" or o.validate())

    def _box_icon(self, o):
        if o.done:
            return self.icons["done"]
        if o.busy_elsewhere or o.tracking:
            return self.icons["box_off"]
        return self.icons["box_on" if o.selected else "box"]

    def _row_values(self, o):
        return ("", o.ref, o.name, o.company, o.street, o.house, o.zip, o.city,
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
        rows = list(enumerate(self.orders))
        if self._sort[0]:
            keyed = [(self._sort_key(o, self._sort[0]), i, o) for i, o in rows]
            filled = sorted((k for k in keyed if not k[0][0]), key=lambda k: k[0], reverse=self._sort[1])
            rows = [(i, o) for _, i, o in filled] + [(i, o) for k, i, o in keyed if k[0]]  # Leeres immer unten
        for i, o in rows:
            if self._visible(o):
                self.tree.insert("", "end", iid=str(i), image=self._box_icon(o), values=self._row_values(o),
                                 tags=(self._tag(o),))
                shown += 1
        if keep and self.tree.exists(keep):
            self.tree.focus(keep)
            self.tree.selection_set(keep)
            self.tree.see(keep)
        self._place_send_buttons_later()
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
        self._update_head_box()
        self._show_details()
        self._update_status()

    @staticmethod
    def _auto_selectable(o):
        """Wird von „Alle auswählen“ erfasst (fehlerhafte und zu prüfende Aufträge nicht)."""
        return not (o.done or o.tracking or o.validate() or o.needs_review or o.busy_elsewhere)

    def _head_on(self):
        shown = [self.orders[int(i)] for i in self.tree.get_children()]
        eligible = [o for o in shown if self._auto_selectable(o)]
        return bool(eligible) and all(o.selected for o in eligible)

    def _sort_by(self, key):
        col, desc = self._sort
        self._sort = (key, False) if col != key else (key, True) if not desc else (None, False)
        for k, title, _ in COLS:
            arrow = "" if k != self._sort[0] else " ▼" if self._sort[1] else " ▲"
            self.tree.heading(k, text=title + arrow)
        self.refresh()

    def _sort_key(self, o, key):
        """Sortierwert je Spalte: Zahlen numerisch, Text ohne Groß-/Kleinschreibung, Leeres immer zuletzt."""
        num = {"value": o.value, "weight": o.weight * o.parcels,
               "ref": int(o.ref) if o.ref.isdigit() else None,
               "send": 0 if self._can_send(o) else 1}
        if key in num:
            v = num[key]
            return (v is None, v if v is not None else 0, o.ref)
        text = {"label": o.printed or ("bereit" if o.labels else ""), "items": o.items_text}.get(key, getattr(o, key, ""))
        text = str(text or "").strip().lower()
        return (text == "", text, o.ref)

    def _update_head_box(self):
        """Kopf der Auswahlspalte: angehakt, wenn alle angezeigten auswählbaren Aufträge ausgewählt sind."""
        self.tree.heading("#0", image=self.icons["box_on" if self._head_on() else "box"])

    def _toggle_all(self):
        self.select_all(not self._head_on())

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
            self.tree.item(str(i), image=self._box_icon(o), values=self._row_values(o), tags=(self._tag(o),))
            self._update_head_box()
            self._place_send_buttons_later()

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
        region, col, iid = self.tree.identify_region(e.x, e.y), self.tree.identify_column(e.x), self.tree.identify_row(e.y)
        if region in ("tree", "cell") and col == "#0" and iid:
            self._toggle(iid)
            return "break"
        if region == "cell" and col == "#1" and iid and self._can_send(self.orders[int(iid)]):
            self._send_row(iid)
            return "break"

    def _send_row(self, iid):
        self.tree.focus(iid)
        self.tree.selection_set(iid)
        self.send_one(self.orders[int(iid)])

    def _place_send_buttons_later(self):
        if not self._send_job:
            self._send_job = self.after_idle(self._place_send_buttons)

    def _place_send_buttons(self):
        self._send_job = None
        spots = []
        for iid in self.tree.get_children():
            box = self.tree.bbox(iid, "send")
            if box and box[2] > 40 and self._can_send(self.orders[int(iid)]):
                spots.append((iid, box))
        while len(self._send_btns) < len(spots):
            self._send_btns.append(RowButton(self.tree, self._send_row))
        for b, (iid, (x, y, w, h)) in zip(self._send_btns, spots):
            b.iid = iid
            b.place(x=x + 7, y=y + 4, width=w - 14, height=h - 8)
        for b in self._send_btns[len(spots):]:
            b.place_forget()

    def _hover(self, e):
        col, iid = self.tree.identify_column(e.x), self.tree.identify_row(e.y)
        hand = bool(iid) and (col == "#0" or (col == "#1" and self._can_send(self.orders[int(iid)])))
        self.tree.configure(cursor="hand2" if hand else "")

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
        o.pick = self._next_pick() if o.selected else 0
        self._update_row(int(iid))
        self._update_status()
        self.save_session()

    def _next_pick(self):
        return max((o.pick for o in self.orders), default=0) + 1

    def select_all(self, on):
        if self.busy:
            return
        visible = [o for o in self.orders if self._visible(o)]  # bei aktiver Suche nur die angezeigten
        for o in visible:
            was = o.selected
            o.selected = on and self._auto_selectable(o)
            if o.selected and not was:
                o.pick = self._next_pick()
            elif not o.selected and not o.done:
                o.pick = 0
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
            self.det_send.pack_forget()
            for c in self.cards.values():
                c.show([])
            return
        o = self.orders[int(iid)]
        err = o.validate()
        if self._can_send(o):
            self.det_send.pack(side="right")
            self.det_send.state(["disabled"] if self.busy else ["!disabled"])
        else:
            self.det_send.pack_forget()

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
            badge = ("bereit zum Senden", C["ink"], C["hall"])
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
        if o.s4t_id:
            if o.s4t_reported:
                s4t = (f"als versendet gemeldet {o.s4t_reported[8:10]}.{o.s4t_reported[5:7]}. {o.s4t_reported[11:16]}", "ok")
            elif o.s4t_error:
                s4t = ("Meldung fehlgeschlagen", "warn")
            else:
                s4t = ("wird beim Senden gemeldet" if self.cfg["s4t_report"] == "1" and not o.tracking
                       else "nicht gemeldet", "muted")
            ship += [("\nS4T\t", "muted"), s4t]
        if o.ebay:
            ship += [("\neBay\t", "muted"), (o.ebay, "small")]
        if o.note:
            ship += [("\nNotiz\t", "muted"), (o.note, "")]
        if BACKEND is not None and o.updated_by:
            when = o.updated_at[8:10] + "." + o.updated_at[5:7] + ". " + o.updated_at[11:16] if o.updated_at else ""
            ship += [("\nGeändert\t", "muted"), (f"{o.updated_by}, {when}", "small")]
        self.cards["ship"].show(ship)

        # Artikel
        items = []
        for n, i in enumerate(o.items):
            items += [(("\n\n" if n else "") + f'{i["qty"]:g} × {i["sku"] or i["name"]}', "b"),
                      (f'   {fmt_eur(i["price"])}', "muted")] + ([("\n" + i["name"], "small")] if i["sku"] else [])
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
        if o.s4t_error and not o.s4t_reported:
            hints.append(("✖  Speed4Trade: " + o.s4t_error + " – per Rechtsklick erneut melden", "err"))
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
        self._add_orders(new, path, f"Import aus {os.path.basename(path)}")

    def fetch_s4t(self):
        """Offene Aufträge (Status „bezahlt, Versand offen“) direkt aus Speed4Trade abholen."""
        if self.busy:
            return
        if not self.cfg.s4t_ready:
            messagebox.showinfo(APP_NAME, "Die Speed4Trade-Anbindung ist noch nicht eingerichtet.")
            self.settings("Speed4Trade")
            if not self.cfg.s4t_ready:
                return
        self.busy = True
        for b in (self.btn_send, self.btn_open, self.btn_new, self.btn_export, self.btn_s4t):
            b.state(["disabled"])
        self.status.set("Hole offene Aufträge aus Speed4Trade…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)

        def run():
            try:
                s4t = S4T(self.cfg)
                orders = s4t.fetch_orders(_num(self.cfg["default_weight_kg"]) or 2.01)
                self.q.put(("s4t", (orders, s4t.pickups)))
            except Exception as e:
                self.q.put(("s4t", e))
        threading.Thread(target=run, daemon=True).start()

    def _s4t_fetched(self, result):
        self.busy = False
        for b in (self.btn_send, self.btn_open, self.btn_new, self.btn_export, self.btn_s4t):
            b.state(["!disabled"])
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if isinstance(result, Exception):
            self._update_status("Abholen aus Speed4Trade fehlgeschlagen.")
            messagebox.showerror(APP_NAME, f"Abholen aus Speed4Trade fehlgeschlagen:\n\n{result}")
            return
        result, pickups = result
        removed = self._prune_s4t({o.ref for o in result}, pickups)
        if not result:
            self._update_status("Speed4Trade: keine neuen Aufträge."
                                + (f" {removed} Aufträge entfernt." if removed else ""))
            tag = self.cfg["s4t_tag"].strip()
            messagebox.showinfo(APP_NAME, "In Speed4Trade sind keine offenen Aufträge"
                                + (f" mit dem Kennzeichen „{tag}“." if tag else "."))
            return
        self._add_orders(result, "Speed4Trade", "Abholung aus Speed4Trade")
        if removed:
            self._update_status(f"{removed} Aufträge entfernt, die in Speed4Trade nicht mehr offen sind.")

    def _s4t_tick(self):
        """Beim Start und danach alle 5 Minuten offene Aufträge aus Speed4Trade im Hintergrund abgleichen."""
        if self.cfg.s4t_ready and not self.busy and not self._s4t_running:
            self._s4t_running = True

            def run():
                try:
                    s4t = S4T(self.cfg)
                    orders = s4t.fetch_orders(_num(self.cfg["default_weight_kg"]) or 2.01)
                    self.q.put(("s4t_auto", (orders, s4t.pickups)))
                except Exception as e:
                    self.q.put(("s4t_auto", e))
            threading.Thread(target=run, daemon=True).start()
        self.after(S4T_INTERVAL_MS, self._s4t_tick)

    def _s4t_auto(self, result):
        self._s4t_running = False
        when = dt.datetime.now().strftime("%H:%M")
        if isinstance(result, Exception):
            self._update_status(f"Speed4Trade-Abgleich {when} fehlgeschlagen: {result}")
            return
        if self.busy:
            return  # während Senden/Drucken nichts umbauen – nächster Durchlauf in 5 Minuten
        result, pickups = result
        added = self._add_orders(result, "Speed4Trade", "Speed4Trade-Abgleich", quiet=True)
        removed = self._prune_s4t({o.ref for o in result}, pickups)
        self._update_status(f"Speed4Trade abgeglichen {when}: {added} neu, {removed} entfernt, "
                            f"{len(result)} offen.")

    def _prune_s4t(self, open_refs, pickups=()):
        """Aufträge aus Speed4Trade, die dort nicht mehr offen sind (z. B. von Hand versendet oder storniert) oder
        Selbstabholer sind und hier noch unbearbeitet sind, aus der Liste nehmen. Begonnene oder versendete
        Aufträge bleiben stehen."""
        stale = [o for o in self.orders if o.s4t_id and o.ref not in open_refs and o.status == "neu"
                 and not o.tracking and not o.selected and not o.busy_elsewhere]
        if not stale:
            return 0
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        removed = []
        for o in stale:
            if BACKEND is not None:
                try:
                    BACKEND.hide(o.ref, "completed")
                except (OSError, Conflict):
                    continue
            self.completed[o.ref] = stamp
            self._synced.pop(o.ref, None)
            append_log(o, "Selbstabholung, aus Liste entfernt" if o.ref in pickups
                       else "in Speed4Trade nicht mehr offen, aus Liste entfernt")
            removed.append(o)
        self.orders = [o for o in self.orders if o not in removed]
        self.tree.delete(*self.tree.get_children())
        self.refresh()
        self.save_session()
        return len(removed)

    def _add_orders(self, new, source, title, quiet=False):
        """Neue Aufträge der Liste hinzufügen (CSV-Import oder Speed4Trade). quiet: automatischer Abgleich im
        Hintergrund – keine Rückfragen und Meldefenster, nur Statuszeile."""
        # Früher gelöschte Aufträge nicht ungefragt wieder aufnehmen
        hidden = [o.ref for o in new if o.ref in self.deleted]
        if hidden and quiet:
            new = [o for o in new if o.ref not in self.deleted]
        elif hidden:
            if messagebox.askyesno(APP_NAME, f"{len(hidden)} dieser Aufträge wurden früher aus der Liste "
                                             "gelöscht:\n" + ", ".join(hidden[:25]) +
                                   "\n\nWieder anzeigen? (Nein = weiterhin ausblenden)"):
                for ref in hidden:
                    self.deleted.pop(ref, None)
                    if BACKEND is not None:
                        BACKEND.unhide(ref)
            else:
                new = [o for o in new if o.ref not in self.deleted]
        if BACKEND is not None:
            return self._import_shared(source, new, title, quiet)
        existing = {o.ref for o in self.orders}
        if quiet and all(o.ref in existing or o.ref in self.completed for o in new):
            if self._link_s4t(new):
                self.save_session()
            return 0
        # Erledigte Aufträge (Label gedruckt + Sendungsnummer exportiert) aus der Liste nehmen
        finished = self._cleanup_finished(refresh=False)
        # Neue Aufträge HINZUFÜGEN: vorhandene behalten ihren Stand, erledigte kommen nicht wieder
        existing = {o.ref for o in self.orders}
        skipped_done = [o.ref for o in new if o.ref in self.completed and o.ref not in existing]
        added = [o for o in new if o.ref not in existing and o.ref not in self.completed]
        already = sum(o.ref in existing for o in new)
        self.orders += added
        linked = self._link_s4t(new)
        self.source = source
        if self.cfg["use_history"] == "1":
            self._apply_history_cached()
        self.refresh()
        self._show_details()
        self.save_session()
        parts = [f"{len(added)} neue Aufträge hinzugefügt"]
        if already:
            parts.append(f"{already} bereits in der Liste" + (f" ({linked} mit Speed4Trade verknüpft)" if linked else ""))
        if skipped_done:
            parts.append(f"{len(skipped_done)} schon erledigt (übersprungen)")
        if finished:
            parts.append(f"{len(finished)} erledigte entfernt")
        self._update_status(f"{title}: " + ", ".join(parts) + ".")
        if not quiet:
            messagebox.showinfo(APP_NAME, f"{title}:\n\n• " + "\n• ".join(parts))
        return len(added)

    def _link_s4t(self, new):
        """Aufträge, die schon aus einer CSV in der Liste stehen, mit Speed4Trade verknüpfen (für die Rückmeldung)."""
        by_ref = {o.ref: o for o in new if o.s4t_id}
        n = 0
        for o in self.orders:
            src = by_ref.get(o.ref)
            if src and not o.s4t_id:
                o.s4t_id, o.ebay = src.s4t_id, o.ebay or src.ebay
                n += 1
        return n

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
                    BACKEND.hide(o.ref, "completed", shared_data(o))
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

    def _import_shared(self, source, new, title, quiet=False):
        """Import in den gemeinsamen Ordner: nur hinzufügen; Erledigte werden für alle entfernt.
        Bekannte Aufträge werden lokal aussortiert – jeder Zugriff auf ein Netzlaufwerk kostet spürbar Zeit."""
        known = {o.ref for o in self.orders}
        res = {"added": 0, "already": sum(o.ref in known for o in new),
               "completed": sum(o.ref in self.completed and o.ref not in known for o in new), "deleted": 0}
        fresh = [o for o in new if o.ref not in known and o.ref not in self.completed]
        if quiet and not fresh:
            if self._link_s4t(new):
                self.save_session()
            return 0
        self._push()
        try:
            finished = self._cleanup_finished(refresh=False)
            if self.cfg["use_history"] == "1":  # Erfahrungswerte vor dem Teilen anwenden
                try:
                    self.hist = self.hist or weight_stats(self.weights.load())
                except OSError:
                    pass
                apply_history(fresh, self.hist, _num(self.cfg["default_weight_kg"]) or 2.01)
            for o in fresh:
                res[BACKEND.add(shared_data(o))] += 1
            records, hidden = BACKEND.load_all()
        except (OSError, Conflict) as e:
            if quiet:
                self._update_status(f"{title}: gemeinsamer Ordner nicht erreichbar ({e}).")
            else:
                messagebox.showerror(APP_NAME, f"Import in den gemeinsamen Ordner fehlgeschlagen:\n{e}")
            return 0
        self.source = source
        self._sig = None
        self.tree.delete(*self.tree.get_children())
        self._merge_remote(None, records, hidden)
        linked = self._link_s4t(new)  # wird mit dem nächsten Speichern in den gemeinsamen Ordner übertragen
        self.refresh()
        self.save_session()
        parts = [f"{res['added']} neue Aufträge hinzugefügt"]
        if res["already"]:
            parts.append(f"{res['already']} bereits in der Liste"
                         + (f" ({linked} mit Speed4Trade verknüpft)" if linked else ""))
        if res["completed"]:
            parts.append(f"{res['completed']} schon erledigt (übersprungen)")
        if res["deleted"]:
            parts.append(f"{res['deleted']} früher gelöscht (ausgeblendet)")
        if finished:
            parts.append(f"{len(finished)} erledigte entfernt")
        self._update_status(f"{title}: " + ", ".join(parts) + ".")
        if not quiet:
            messagebox.showinfo(APP_NAME, f"{title} in die gemeinsame Liste:\n\n• " + "\n• ".join(parts))
        return res["added"]

    # ----- Erfahrungswerte aus früheren Sendungen
    hist = {}  # Erfahrungswerte je SKU (aus WeightStore berechnet)

    @property
    def weights(self):
        return WeightStore(BACKEND.root if BACKEND is not None else APP_DIR, USER)

    def _apply_history_cached(self):
        """Gespeicherte Gewichte sofort anwenden; Sendcloud-Werte älter als 12 h im Hintergrund ergänzen,
        solange noch Sendcloud-Zugangsdaten eingetragen sind."""
        store = self.weights
        try:
            self.hist = weight_stats(store.load())
        except OSError:
            pass
        apply_history(self.orders, self.hist, _num(self.cfg["default_weight_kg"]) or 2.01)
        age = store.sendcloud_age()
        if (age is None or age > 12 * 3600) and self.cfg.public_key and not self.busy:
            self.reload_history()

    def reload_history(self, notify=False):
        if self.busy:
            return
        self.busy = True
        for b in (self.btn_send, self.btn_open, self.btn_new, self.btn_export, self.btn_s4t):
            b.state(["disabled"])
        self.status.set("Lade Erfahrungswerte (Gewicht/Pakete) aus früheren Sendungen…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)

        def run():
            store, sc_error = self.weights, None
            try:
                if self.cfg.public_key:  # Sendcloud ergänzen; erster Import holt 3 Jahre
                    try:
                        store.put_sendcloud(Sendcloud(self.cfg).history(
                            days=120 if store.sendcloud_age() is not None else 3 * 365))
                    except SendcloudError as e:
                        sc_error = e  # gespeicherte Werte gelten trotzdem
                self.q.put(("history", (weight_stats(store.load()), notify, sc_error)))
            except Exception as e:
                self.q.put(("history", (e, notify, None)))
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

    def new_shipment(self, template=None, parent=None):
        """Sendung ohne Auftrag aus S4T/CSV von Hand erfassen (z. B. Nachlieferung, Ersatzteil an Werkstatt).
        template: früherer Auftrag, dessen Empfänger, Inhalt und Pakete vorbelegt werden (Referenz <ref>-2, -3 …).
        Sie läuft danach wie jeder andere Auftrag: Senden, Drucken, Export."""
        if self.busy:
            messagebox.showinfo(APP_NAME, "Bitte warten, bis der laufende Vorgang fertig ist.", parent=parent or self)
            return
        taken = {x.ref for x in self.orders} | set(self.completed) | set(self.deleted)
        content, price = "Ersatzteile", 0.0
        if template is None:
            o = Order(dt.datetime.now().strftime("M-%Y%m%d-%H%M%S"))
            o.country = self.cfg["sender_country"] or "DE"
            o.weight = _num(self.cfg["default_weight_kg"]) or 2.01
        else:
            base = re.sub(r"[^A-Za-z0-9_-]", "", template.ref)[:26] or "M"
            o = Order(next(f"{base}-{n}" for n in range(2, 1000) if f"{base}-{n}" not in taken))
            for k in ("name", "company", "street", "house", "addr2", "zip", "city", "country", "email", "phone",
                      "weight", "parcels"):
                setattr(o, k, getattr(template, k))
            names = [i["name"] or i["sku"] for i in template.items]
            content, price = (", ".join(n for n in names if n)[:100] or content), max(template.value, 0.0)
        o.weight_source = "manuell"
        dlg = NewShipmentDialog(parent or self, o, taken, content, price)
        if not dlg.saved:
            return
        append_log(o, "manuell erfasst" + (f" (aus Auftrag {template.ref})" if template else ""))
        self._add_orders([o], self.source, "Sendung erfasst", quiet=True)
        for i, x in enumerate(self.orders):
            if x.ref == o.ref and self.tree.exists(str(i)):
                self.tree.focus(str(i))
                self.tree.selection_set(str(i))
                self.tree.see(str(i))
                self._show_details()
                break

    def create_return(self, template, parent=None):
        """DHL-Retourenlabel für einen früheren Auftrag: Label (PDF) und QR-Code im Ordner „retouren“ ablegen,
        im Protokoll vermerken und einen E-Mail-Entwurf an den Kunden öffnen."""
        parent = parent or self
        dhl = DHL(self.cfg)
        if not self.cfg.dhl_api_key or (not dhl.sandbox and not (dhl.user and dhl.password)):
            messagebox.showinfo(APP_NAME, "Für Retourenlabels fehlen noch die DHL-Zugangsdaten (API-Key, "
                                          "GKP-Benutzer, Passwort).", parent=parent)
            self.settings("DHL")
            return
        o = Order(template.ref)
        for k in ("name", "company", "street", "house", "addr2", "zip", "city", "country", "email", "phone"):
            setattr(o, k, getattr(template, k))
        dlg = ReturnDialog(parent, o, template.weight or _num(self.cfg["default_weight_kg"]) or 2.01,
                           max(template.value, 0.0))
        if not dlg.saved:
            return
        parent.configure(cursor="watch")
        parent.update()
        try:
            number, pdf, qr = dhl.create_return(o, dlg.weight, dlg.value, dlg.reference)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Retourenlabel konnte nicht erstellt werden:\n\n{e}", parent=parent)
            return
        finally:
            parent.configure(cursor="")
        folder = os.path.join(BACKEND.root if BACKEND is not None else APP_DIR, "retouren")
        os.makedirs(folder, exist_ok=True)
        base = os.path.join(folder, f"Retoure_{re.sub(r'[^A-Za-z0-9_-]', '', o.ref)}_{number}")
        files = [base + ".pdf"] + ([base + "_QR.png"] if qr else [])
        for path, data in zip(files, (pdf, qr)):
            with open(path, "wb") as f:
                f.write(data)
        o.tracking, o.parcel_id = number, DHL_RETURN_PREFIX + number
        append_log(o, "Retourenlabel erstellt" + (" (Testumgebung)" if dhl.sandbox else ""))
        if dlg.mail and o.email:
            try:
                os.startfile(return_mail(o, number, files, self.cfg, folder))
                return
            except OSError as e:
                messagebox.showwarning(APP_NAME, f"E-Mail-Entwurf konnte nicht geöffnet werden:\n{e}\n\n"
                                                 "Das Label wird stattdessen geöffnet.", parent=parent)
        os.startfile(files[0])

    def archived_order(self, ref):
        """Früheren Auftrag für „Neue Sendung daraus“ finden: aktuelle Liste, gemeinsamer Ordner (erledigte
        Aufträge ab 1.0.19), sonst Speed4Trade über die Auftragsnummer. None, wenn nichts gefunden wurde."""
        for o in self.orders:
            if o.ref == ref:
                return o
        if BACKEND is not None:
            try:
                data = BACKEND.archived(ref)
            except OSError:
                data = {}
            if data.get("street"):
                return Order.from_dict(data)
        if self.cfg.s4t_ready:
            s4t = S4T(self.cfg)
            resp = s4t._call("fetchOrders", '<request method="fetchOrders" version="1.0.0"><filter>'
                                            f'<order_number filter_method="=" filter_value="{xml_escape(ref)}"/>'
                                            '</filter><use_modification_context_filter>false'
                                            '</use_modification_context_filter></request>')
            x = resp.find("order")
            if x is not None:
                return s4t_order(x, _num(self.cfg["default_weight_kg"]) or 2.01)
        return None

    def settings(self, page="Allgemein"):
        SettingsDialog(self, self.cfg, page)
        if getattr(self, "restart_pending", False):  # neue Farben/gemeinsamer Ordner, erst nach dem Fenster
            self.restart_pending = False
            self.restart()

    def restart(self):
        """Tool beenden und neu starten (z. B. für ein anderes Farbschema)."""
        if self.busy:
            messagebox.showinfo(APP_NAME, "Es läuft gerade ein Vorgang. Die Änderungen werden nach dem nächsten "
                                          "Start wirksam.")
            return
        import subprocess
        self.save_session()
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(cmd, close_fds=True, env=env)
        self.destroy()

    def send_selected(self):
        if self.busy:
            return
        sel = sorted((o for o in self.orders if o.selected and not o.tracking and not o.busy_elsewhere),
                     key=lambda o: o.pick or float("inf"))
        if not sel:
            messagebox.showinfo(APP_NAME, "Keine Aufträge ausgewählt (Häkchen in der ersten Spalte).")
            return
        self._send(sel)

    def tracking(self):
        """Fenster mit allen Sendungen aus dem Protokoll und ihrem DHL-Status; nur einmal offen."""
        win = getattr(self, "_track_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_set()
            return
        self._track_win = TrackingWindow(self, self.cfg)

    def send_one(self, o):
        """„Senden“ direkt am Auftrag (Spalte in der Liste oder Knopf im Detailbereich)."""
        if self.busy or not self._can_send(o):
            return
        self._send([o])

    def _send(self, sel):
        bad = [o for o in sel if o.validate()]
        if bad:
            messagebox.showwarning(APP_NAME, "Diese Aufträge sind nicht sendbar und werden übersprungen:\n\n" +
                                   "\n".join(f"{o.ref}: {'; '.join(o.validate())}" for o in bad[:15]))
            sel = [o for o in sel if not o.validate()]
            if not sel:
                return
        if self.cfg.carrier_missing:
            messagebox.showerror(APP_NAME, "Zugangsdaten fehlen – bitte unter Einstellungen eintragen:\n\n• "
                                 + "\n• ".join(self.cfg.carrier_missing))
            return
        dhl = self.cfg["carrier"] == "dhl"
        missing = [label for k, label in REQUIRED_SENDER
                   if not self.cfg[k].strip() and not (dhl and k == "contract_id")]
        if missing:
            messagebox.showerror(APP_NAME, "Absenderdaten fehlen – bitte unter Einstellungen eintragen:\n\n• "
                                 + "\n• ".join(missing))
            self.settings()
            return
        high = [o for o in sel if o.value / o.parcels > 500]
        n_parcels = sum(o.parcels for o in sel)
        via = ("direkt bei DHL" + (" (TESTUMGEBUNG)" if self.cfg["dhl_sandbox"] == "1" else "")) if dhl else "bei Sendcloud"
        msg = (f"{len(sel)} Auftrag/Aufträge mit insgesamt {n_parcels} Paket(en) {via} anlegen und bei DHL "
               f"anmelden?\n\nGesamtgewicht {sum(o.weight * o.parcels for o in sel):.2f} kg. "
               f"Die Labels sind kostenpflichtig.").replace(".", ",", 1)
        if high:
            msg += (f"\n\nHinweis: {len(high)} Sendung(en) über 500 € Warenwert (DHL-Standardhaftung): "
                    + ", ".join(o.ref for o in high[:10]))
        if not messagebox.askyesno(APP_NAME, msg):
            return
        self.busy = True
        for b in (self.btn_send, self.btn_open, self.btn_new, self.btn_export, self.btn_s4t):
            b.state(["disabled"])
        self.progress.configure(maximum=len(sel), value=0)
        threading.Thread(target=self._worker, args=(sel,), daemon=True).start()

    def _fetch_labels(self, sc, o, parcels):
        """Label-PDFs zu (tracking, parcel_id)-Paaren herunterladen und im Programm ablegen."""
        for tracking, pid in parcels:
            try:
                store_label(o, tracking, (sc or label_client(self.cfg, pid)).label_pdf(pid), self.cfg)
            except Exception as e:
                o.hints.append(f"Label-PDF für {tracking} nicht geladen: {e}")

    def _report_s4t(self, o):
        """Versand mit Sendungsnummer an Speed4Trade melden. Fehler werden am Auftrag vermerkt, nicht geworfen."""
        try:
            warnings = S4T(self.cfg).report_shipped(o)
            o.s4t_reported, o.s4t_error = dt.datetime.now().isoformat(timespec="minutes"), ""
            o.exported = True  # Sendungsnummer ist in Speed4Trade – kein CSV-Export mehr nötig
            append_log(o, "Versand an Speed4Trade gemeldet" + (f" (Hinweis: {'; '.join(warnings)})" if warnings else ""))
            return True
        except Exception as e:
            o.s4t_error = str(e)
            append_log(o, f"Meldung an Speed4Trade fehlgeschlagen: {e}")
            return False

    def report_s4t_current(self):
        iid = self.tree.focus()
        if not iid or self.busy:
            return
        o = self.orders[int(iid)]
        if not (o.s4t_id and o.tracking and not o.s4t_reported):
            return
        if not messagebox.askyesno(APP_NAME, f"Auftrag {o.ref} in Speed4Trade als versendet melden?\n\n"
                                             f"Sendungsnummer(n): {o.tracking}"):
            return
        self.configure(cursor="watch")
        self.update()
        try:
            ok = self._report_s4t(o)
        finally:
            self.configure(cursor="")
        self._after_change(iid)
        if ok:
            self._cleanup_finished()
        else:
            messagebox.showerror(APP_NAME, f"Meldung an Speed4Trade fehlgeschlagen:\n\n{o.s4t_error}")

    def _worker(self, sel):
        sc = carrier(self.cfg)
        where = "bei DHL" if isinstance(sc, DHL) else "in Sendcloud"
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
                ex = sc.existing(o) if isinstance(sc, DHL) else sc.existing(o.ref)
                if ex:
                    o.tracking = ", ".join(p["tracking_number"] for p in ex)
                    o.parcel_id = ", ".join(str(p["id"]) for p in ex)
                    o.status = "bereits vorhanden"
                    if len(ex) < o.parcels:
                        o.hints.append(f"Achtung: {where} nur {len(ex)} von {o.parcels} Paketen vorhanden – "
                                       "fehlende bitte manuell anlegen")
                    append_log(o, f"bereits {where} vorhanden, nicht neu angelegt")
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
                    try:
                        self.weights.add(o)  # eigene Erfahrungswerte, unabhängig vom Versandweg
                    except OSError:
                        pass
                o.selected = False
                if o.s4t_id and self.cfg["s4t_report"] == "1" and not o.s4t_reported:
                    self._report_s4t(o)
            except Exception as e:
                o.status, o.error = "Fehler", str(e)
                if o.tracking:  # Mehrpaket-Sendung nur teilweise angelegt
                    o.status = "teilweise"
                    o.error = f"Nur {len(o.tracking.split(', '))} von {o.parcels} Paketen angelegt: {e}"
            o.locked_by = o.locked_at = ""  # Reservierung freigeben (wird mit dem Ergebnis gespeichert)
            self.q.put(("row", i))
            self.q.put(("progress", n))
        self.q.put(("done", sel))

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
                elif kind == "s4t":
                    self._s4t_fetched(val)
                elif kind == "s4t_auto":
                    self._s4t_auto(val)
                elif kind == "update":
                    self._show_update(*val)
                elif kind == "update_progress":
                    self.update_text.configure(text=f"Update wird geladen … {val:.0%}")
                elif kind == "update_done":
                    if self._update_finished(*val):
                        return  # Fenster ist zu, kein weiteres after()
                elif kind == "history":
                    data, notify, sc_error = val
                    self.busy = False
                    for b in (self.btn_send, self.btn_open, self.btn_new, self.btn_export, self.btn_s4t):
                        b.state(["!disabled"])
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    if isinstance(data, Exception):
                        self._update_status(f"Erfahrungswerte nicht geladen: {data}")
                        if notify:
                            messagebox.showerror(APP_NAME, f"Erfahrungswerte konnten nicht geladen werden:\n{data}")
                    else:
                        self.hist = data
                        n = apply_history(self.orders, data, _num(self.cfg["default_weight_kg"]) or 2.01) if self.cfg["use_history"] == "1" else 0
                        self.save_session()
                        self.refresh()
                        self._show_details()
                        note = f" Sendcloud nicht erreichbar ({sc_error}), gespeicherte Werte genutzt." if sc_error else ""
                        self._update_status(f"Erfahrungswerte: {len(data)} Artikel bekannt, {n} Aufträge angepasst.{note}")
                        if notify:
                            messagebox.showinfo(APP_NAME, f"Erfahrungswerte für {len(data)} Artikel geladen.\n"
                                                          f"{n} Aufträge der aktuellen Liste angepasst." + note)
                elif kind == "progress":
                    self.progress.configure(value=val)
                    self._update_status()
                elif kind == "done":
                    self.busy = False
                    for b in (self.btn_send, self.btn_open, self.btn_new, self.btn_export, self.btn_s4t):
                        b.state(["!disabled"])
                    self.save_session()
                    run = val  # nur die Aufträge dieses Durchlaufs zählen, nicht ältere Ergebnisse in der Liste
                    ok = sum(o.status == "Label erstellt" for o in run)
                    ex = sum(o.status == "bereits vorhanden" for o in run)
                    err = [o for o in run if o.status in ("Fehler", "teilweise")]
                    skipped = [o for o in run if o.local_msg]
                    s4t_bad = [o for o in run if o.s4t_error and o.tracking and not o.s4t_reported]
                    self._update_status()
                    messagebox.showinfo(APP_NAME, f"Fertig.\n\nLabels erstellt: {ok}\nBereits vorhanden: {ex}\n"
                                                  f"Fehler: {len(err)}" + ("\n\n" + "\n".join(
                                                      f"{o.ref}: {o.error[:150]}" for o in err[:8]) if err else "")
                                        + ("\n\nÜbersprungen (Kollegen):\n" + "\n".join(
                                            f"{o.ref}: {o.local_msg}" for o in skipped[:8]) if skipped else "")
                                        + ("\n\nNicht an Speed4Trade gemeldet (Rechtsklick → nachholen):\n" + "\n".join(
                                            f"{o.ref}: {o.s4t_error[:150]}" for o in s4t_bad[:8]) if s4t_bad else ""))
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
        self.update_text.configure(text="Schnellere Programmversion verfügbar (einmalige Umstellung)"
                                   if info.get("migrate") else
                                   f"Neue Version {info['version']} verfügbar (installiert: {APP_VERSION})")
        self.update_bar.pack(fill="x", after=self._update_anchor)

    def do_update(self):
        info = getattr(self, "update_info", None)
        if not info:
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "Bitte warten, bis der laufende Vorgang fertig ist.")
            return
        notes = info["notes"][:900] + ("…" if len(info["notes"]) > 900 else "")
        question = ("Auf die schnellere Programmversion umstellen?\n\nDas Tool liegt danach als Ordner vor "
                    "(exe + Unterordner „_internal“) und startet ohne Entpacken.\n\n" if info.get("migrate") else
                    f"Auf Version {info['version']} aktualisieren?\n\n" + (f"Neu:\n{notes}\n\n" if notes else ""))
        if not messagebox.askyesno(APP_NAME, question + "Das Tool startet danach neu. Deine Arbeitsliste bleibt "
                                                        "erhalten."):
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
        # Ohne Reset übernimmt die neue exe den Entpackordner der alten (PyInstaller-onefile) – mit alten
        # Modulen, und er wird beim Beenden gelöscht. Dann hängt der Neustart.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        if self.update_info.get("kind") == "zip":  # neue Version installiert sich selbst, sobald wir beendet sind
            subprocess.Popen([exe, "--install", os.path.dirname(sys.executable), str(os.getpid())],
                             cwd=os.path.dirname(exe), close_fds=True, env=env)
        else:
            subprocess.Popen([exe], close_fds=True, env=env)
        self.destroy()
        return True

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
            if k not in ("selected", "pick"):
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
        picks = {x["ref"]: x.get("pick", 0) for x in (local or {}).get("orders", [])}
        self.orders = []
        for rec in records:
            o = Order(rec["ref"])
            self._adopt(o, rec)
            o.selected = o.ref in sel and not o.done
            o.pick = picks.get(o.ref, 0)
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
        if o.s4t_id:
            m.add_command(label="An Speed4Trade als versendet melden…", command=self.report_s4t_current,
                          state="normal" if o.tracking and not o.s4t_reported else "disabled")
        m.add_command(label="Retourenlabel erstellen…", command=lambda: self.create_return(o))
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
        orders, paper, action = dlg.result
        orders = sorted(orders, key=lambda o: o.pick or float("inf"))  # in der Reihenfolge des Anhakens
        self.cfg["label_paper"] = paper
        self.cfg.save()
        self.busy = True
        self.status.set("Bereite Sammeldruck vor…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)

        def run():
            try:
                target = os.path.join(PRINT_DIR, f"sammeldruck_{dt.datetime.now():%Y%m%d_%H%M%S}.pdf")
                if paper == "A6":
                    parts = []
                    for o in orders:  # fehlende Labels nachladen
                        if not o.label_paths and o.parcel_id:
                            self._fetch_labels(None, o, zip(o.tracking.split(", "), o.parcel_id.split(", ")))
                        parts += o.label_paths
                    if not parts:
                        raise RuntimeError("Keine Label-PDFs gefunden.")
                    parts = [to_a6(open(p, "rb").read()) for p in parts]  # auch ältere, nicht umgerechnete Labels
                else:
                    if any(pid.startswith(DHL_PREFIX) for o in orders for pid in o.parcel_id.split(", ")):
                        raise RuntimeError("Labels, die direkt bei DHL erstellt wurden, bitte im Format A6 drucken.")
                    ids = [pid for o in orders for pid in o.parcel_id.split(", ") if pid.strip().isdigit()]
                    sc = Sendcloud(self.cfg)
                    if not ids:
                        raise RuntimeError("Für A4 werden Sendcloud-Paket-IDs benötigt (keine vorhanden).")
                    parts = [sc.labels_bulk(ids[k:k + 20], "A4") for k in range(0, len(ids), 20)]
                pages = merge_pdfs(parts, target)
                self.q.put(("printjob", (orders, target, pages, None, paper, action)))
            except Exception as e:
                self.q.put(("printjob", (orders, None, 0, e, paper, action)))
        threading.Thread(target=run, daemon=True).start()

    def _print_finished(self, orders, target, pages, error, paper="A6", action="pdf"):
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if error:
            messagebox.showerror(APP_NAME, f"Sammeldruck fehlgeschlagen:\n{error}")
            self._update_status()
            return
        printed = ""
        if action == "print":
            key = f"printer_{paper.lower()}"  # eigener Drucker je Format: A6 Etiketten, A4 Büro
            self.busy = True  # während des Dialogs keinen Hintergrund-Abgleich einarbeiten
            self.configure(cursor="watch")
            self.update()
            try:
                printed = print_pdf(open(target, "rb").read(), int(self.wm_frame(), 16), self.cfg[key],
                                    ask=self.cfg["print_direct"] != "1", title=os.path.basename(target))
                self.cfg[key] = printed
                self.cfg.save()
            except PrintCancelled:
                self._update_status("Druck abgebrochen – Labels bleiben als ungedruckt markiert.")
                return
            except ImportError:
                action = "pdf"  # Druckmodul fehlt (Entwicklungsumgebung) – wie bisher PDF öffnen
            except Exception as e:
                if not messagebox.askyesno(APP_NAME, f"Drucken fehlgeschlagen:\n{e}\n\nStattdessen das PDF öffnen?"):
                    return
                action = "pdf"
            finally:
                self.busy = False
                self.configure(cursor="")
        if action == "pdf":
            os.startfile(target)
        stamp = dt.datetime.now().strftime("%d.%m. %H:%M")
        for o in orders:
            o.printed = stamp
        self.refresh()
        self._show_details()
        self.save_session()
        removed = self._cleanup_finished()
        self._update_status(f"Sammeldruck: {len(orders)} Aufträge, {pages} Seite(n) – "
                            + (f"an {printed} gesendet." if printed else "PDF geöffnet, bitte drucken (Strg+P).")
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
    "signal": "#F5B700", "signal_hover": "#FFC61F", "signal_edge": "#D99F00",
    "amber_bg": "#FFF1CC", "amber_ink": "#8A5300",
    "red_bg": "#FDE7E5", "red_ink": "#A3261B",
    "green_bg": "#E3F2E8", "green_ink": "#22663F",
    "blue_bg": "#E4EDF7", "blue_ink": "#24538A",
    "violet_bg": "#EEE8F8", "violet_ink": "#5B3FA8",
}
# Farbschemata (pro Arbeitsplatz): Leiste und Akzent genügen, Hover-, Linien- und Textfarben werden abgeleitet.
# „Dunkel“ ersetzt zusätzlich die Flächen und die Statusfarben.
THEMES = {
    "Standard": {"steel": "#22303C", "signal": "#F5B700"},
    "Marine": {"steel": "#1B3A5C", "signal": "#F28C28"},
    "Waldgrün": {"steel": "#1F4034", "signal": "#E9C46A"},
    "Bordeaux": {"steel": "#4A1F2A", "signal": "#E8B04B"},
    "Hell": {"steel": "#DDE3EA", "signal": "#2F6FDE", "select": "#CFE0FA"},
    "Dunkel": {"steel": "#0D1115", "signal": "#F5B700", "select": "#2C4760",
               "hall": "#161B21", "paper": "#1F262E", "ink": "#E6EBF0", "muted": "#9AA7B4", "faint": "#6B7884",
               "line": "#2C353F", "line_strong": "#3E4955", "hover": "#29323C",
               "amber_bg": "#3A2F12", "amber_ink": "#F0C36A", "red_bg": "#3D2321", "red_ink": "#F2A49C",
               "green_bg": "#1C3325", "green_ink": "#8FD4A8", "blue_bg": "#1D2B3D", "blue_ink": "#9DC1EB",
               "violet_bg": "#2B2440", "violet_ink": "#C2B1F0"},
}
BASE_C = dict(C)
_HEX_RX = re.compile(r"#[0-9A-Fa-f]{6}")


def _mix(a, b, t):
    """Farbe a um den Anteil t in Richtung b verschieben."""
    ca, cb = (tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def _light(h):
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b > 0.6


def build_palette(theme="Standard", bar="", accent=""):
    """Vollständige Farbtabelle für ein Schema, optional mit eigener Leisten- und Akzentfarbe."""
    pal = {**BASE_C, **THEMES.get(theme, {})}
    if _HEX_RX.fullmatch(bar or ""):
        pal["steel"] = bar.upper()
        pal.pop("select", None)
    if _HEX_RX.fullmatch(accent or ""):
        pal["signal"] = accent.upper()
    # markierte Zeile: Leistenfarbe, bei heller Leiste eine helle Abstufung des Akzents
    pal.setdefault("select", _mix(pal["signal"], pal["paper"], 0.72) if _light(pal["steel"]) else pal["steel"])
    dark_ink = BASE_C["ink"]
    pal["on_steel"] = dark_ink if _light(pal["steel"]) else "#FFFFFF"
    pal["steel_text"] = _mix(pal["steel"], pal["on_steel"], 0.82)
    pal["steel_hover"] = _mix(pal["steel"], pal["on_steel"], 0.10)
    pal["steel_line"] = _mix(pal["steel"], pal["on_steel"], 0.20)
    # Knöpfe in der Kopfleiste: eigene Fläche und Rahmen, damit sie auch ohne Maus als Knöpfe erkennbar sind
    pal["steel_btn"] = _mix(pal["steel"], pal["on_steel"], 0.09)
    pal["steel_btn_hover"] = _mix(pal["steel"], pal["on_steel"], 0.20)
    pal["steel_btn_edge"] = _mix(pal["steel"], pal["on_steel"], 0.30)
    pal["on_select"] = dark_ink if _light(pal["select"]) else "#FFFFFF"
    pal["on_signal"] = dark_ink if _light(pal["signal"]) else "#FFFFFF"
    pal["signal_hover"] = _mix(pal["signal"], "#FFFFFF", 0.15)
    pal["signal_edge"] = _mix(pal["signal"], "#000000", 0.13)
    # Fokusrahmen: Leistenfarbe, solange sie sich von den Flächen abhebt, sonst Akzent
    pal["focus"] = pal["steel"] if _light(pal["paper"]) != _light(pal["steel"]) else pal["signal"]
    return pal


def apply_colors(cfg):
    C.update(build_palette(cfg["color_theme"], cfg["color_bar"], cfg["color_accent"]))


_BODY, _DIN, _MONO = "Segoe UI Variable Text", "Bahnschrift", "Cascadia Mono"
F = {
    "brand": ("Bahnschrift SemiBold", 14), "order": ("Bahnschrift SemiBold", 20), "title": (_DIN, 14),
    "tab": (_DIN, 11), "tab_count": ("Bahnschrift Light", 11), "section": ("Bahnschrift SemiBold", 11),
    "body": (_BODY, 10), "body_l": (_BODY, 11), "body_b": ("Segoe UI Variable Text Semibold", 10),
    "small": (_BODY, 9), "badge": ("Bahnschrift SemiBold", 10), "button": ("Segoe UI Variable Text Semibold", 10),
    "mono": (_MONO, 10), "head": ("Segoe UI Variable Text Semibold", 9),
    "row_button": ("Segoe UI Variable Text Semibold", 9),
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
                focuscolor=C["focus"], troughcolor=C["hall"])
    s.configure("TFrame", background=C["hall"])
    s.configure("TLabel", background=C["hall"], foreground=C["ink"])
    s.configure("TLabelframe", background=C["hall"])
    s.configure("TCheckbutton", background=C["hall"], foreground=C["ink"], indicatorbackground=C["paper"],
                indicatorforeground=C["ink"])
    s.configure("TRadiobutton", background=C["hall"], foreground=C["ink"], indicatorbackground=C["paper"],
                indicatorforeground=C["ink"])
    s.map("TCheckbutton", background=[("active", C["hall"])])
    s.map("TRadiobutton", background=[("active", C["hall"])])
    # Inhaltsflächen auf Papierweiß (Einstellungen)
    s.configure("Card.TFrame", background=C["paper"])
    s.configure("Card.TLabel", background=C["paper"], foreground=C["ink"])
    s.configure("CardMuted.TLabel", background=C["paper"], foreground=C["muted"], font=F["small"])
    s.configure("CardHead.TLabel", background=C["paper"], foreground=C["ink"], font=F["section"])
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(f"Card.{w}", background=C["paper"], foreground=C["ink"], indicatorbackground=C["paper"],
                    indicatorforeground=C["ink"])
        s.map(f"Card.{w}", background=[("active", C["paper"])])
    s.configure("TCombobox", fieldbackground=C["paper"], foreground=C["ink"], background=C["paper"],
                arrowcolor=C["muted"], bordercolor=C["line_strong"], lightcolor=C["paper"], darkcolor=C["paper"])
    s.map("TCombobox", fieldbackground=[("readonly", C["paper"])], foreground=[("readonly", C["ink"])])
    s.configure("TEntry", fieldbackground=C["paper"], bordercolor=C["line_strong"], lightcolor=C["paper"],
                foreground=C["ink"], insertcolor=C["ink"], padding=4)
    s.map("TEntry", bordercolor=[("focus", C["focus"])], lightcolor=[("focus", C["focus"])])
    s.configure("TButton", background=C["paper"], foreground=C["ink"], bordercolor=C["line_strong"],
                lightcolor=C["paper"], darkcolor=C["paper"], padding=(12, 5), font=F["button"])
    s.map("TButton", background=[("active", C["hover"]), ("pressed", C["line"])])
    s.configure("Treeview", background=C["paper"], fieldbackground=C["paper"], foreground=C["ink"],
                rowheight=30, borderwidth=0, font=F["body"])
    s.map("Treeview", background=[("selected", C["select"])], foreground=[("selected", C["on_select"])])
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
    root.option_add("*Menu.foreground", C["ink"])
    root.option_add("*Menu.activeBackground", C["select"])
    root.option_add("*Menu.activeForeground", C["on_select"])
    root.option_add("*Menu.font", F["body"])
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Text.font", F["body"])
    for w in ("Text", "Entry", "Label", "Listbox"):  # Textfarbe auch im dunklen Schema
        root.option_add(f"*{w}.foreground", C["ink"])
    for w in ("Text", "Entry"):
        root.option_add(f"*{w}.insertBackground", C["ink"])
    root.option_add("*TCombobox*Listbox.background", C["paper"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["select"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["on_select"])


def icon_png(kind, px=18):
    """Kantengeglättete Auswahl-Icons als PNG (ohne Bildbibliothek): 'box' leer, 'box_on' angehakt (Signalgelb),
    'box_off' gesperrt, 'done' grüner Haken für versendete Aufträge."""
    import zlib
    import struct
    rgb = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    k = px / 18
    c, half, rad, bw = px / 2, px / 2 - 1.5 * k, 4 * k, 1.4 * k
    check = [(4.6 * k, 9.4 * k), (7.6 * k, 12.4 * k), (13.4 * k, 5.9 * k)]
    fill, border, mark, stroke = {
        "box": (C["paper"], C["line_strong"], None, 0),
        "box_on": (C["signal"], C["signal_edge"], C["on_signal"], 1.15 * k),
        "box_off": (C["hall"], C["line"], None, 0),
        "done": (None, None, C["green_ink"], 1.35 * k),
    }[kind]

    def rrect(x, y):  # Abstand zum abgerundeten Quadrat (negativ = innen)
        qx, qy = abs(x - c) - half + rad, abs(y - c) - half + rad
        return (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - rad

    def on_check(x, y):
        for (ax, ay), (bx, by) in zip(check, check[1:]):
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 <= stroke ** 2:
                return True
        return False

    ss, raw = 4, bytearray()
    for py in range(px):
        raw.append(0)
        for px_ in range(px):
            acc = [0.0, 0.0, 0.0, 0.0]  # vormultipliziert
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px_ + (sx + .5) / ss, py + (sy + .5) / ss
                    col = None
                    d = rrect(x, y) if fill else 1
                    if d <= 0:
                        col = border if d > -bw else fill
                    if mark and on_check(x, y):
                        col = mark
                    if col:
                        r, g, b = rgb(col)
                        acc[0] += r; acc[1] += g; acc[2] += b; acc[3] += 1
            n = ss * ss
            a = acc[3] / n
            raw += bytes([round(acc[0] / acc[3]), round(acc[1] / acc[3]), round(acc[2] / acc[3]), round(a * 255)]
                         if acc[3] else [0, 0, 0, 0])

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    return base64.b64encode(png)


def center_over(win, master, size=None):
    """Dialog mittig über dem Fenster öffnen, aus dem er kommt – sonst setzt Windows ihn auf den Hauptbildschirm.
    size: (Breite, Höhe) für Fenster mit fester Größe, sonst die benötigte Größe."""
    win.withdraw()
    win.update_idletasks()
    top = master.winfo_toplevel()
    w, h = size or (win.winfo_reqwidth(), win.winfo_reqheight())
    x = top.winfo_rootx() + (top.winfo_width() - w) // 2
    y = top.winfo_rooty() + max(0, (top.winfo_height() - h) // 3)
    win.geometry(f"{w}x{h}+{x}+{y}" if size else f"+{x}+{y}")
    win.deiconify()


class RowButton(tk.Label):
    """„Senden“ in einer Tabellenzeile. Signalgelb, damit er auch auf markierten (dunklen) Zeilen sichtbar bleibt."""

    def __init__(self, tree, command):
        super().__init__(tree, text="Senden  ›", bg=C["signal"], fg=C["on_signal"], font=F["row_button"], cursor="hand2",
                         bd=0, highlightthickness=1, highlightbackground=C["signal_edge"])
        self.iid = None
        self.bind("<Enter>", lambda e: self.configure(bg=C["signal_hover"]))
        self.bind("<Leave>", lambda e: self.configure(bg=C["signal"]))
        self.bind("<Button-1>", lambda e: self.iid and command(self.iid))
        # Mausrad und Rechtsklick an die Tabelle weiterreichen, als läge der Knopf nicht darüber
        self.bind("<MouseWheel>", lambda e: tree.event_generate("<MouseWheel>", delta=e.delta,
                                                                x=e.x + self.winfo_x(), y=e.y + self.winfo_y()))
        self.bind("<Button-3>", lambda e: tree.event_generate("<Button-3>", x=e.x + self.winfo_x(),
                                                              y=e.y + self.winfo_y(), rootx=e.x_root,
                                                              rooty=e.y_root))


class FlatButton(tk.Label):
    """Flacher Button: 'bar' (auf Graphit), 'primary' (Signalgelb), 'ghost' (auf Hallengrau)."""
    @staticmethod
    def variants():
        return {
            "bar": dict(bg=C["steel_btn"], fg=C["on_steel"], hover=C["steel_btn_hover"], hfg=C["on_steel"],
                        font="button", edge=C["steel_btn_edge"]),
            "primary": dict(bg=C["signal"], fg=C["on_signal"], hover=C["signal_hover"], hfg=C["on_signal"],
                            font="button", edge=C["signal_edge"]),
            "ghost": dict(bg=C["hall"], fg=C["ink"], hover=C["hover"], hfg=C["ink"], font="body"),
        }

    def __init__(self, master, text, command, variant="ghost"):
        v = self.variants()[variant]
        edge = v.get("edge")
        super().__init__(master, text=text, bg=v["bg"], fg=v["fg"], font=F[v["font"]], cursor="hand2",
                         padx=16 if variant == "primary" else 11, pady=5 if edge else 6,
                         highlightthickness=1 if edge else 0, highlightbackground=edge or v["bg"],
                         highlightcolor=edge or v["bg"])
        self.v, self.command, self._disabled = v, command, False
        self.bind("<Enter>", lambda e: self._disabled or self.configure(bg=v["hover"], fg=v["hfg"]))
        self.bind("<Leave>", lambda e: self._disabled or self.configure(bg=v["bg"], fg=v["fg"]))
        self.bind("<Button-1>", lambda e: self._disabled or self.command())

    def state(self, flags):
        self._disabled = "disabled" in flags
        self.configure(fg=_mix(self.v["bg"], self.v["fg"], 0.45) if self._disabled else self.v["fg"], bg=self.v["bg"],
                       cursor="arrow" if self._disabled else "hand2")


class FilterTabs(tk.Frame):
    """Reiter mit Live-Zähler; der aktive trägt eine Signalgelb-Unterstreichung."""

    def __init__(self, master, var, on_change, filters=FILTERS, labels=FILTER_TABS):
        super().__init__(master, bg=C["hall"])
        self.var, self.on_change, self.tabs = var, on_change, {}
        for f in filters:
            cell = tk.Frame(self, bg=C["hall"], cursor="hand2")
            cell.pack(side="left", padx=(0, 4))
            row = tk.Frame(cell, bg=C["hall"])
            row.pack(padx=8, pady=(4, 2))
            name = tk.Label(row, text=labels[f], bg=C["hall"], font=F["tab"])
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
            if f in ("Bitte prüfen", "Nicht sendbar / Fehler", "Probleme") and f != self.var.get():
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
                          highlightbackground=C["line_strong"], highlightcolor=C["focus"])
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
        center_over(self, master)
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
        ttk.Button(b, text="Drucken…", command=lambda: self.ok("print")).pack(side="left")
        ttk.Button(b, text="Als PDF öffnen", command=lambda: self.ok("pdf")).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=(6, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        center_over(self, master)
        self.grab_set()
        self.wait_window()

    def ok(self, action="print"):
        if self.choice.get() < 0:
            messagebox.showinfo(APP_NAME, "Bitte auswählen, welche Labels gedruckt werden sollen.", parent=self)
            return
        lst = self.choices[self.choice.get()][1]
        if not lst:
            return
        self.result = (lst, self.paper.get(), action)
        self.destroy()


class EditDialog(tk.Toplevel):
    FIELDS = [("name", "Name"), ("company", "Firma"), ("street", "Straße"), ("house", "Hausnummer"),
              ("addr2", "Adresszusatz"), ("zip", "PLZ"), ("city", "Ort"), ("country", "Land (ISO)"),
              ("email", "E-Mail"), ("phone", "Telefon"), ("parcels", "Anzahl Pakete"),
              ("weight", "Gewicht je Paket (kg)")]

    def __init__(self, master, order: Order):
        super().__init__(master)
        self.o, self.saved = order, False
        self.title(self._title())
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both")
        self.vars = {}
        for r, (k, label) in enumerate(self.FIELDS):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            v = self._initial(k)
            self.vars[k] = tk.StringVar(value=(f"{v:.2f}".replace(".", ",") if isinstance(v, float) else str(v)))
            e = ttk.Entry(f, textvariable=self.vars[k], width=45)
            e.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=2)
            if r == 0:
                e.focus_set()
        self._extra(f, len(self.FIELDS))
        b = ttk.Frame(f)
        b.grid(row=len(self.FIELDS) + 1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text=self.OK_TEXT, command=self.ok).pack(side="left")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=(6, 0))
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        center_over(self, master)
        self.grab_set()
        self.wait_window()

    OK_TEXT = "Übernehmen"

    def _title(self):
        return f"Auftrag {self.o.ref} bearbeiten"

    def _initial(self, k):
        return getattr(self.o, k)

    def _extra(self, f, row):
        """Zusätzliche Elemente unter den Feldern (abgeleitete Dialoge)."""

    def ok(self):
        if self.apply():
            self.saved = True
            self.destroy()

    def apply(self):
        """Eingaben prüfen und in den Auftrag übernehmen. False: Fehler wurde angezeigt, Dialog bleibt offen."""
        w = _num(self.vars["weight"].get())
        if w is None or not (0 < w <= 31.5):
            messagebox.showerror(APP_NAME, "Gewicht muss zwischen 0 und 31,5 kg liegen.", parent=self)
            return False
        n = self.vars["parcels"].get().strip()
        if not n.isdigit() or not (1 <= int(n) <= 20):
            messagebox.showerror(APP_NAME, "Anzahl Pakete muss zwischen 1 und 20 liegen.", parent=self)
            return False
        em = self.vars["email"].get().strip()
        if em and not EMAIL_RX.match(em):
            messagebox.showerror(APP_NAME, "E-Mail-Adresse ist ungültig.", parent=self)
            return False
        for k, _ in self.FIELDS:
            if k in ("parcels", "ref") or not hasattr(self.o, k):
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
        return True


class NewShipmentDialog(EditDialog):
    """Sendung von Hand erfassen: Referenz, Empfänger, Inhalt, Warenwert, Pakete und Gewicht."""
    FIELDS = ([("ref", "Referenz")] + EditDialog.FIELDS[:10] +
              [("content", "Inhalt"), ("price", "Warenwert (€)")] + EditDialog.FIELDS[10:])
    OK_TEXT = "Sendung anlegen"

    def __init__(self, master, order: Order, taken, content="Ersatzteile", price=0.0):
        self.taken, self.preset = taken, {"content": content, "price": price}
        super().__init__(master, order)

    def _title(self):
        return "Sendung manuell erfassen"

    def _initial(self, k):
        return self.preset[k] if k in self.preset else getattr(self.o, k)

    def ok(self):
        ref = self.vars["ref"].get().strip()
        # Referenz wird Dateiname im gemeinsamen Ordner und Sendungsreferenz bei Sendcloud/DHL
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,29}", ref):
            messagebox.showerror(APP_NAME, "Referenz: 1–30 Zeichen, nur Buchstaben, Ziffern, - und _.", parent=self)
            return
        if ref in self.taken:
            messagebox.showerror(APP_NAME, f"Die Referenz {ref} ist schon vergeben (in der Liste oder bereits "
                                           "erledigt). Bitte eine andere wählen.", parent=self)
            return
        price = _num(self.vars["price"].get() or "0")
        if price is None or price < 0:
            messagebox.showerror(APP_NAME, "Warenwert ist ungültig.", parent=self)
            return
        content = self.vars["content"].get().strip()
        if not content:
            messagebox.showerror(APP_NAME, "Bitte den Inhalt angeben.", parent=self)
            return
        if not self.apply():
            return
        self.o.ref = ref
        self.o.items = [{"sku": "", "name": content, "qty": 1, "price": round(price, 2), "ean": "", "weight": None}]
        errors = self.o.validate()
        if errors:
            messagebox.showerror(APP_NAME, "Bitte ergänzen:\n\n• " + "\n• ".join(errors), parent=self)
            return
        self.saved = True
        self.destroy()


class ReturnDialog(EditDialog):
    """Retourenlabel: Kundenadresse (Absender der Rücksendung) prüfen, Gewicht, Wert und Referenz."""
    FIELDS = ([("ref", "Referenz")] + [f for f in EditDialog.FIELDS if f[0] not in ("country", "parcels", "weight")]
              + [("weight", "Gewicht (kg)"), ("price", "Warenwert (€)")])
    OK_TEXT = "Retourenlabel erstellen"

    def __init__(self, master, order: Order, weight, value):
        self.preset = {"ref": f"Retoure {order.ref}", "weight": float(weight), "price": float(value)}
        self.mail_var = None
        super().__init__(master, order)

    def _title(self):
        return f"DHL-Retourenlabel für Auftrag {self.o.ref}"

    def _initial(self, k):
        return self.preset[k] if k in self.preset else getattr(self.o, k)

    def _extra(self, f, row):
        self.mail_var = tk.BooleanVar(value=bool(self.o.email))
        ttk.Checkbutton(f, text="E-Mail-Entwurf mit Label an den Kunden öffnen", variable=self.mail_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        if self.o.country and self.o.country != "DE":
            ttk.Label(f, text=f"Achtung: Kunde in {self.o.country} – Retouren sind nur aus Deutschland eingerichtet.",
                      foreground=C["amber_ink"]).grid(row=row, column=0, columnspan=2, sticky="e")

    def ok(self):
        v = {k: self.vars[k].get().strip() for k, _ in self.FIELDS}
        w, value = _num(v["weight"]), _num(v["price"] or "0")
        miss = [label for k, label in (("name", "Name"), ("street", "Straße"), ("zip", "PLZ"), ("city", "Ort"))
                if not v[k]]
        if miss:
            messagebox.showerror(APP_NAME, "Bitte ergänzen: " + ", ".join(miss), parent=self)
            return
        if not re.fullmatch(r"\d{5}", v["zip"]):
            messagebox.showerror(APP_NAME, "Retouren sind nur aus Deutschland möglich – PLZ muss 5-stellig sein.",
                                 parent=self)
            return
        if w is None or not (0 < w <= 31.5):
            messagebox.showerror(APP_NAME, "Gewicht muss zwischen 0 und 31,5 kg liegen.", parent=self)
            return
        if value is None or value < 0:
            messagebox.showerror(APP_NAME, "Warenwert ist ungültig.", parent=self)
            return
        if v["email"] and not EMAIL_RX.match(v["email"]):
            messagebox.showerror(APP_NAME, "E-Mail-Adresse ist ungültig.", parent=self)
            return
        for k in ("name", "company", "street", "house", "addr2", "zip", "city", "email", "phone"):
            setattr(self.o, k, v[k])
        self.o.country = "DE"
        self.weight, self.value, self.reference = w, value, v["ref"] or f"Retoure {self.o.ref}"
        self.mail = self.mail_var.get()
        self.saved = True
        self.destroy()


SETTINGS_PAGES = ["Allgemein", "Absender", "Versand", "Sendcloud", "DHL", "Speed4Trade", "Arbeitsplätze",
                  "Darstellung"]


class SettingsDialog(tk.Toplevel):
    """Alle Einstellungen in einem Fenster mit Reitern, gespeichert wird gemeinsam."""
    PRINT_FORMATS = ("910-300-400", "910-300-300-oz", "910-300-300", "910-300-600", "910-300-700-oz", "910-300-700", "A4")
    SIZE = (920, 660)

    def __init__(self, master, cfg: Config, page="Allgemein"):
        super().__init__(master)
        self.cfg = cfg
        self.title("Einstellungen")
        self.resizable(False, False)
        self.transient(master)
        self.configure(bg=C["hall"])
        self.vars, self.flags = {}, {}
        head = tk.Frame(self, bg=C["hall"])
        head.pack(fill="x", padx=24, pady=(18, 0))
        tk.Label(head, text="Einstellungen", bg=C["hall"], fg=C["ink"], font=F["title"]).pack(side="left")
        tk.Label(head, text=f"{APP_NAME} · Version {APP_VERSION}", bg=C["hall"], fg=C["muted"],
                 font=F["small"]).pack(side="right")
        self.page = tk.StringVar(value=page if page in SETTINGS_PAGES else SETTINGS_PAGES[0])
        self.tabs = FilterTabs(self, self.page, self._show, SETTINGS_PAGES, {p: p for p in SETTINGS_PAGES})
        self.tabs.pack(fill="x", padx=16, pady=(12, 0))
        foot = tk.Frame(self, bg=C["hall"])
        foot.pack(side="bottom", fill="x", padx=24, pady=12)
        FlatButton(foot, "Speichern", self.ok, "primary").pack(side="right")
        FlatButton(foot, "Abbrechen", self.destroy).pack(side="right", padx=(0, 8))
        tk.Frame(self, height=1, bg=C["line"]).pack(side="bottom", fill="x")
        tk.Frame(self, height=1, bg=C["line"]).pack(fill="x")
        body = tk.Frame(self, bg=C["paper"])
        body.pack(fill="both", expand=True)
        self.pages = {}
        for name, build in zip(SETTINGS_PAGES, (self._p_general, self._p_sender, self._p_shipping, self._p_sendcloud,
                                                self._p_dhl, self._p_s4t, self._p_network, self._p_colors)):
            pg = ttk.Frame(body, style="Card.TFrame", padding=(28, 18, 28, 12))
            pg.place(x=0, y=0, relwidth=1, relheight=1)
            build(pg)
            self.pages[name] = pg
        self._show()
        self.bind("<Escape>", lambda e: self.destroy())
        center_over(self, master, self.SIZE)
        self.grab_set()
        self.wait_window()

    # ----- Bausteine
    def _show(self):
        self.pages[self.page.get()].tkraise()

    def _var(self, key, default=None):
        self.vars[key] = tk.StringVar(value=self.cfg[key] or (default or ""))
        return self.vars[key]

    def _head(self, parent, text, hint=None, action=None, top=14):
        """Abschnitt mit Überschrift, optional Erklärung und Knopf rechts; liefert das Raster für die Felder."""
        h = ttk.Frame(parent, style="Card.TFrame")
        h.pack(fill="x", pady=(top, 0))
        ttk.Label(h, text=text, style="CardHead.TLabel").pack(side="left", anchor="s")
        if action:
            ttk.Button(h, text=action[0], command=action[1]).pack(side="right")
        if hint:
            ttk.Label(parent, text=hint, style="CardMuted.TLabel", wraplength=840,
                      justify="left").pack(anchor="w", pady=(2, 0), fill="x")
        g = ttk.Frame(parent, style="Card.TFrame")
        g.pack(fill="x", pady=(6, 0))
        return g

    def _row(self, g, label, var, show="", width=40, note=None, lw=24):
        r = g.grid_size()[1]
        ttk.Label(g, text=label, style="Card.TLabel", width=lw).grid(row=r, column=0, sticky="w", pady=3)
        cell = ttk.Frame(g, style="Card.TFrame")
        cell.grid(row=r, column=1, sticky="w")
        e = ttk.Entry(cell, textvariable=var, width=width, show=show)
        e.pack(side="left")
        if note:
            ttk.Label(cell, text=note, style="CardMuted.TLabel").pack(side="left", padx=(10, 0))
        return e

    def _check(self, parent, key, text, **grid):
        self.flags[key] = tk.BooleanVar(value=self.cfg[key] == "1")
        cb = ttk.Checkbutton(parent, text=text, variable=self.flags[key], style="Card.TCheckbutton")
        cb.grid(row=parent.grid_size()[1], column=0, columnspan=3, sticky="w", pady=3, **grid)
        return cb

    def _folder(self, g, var, width=60):
        r = g.grid_size()[1]
        f = ttk.Frame(g, style="Card.TFrame")
        f.grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Entry(f, textvariable=var, width=width).pack(side="left")
        def pick():
            d = filedialog.askdirectory(parent=self)
            if d:
                var.set(os.path.normpath(d))
        ttk.Button(f, text="Durchsuchen…", command=pick).pack(side="left", padx=(6, 0))

    def _cards(self, parent, var, options, on_pick=None):
        """Auswahl als Kacheln: options = [(Wert, Titel, Beschreibung)]."""
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        tiles = {}

        def paint():
            for v, (tile, ws) in tiles.items():
                on = v == var.get()
                tile.configure(highlightbackground=C["signal"] if on else C["line"],
                               highlightcolor=C["signal"] if on else C["line"])

        def pick(v):
            var.set(v)
            paint()
            on_pick and on_pick()
        for i, (value, title, desc) in enumerate(options):
            tile = tk.Frame(row, bg=C["paper"], highlightthickness=2, cursor="hand2", padx=14, pady=10)
            tile.grid(row=0, column=i, sticky="nsew", padx=(0, 12))
            row.columnconfigure(i, weight=1, uniform="cards")
            t = tk.Label(tile, text=title, bg=C["paper"], fg=C["ink"], font=F["body_b"], anchor="w")
            t.pack(fill="x")
            d = tk.Label(tile, text=desc, bg=C["paper"], fg=C["muted"], font=F["small"], anchor="w", justify="left",
                         wraplength=320)
            d.pack(fill="x", pady=(2, 0))
            for w in (tile, t, d):
                w.bind("<Button-1>", lambda e, v=value: pick(v))
            tiles[value] = (tile, (t, d))
        paint()
        return row

    def _scratch(self):
        """Kopie der Einstellungen mit den Eingaben, zum Testen ohne zu speichern."""
        tmp = Config.__new__(Config)
        tmp.cp = configparser.ConfigParser()
        tmp.cp.read_dict(self.cfg.cp)
        self._apply(tmp)
        return tmp

    def _busy_test(self, fn, ok_text):
        self.configure(cursor="watch")
        self.update()
        try:
            extra = fn()
            messagebox.showinfo(APP_NAME, ok_text + (extra or ""), parent=self)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
        finally:
            self.configure(cursor="")

    # ----- Reiter
    def _p_general(self, pg):
        g = self._head(pg, "Drucken", top=0)
        self._check(g, "print_direct", "Ohne Druckdialog direkt auf den zuletzt gewählten Drucker drucken")
        self._check(g, "save_label_pdf", "Label-PDFs zusätzlich in diesen Ordner kopieren:")
        self._folder(g, self._var("label_dir"))
        g = self._head(pg, "Protokoll", f"Einstellungen und Protokoll dieses Arbeitsplatzes liegen in {APP_DIR}",
                       top=18)
        ttk.Button(g, text="Protokoll öffnen", command=self.open_log).grid(row=0, column=0, sticky="w")

    def _p_sender(self, pg):
        g = self._head(pg, "Absender", "Steht als Absender auf jedem Label. Gilt für alle Arbeitsplätze.", top=0)
        layout = [(("sender_company", "Firma", 36), ("sender_name", "Name / Ansprechpartner", 36)),
                  (("sender_street", "Straße", 36), ("sender_house_number", "Hausnr.", 10)),
                  (("sender_postal_code", "PLZ", 10), ("sender_city", "Ort", 36)),
                  (("sender_country", "Land (Ländercode, z. B. DE)", 10), None),
                  (("sender_email", "E-Mail", 36), ("sender_phone", "Telefon", 36))]
        for r, pair in enumerate(layout):
            for c, item in enumerate(pair):
                if not item:
                    continue
                key, label, width = item
                ttk.Label(g, text=label, style="CardMuted.TLabel").grid(row=2 * r, column=c, sticky="w",
                                                                        pady=(10 if r else 4, 2), padx=(0, 24))
                ttk.Entry(g, textvariable=self._var(key), width=width).grid(row=2 * r + 1, column=c, sticky="w",
                                                                            padx=(0, 24))

    def _p_shipping(self, pg):
        self._head(pg, "Versandweg", "Über welchen Dienst die Labels erstellt werden. Gilt für alle Arbeitsplätze.",
                   top=0).destroy()
        self.carrier = tk.StringVar(value=self.cfg["carrier"])
        self._cards(pg, self.carrier, [
            ("sendcloud", "Sendcloud", "Labels über das Sendcloud-Konto, Inland und EU-Ausland. "
                                       "Zugang im Reiter „Sendcloud“."),
            ("dhl", "DHL direkt", "Über den eigenen DHL-Geschäftskundenvertrag, nur Inland. Zugang im Reiter „DHL“.")])
        g = self._head(pg, "Gewicht", "Gilt, wenn ein Auftrag kein Gewicht mitbringt.", top=20)
        self._row(g, "Standardgewicht", self._var("default_weight_kg"), width=8, note="kg")
        self._check(g, "use_history", "Fehlende Gewichte und Paketanzahl aus früheren Sendungen übernehmen")
        ttk.Button(g, text="Erfahrungswerte jetzt neu laden", command=self.reload_hist).grid(
            row=g.grid_size()[1], column=0, columnspan=3, sticky="w", pady=(2, 0), padx=(24, 0))

    def _p_sendcloud(self, pg):
        g = self._head(pg, "Zugang", "Public und Secret Key aus dem Sendcloud-Konto. Die Schlüssel bleiben "
                                     "verschlüsselt auf diesem PC.", action=("Verbindung testen", self.test_sendcloud),
                       top=0)
        self.pub = tk.StringVar(value=self.cfg.public_key)
        self.sec = tk.StringVar(value=self.cfg.secret_key)
        self._row(g, "Public Key", self.pub, width=52)
        self._row(g, "Secret Key", self.sec, show="•", width=52)
        g = self._head(pg, "Versandarten & Vertrag", "Gelten für alle Arbeitsplätze.", top=18)
        self._row(g, "Versandart Inland", self._var("shipping_option_de"), width=52)
        self._row(g, "Versandart EU-Ausland", self._var("shipping_option_eu"), width=52)
        self._row(g, "DHL-Vertrags-ID", self._var("contract_id"), width=14)
        self._row(g, "Brand-ID", self._var("brand_id"), width=14)

    def _p_dhl(self, pg):
        g = self._head(pg, "Zugang Geschäftskunden",
                       "API-Key und Secret stammen aus der App im DHL Developer Portal (developer.dhl.com), Benutzer "
                       "und Passwort aus dem DHL-Geschäftskundenportal (am besten ein Systembenutzer). Die "
                       "Abrechnungsnummer ist 14-stellig: EKP + 01 (DHL Paket) + Teilnahme.",
                       action=("Verbindung testen", self.test_dhl), top=0)
        self.dhl_key, self.dhl_secret, self.dhl_pw = (tk.StringVar(value=v) for v in
                                                      (self.cfg.dhl_api_key, self.cfg.dhl_api_secret,
                                                       self.cfg.dhl_password))
        self._row(g, "API-Key", self.dhl_key, show="•")
        self._row(g, "API-Secret (optional)", self.dhl_secret, show="•")
        self._row(g, "GKP-Benutzer", self._var("dhl_user"))
        self._row(g, "Passwort", self.dhl_pw, show="•")
        self._row(g, "Abrechnungsnummer", self._var("dhl_billing"), width=20)
        # Retoure: eigene Abrechnungsnummer (Verfahren 07); DHL erwartet beim Label die zugehörige Empfänger-ID
        self._row(g, "Retouren-Abrechnungsnr.", self._var("dhl_return_billing"), width=20, note="Verfahren 07")
        e = self._row(g, "Retouren-Empfänger-ID", self._var("dhl_return_receiver"), width=20)
        ttk.Button(e.master, text="Bei DHL abrufen", command=self.fetch_return_receiver).pack(side="left", padx=(10, 0))
        self._row(g, "Gruppenprofil", self._var("dhl_profile"))
        cols = ttk.Frame(pg, style="Card.TFrame")
        cols.pack(fill="x", pady=(6, 0))
        left, right = (ttk.Frame(cols, style="Card.TFrame") for _ in range(2))
        left.grid(row=0, column=0, sticky="nw", padx=(0, 28))
        right.grid(row=0, column=1, sticky="nw")
        for side in (left, right):
            side.configure(width=370)
        g = self._head(left, "Sendungsverfolgung", "Leer lassen, wenn der GKP-Benutzer das Recht "
                                                   "„Sendungsverfolgung“ hat.")
        self.track_pw = tk.StringVar(value=self.cfg.dhl_track_password)
        self._row(g, "Benutzer", self._var("dhl_track_user"), width=24, lw=12)
        self._row(g, "Passwort", self.track_pw, show="•", width=24, lw=12)
        g = self._head(right, "Label", "910-300-400 = 100 × 150 mm für Etikettendrucker (Zebra), "
                                       "910-300-300 = A6 für Laserdrucker, „-oz“ = ohne Abziehcodes.")
        self.vars["dhl_print_format"] = tk.StringVar(value=self.cfg["dhl_print_format"] or "910-300-400")
        ttk.Label(g, text="Labelformat", style="Card.TLabel", width=12).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Combobox(g, textvariable=self.vars["dhl_print_format"], values=self.PRINT_FORMATS, width=16,
                     state="readonly").grid(row=0, column=1, sticky="w")
        self._check(g, "dhl_sandbox", "Testumgebung (Sandbox), keine echten Labels")
        for side in (left, right):  # Erklärtexte auf die Spaltenbreite umbrechen
            for w in side.winfo_children():
                if isinstance(w, ttk.Label) and str(w.cget("style")) == "CardMuted.TLabel":
                    w.configure(wraplength=360)

    def _p_s4t(self, pg):
        g = self._head(pg, "Verbindung", "Webservice von Speed4Trade CONNECT. Passwort und Benutzer bleiben auf "
                                         "diesem PC.", action=("Verbindung testen", self.test_s4t), top=0)
        self.s4t_pw = tk.StringVar(value=self.cfg.s4t_password)
        self._row(g, "Server (Adresse/IP)", self._var("s4t_host"))
        self._row(g, "Webservice-Benutzer", self._var("s4t_user"))
        self._row(g, "Passwort", self.s4t_pw, show="•")
        self._row(g, "Mandant", self._var("s4t_mandator", "1"), width=8)
        self.pin = self.cfg["s4t_cert_sha256"].strip().upper()
        self.pin_label = ttk.Label(g, text="", style="CardMuted.TLabel")
        self.pin_label.grid(row=g.grid_size()[1], column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._show_pin()
        g = self._head(pg, "Aufträge abholen",
                       "Abgeholt werden alle Aufträge im Auftragsstatus (130 = bezahlt, Versand offen), dazu "
                       "Shop-Aufträge in den weiteren Status (Komma-getrennt, z. B. 240), solange sie noch nicht "
                       "versendet sind. Optional lässt sich auf ein Auftragskennzeichen einschränken.", top=18)
        self._row(g, "Auftragsstatus-ID", self._var("s4t_status_id", "130"), width=8)
        self._row(g, "Weitere Status", self._var("s4t_status_extra"), width=20, note="z. B. 240")
        self._row(g, "Nur mit Kennzeichen", self._var("s4t_tag"), note="optional")
        self._check(g, "s4t_report", "Nach dem Senden Sendungsnummer an Speed4Trade melden (Status „versendet“)",
                    )

    def _p_network(self, pg):
        g = self._head(pg, "Gemeinsamer Ordner", "Für mehrere Arbeitsplätze: Netzwerkordner, auf den alle schreiben "
                                                 "dürfen (z. B. \\\\server\\freigabe\\Versand). Leer = nur lokal. "
                                                 "Nach einer Änderung startet das Tool neu.", top=0)
        self.shared_dir = tk.StringVar(value=self.cfg["shared_dir"])
        self._folder(g, self.shared_dir)
        g = self._head(pg, "Updates", f"Installiert ist Version {APP_VERSION}. Neue Versionen werden aus diesem "
                                      "GitHub-Repository geholt.", action=("Jetzt nach Updates suchen", self._check_now),
                       top=18)
        self.update_repo = tk.StringVar(value=self.cfg["update_repo"] or UPDATE_REPO)
        self._row(g, "Repository (besitzer/repo)", self.update_repo, width=40)

    def _p_colors(self, pg):
        self._head(pg, "Farbschema", "Gilt nur für diesen Arbeitsplatz. Zum Übernehmen startet das Tool kurz neu.",
                   top=0).destroy()
        self.theme = tk.StringVar(value=self.cfg["color_theme"] if self.cfg["color_theme"] in THEMES else "Standard")
        row = ttk.Frame(pg, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self.theme_tiles = {}
        for i, name in enumerate(THEMES):
            pal = build_palette(name)
            tile = tk.Frame(row, bg=C["paper"], highlightthickness=2, cursor="hand2", padx=6, pady=6)
            tile.grid(row=0, column=i, padx=(0, 10))
            sw = tk.Canvas(tile, width=92, height=40, highlightthickness=0, bg=pal["hall"], cursor="hand2")
            sw.create_rectangle(0, 0, 93, 13, fill=pal["steel"], width=0)
            sw.create_rectangle(62, 3, 88, 10, fill=pal["signal"], width=0)
            sw.create_rectangle(6, 19, 86, 27, fill=pal["paper"], width=0)
            sw.create_rectangle(6, 30, 86, 38, fill=pal["select"], width=0)
            sw.pack()
            lab = tk.Label(tile, text=name, bg=C["paper"], fg=C["ink"], font=F["small"], cursor="hand2")
            lab.pack(pady=(4, 0))
            for w in (tile, sw, lab):
                w.bind("<Button-1>", lambda e, n=name: (self.theme.set(n), self._preview()))
            self.theme_tiles[name] = tile
        cols = ttk.Frame(pg, style="Card.TFrame")
        cols.pack(fill="x", pady=(4, 0))
        left = ttk.Frame(cols, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nw", padx=(0, 28))
        right = ttk.Frame(cols, style="Card.TFrame")
        right.grid(row=0, column=1, sticky="nwe")
        cols.columnconfigure(1, weight=1)
        g = self._head(left, "Eigene Farben", top=18)
        self.custom = {"color_bar": tk.StringVar(value=self.cfg["color_bar"]),
                       "color_accent": tk.StringVar(value=self.cfg["color_accent"])}
        self.swatches = {}
        for key, label in (("color_bar", "Leiste oben"), ("color_accent", "Akzent (Knöpfe, Auswahl)")):
            line = ttk.Frame(g, style="Card.TFrame")
            line.pack(anchor="w", pady=(8, 3))
            sw = tk.Label(line, width=3, highlightthickness=1, highlightbackground=C["line_strong"], cursor="hand2")
            sw.pack(side="left")
            sw.bind("<Button-1>", lambda e, k=key: self._pick(k))
            self.swatches[key] = sw
            ttk.Label(line, text=label, style="Card.TLabel").pack(side="left", padx=(8, 0))
            bf = ttk.Frame(g, style="Card.TFrame")
            bf.pack(anchor="w")
            ttk.Button(bf, text="Wählen…", command=lambda k=key: self._pick(k)).pack(side="left")
            ttk.Button(bf, text="Aus Schema", command=lambda k=key: self._reset(k)).pack(side="left", padx=(4, 0))
        self._head(right, "Vorschau", top=18).destroy()
        self.prev = tk.Frame(right, highlightthickness=1, highlightbackground=C["line"])
        self.prev.pack(fill="x", pady=(6, 0))
        self._preview()

    # ----- Darstellung
    def _palette(self):
        return build_palette(self.theme.get(), self.custom["color_bar"].get(), self.custom["color_accent"].get())

    def _pick(self, key):
        from tkinter import colorchooser
        pal = self._palette()
        rgb, hx = colorchooser.askcolor(color=pal["steel" if key == "color_bar" else "signal"], parent=self,
                                        title="Leistenfarbe wählen" if key == "color_bar" else "Akzentfarbe wählen")
        if hx:
            self.custom[key].set(hx.upper())
            self._preview()

    def _reset(self, key):
        self.custom[key].set("")
        self._preview()

    def _preview(self):
        pal = self._palette()
        for name, tile in self.theme_tiles.items():
            on = name == self.theme.get()
            tile.configure(highlightbackground=C["signal"] if on else C["paper"],
                           highlightcolor=C["signal"] if on else C["paper"])
        for key, sw in self.swatches.items():
            sw.configure(bg=pal["steel" if key == "color_bar" else "signal"])
        for w in self.prev.winfo_children():
            w.destroy()
        bar = tk.Frame(self.prev, bg=pal["steel"], padx=12, pady=8)
        bar.pack(fill="x")
        tk.Label(bar, text=APP_NAME, bg=pal["steel"], fg=pal["on_steel"], font=F["section"]).pack(side="left")
        tk.Label(bar, text="Ausgewählte senden", bg=pal["signal"], fg=pal["on_signal"], font=F["row_button"],
                 padx=10, pady=3).pack(side="right")
        body = tk.Frame(self.prev, bg=pal["hall"], padx=10, pady=8)
        body.pack(fill="x")
        for text, bg, fg in (("50100   Kunde 0   Aachen", pal["paper"], pal["ink"]),
                             ("50101   Kunde 1   (markiert)", pal["select"], pal["on_select"]),
                             ("50102   Kunde 2   versendet", pal["green_bg"], pal["green_ink"]),
                             ("50103   Kunde 3   bitte prüfen", pal["amber_bg"], pal["amber_ink"])):
            row = tk.Frame(body, bg=bg)
            row.pack(fill="x")
            tk.Label(row, text="Senden  ›", bg=pal["signal"], fg=pal["on_signal"], font=F["row_button"],
                     padx=8).pack(side="left", padx=6, pady=3)
            tk.Label(row, text=text, bg=bg, fg=fg, font=F["small"]).pack(side="left", padx=6)

    # ----- Speed4Trade-Zertifikat
    def _show_pin(self):
        self.pin_label.configure(text=f"Server-Zertifikat bestätigt: {self.pin[:8]}…{self.pin[-8:]}" if self.pin
                                 else "Server-Zertifikat noch nicht bestätigt, „Verbindung testen“ klicken.")

    def _confirm_cert(self):
        """Fingerabdruck abrufen (ohne Zugangsdaten) und bei neuem/geändertem Zertifikat bestätigen lassen."""
        host = self.vars["s4t_host"].get().strip()
        if not host:
            messagebox.showerror(APP_NAME, "Bitte die Server-Adresse eintragen.", parent=self)
            return False
        try:
            fp = s4t_fingerprint(host)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Server nicht erreichbar:\n{e}", parent=self)
            return False
        if fp == self.pin:
            return True
        pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
        msg = ("Das Zertifikat des Servers hat sich GEÄNDERT.\n\n" if self.pin else
               "Der Server nutzt ein eigenes (selbst ausgestelltes) Zertifikat.\n\n")
        msg += (f"SHA-256-Fingerabdruck:\n{pretty[:47]}\n{pretty[48:]}\n\nNur bestätigen, wenn das der "
                "Speed4Trade-Server ist (im Zweifel die IT fragen). Die Zugangsdaten werden danach nur an genau "
                "diesen Server gesendet.\n\nZertifikat vertrauen?")
        if not messagebox.askyesno(APP_NAME, msg, icon="warning" if self.pin else "question", parent=self):
            return False
        self.pin = fp
        self._show_pin()
        return True

    # ----- Tests
    def test_sendcloud(self):
        tmp = self._scratch()
        self._busy_test(lambda: Sendcloud(tmp).test(), "Verbindung zu Sendcloud erfolgreich.")

    def test_dhl(self):
        tmp = self._scratch()
        tmp["carrier"] = "dhl"
        if tmp.carrier_missing:
            messagebox.showerror(APP_NAME, "Es fehlt:\n\n• " + "\n• ".join(tmp.carrier_missing), parent=self)
            return

        def run():
            DHL(tmp).test()
            return "\n\nAchtung: Testumgebung aktiv." if tmp["dhl_sandbox"] == "1" else ""
        self._busy_test(run, "Verbindung zu DHL erfolgreich – Probe-Sendung an die eigene Adresse wurde geprüft "
                             "(nicht angelegt).")

    def fetch_return_receiver(self):
        tmp = self._scratch()
        tmp["carrier"] = "dhl"
        if tmp.carrier_missing:
            messagebox.showerror(APP_NAME, "Es fehlt:\n\n• " + "\n• ".join(tmp.carrier_missing), parent=self)
            return
        billing = self.vars["dhl_return_billing"].get().strip().replace(" ", "")
        self.configure(cursor="watch")
        self.update()
        try:
            found = DHL(tmp).return_receivers(billing)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Empfänger-IDs konnten nicht abgerufen werden:\n\n{e}\n\nAlternativ die "
                                           "Empfänger-ID im Geschäftskundenportal nachsehen (Retoure → Einstellungen) "
                                           "und von Hand eintragen.", parent=self)
            return
        finally:
            self.configure(cursor="")
        usable = [f for f in found if f[1]] or found
        if billing:
            usable = [f for f in usable if f[1].replace(" ", "") == billing] or usable
        if not usable:
            messagebox.showinfo(APP_NAME, "DHL meldet für dieses Konto keine Retouren-Empfänger"
                                + (f" zur Abrechnungsnummer {billing}" if billing else "")
                                + ". Bitte im Geschäftskundenportal unter Retoure prüfen.", parent=self)
            return
        if len(usable) == 1:
            rid, bill, desc = usable[0]
            self.vars["dhl_return_receiver"].set(rid)
            if bill and not billing:
                self.vars["dhl_return_billing"].set(bill)
            messagebox.showinfo(APP_NAME, f"Empfänger-ID „{rid}“ übernommen"
                                + (f" ({desc})" if desc else "") + ".\n\nZum Übernehmen „Speichern“ klicken.", parent=self)
            return
        lines = "\n".join(f"• {rid}" + (f"  –  Abrechnungsnr. {bill}" if bill else "") + (f"  –  {desc}" if desc else "")
                          for rid, bill, desc in usable)
        messagebox.showinfo(APP_NAME, "DHL meldet mehrere Retouren-Empfänger:\n\n" + lines +
                            "\n\nBitte die passende Empfänger-ID eintragen (oder die Abrechnungsnummer angeben und "
                            "erneut abrufen).", parent=self)

    def test_s4t(self):
        if not self._confirm_cert():
            return
        tmp = self._scratch()
        self._busy_test(lambda: "\n\nMandanten:\n" + "\n".join(f"{i}: {n}" for i, n in S4T(tmp).test()),
                        "Verbindung zu Speed4Trade erfolgreich.")

    # ----- Übernehmen
    def _apply(self, cfg):
        for k, v in self.vars.items():
            cfg[k] = v.get().strip().replace(" ", "") if k == "dhl_billing" else v.get().strip()
        for k, v in self.flags.items():
            cfg[k] = "1" if v.get() else "0"
        cfg["carrier"] = self.carrier.get()
        cfg.set_keys(self.pub.get(), self.sec.get())
        cfg.set_dhl(self.dhl_key.get(), self.dhl_secret.get(), self.dhl_pw.get())
        cfg.dhl_track_password = self.track_pw.get()
        cfg.s4t_password = self.s4t_pw.get()
        cfg["s4t_cert_sha256"] = self.pin
        cfg["shared_dir"] = self.shared_dir.get().strip()
        cfg["update_repo"] = self._repo()
        cfg["color_theme"] = self.theme.get()
        for k, v in self.custom.items():
            cfg[k] = v.get()

    def _repo(self):
        repo = self.update_repo.get().strip().strip("/")
        repo = re.sub(r"^https?://github\.com/", "", repo)  # auch komplette GitHub-Adresse akzeptieren
        return "" if repo == UPDATE_REPO else repo

    def _problem(self):
        """Erste ungültige Eingabe als (Reiter, Meldung), sonst None."""
        v = lambda k: self.vars[k].get().strip()
        if _num(v("default_weight_kg")) is None:
            return "Versand", "Standardgewicht ist keine Zahl."
        for k, name in (("contract_id", "DHL-Vertrags-ID"), ("brand_id", "Brand-ID")):
            if v(k) and not v(k).isdigit():
                return "Sendcloud", f"{name} muss eine Zahl sein."
        billing = v("dhl_billing").replace(" ", "")
        if billing and not re.fullmatch(r"\w{10}\d{2}\w{2}", billing):
            return "DHL", "Die Abrechnungsnummer muss 14 Stellen haben (EKP + 01 + Teilnahme)."
        if not (v("s4t_status_id").isdigit() and v("s4t_mandator").isdigit()):
            return "Speed4Trade", "Auftragsstatus-ID und Mandant müssen Zahlen sein."
        if not all(x.isdigit() for x in re.split(r"[,;\s]+", v("s4t_status_extra")) if x):
            return "Speed4Trade", "Weitere Status: nur Zahlen, durch Komma getrennt (z. B. 240)."
        new_shared = self.shared_dir.get().strip()
        if new_shared and new_shared != self.cfg["shared_dir"].strip():
            try:  # Schreibrecht prüfen, bevor der Ordner übernommen wird
                os.makedirs(new_shared, exist_ok=True)
                probe = os.path.join(new_shared, f".schreibtest_{USER}.tmp")
                with open(probe, "w") as fh:
                    fh.write("ok")
                os.remove(probe)
            except OSError as e:
                return "Arbeitsplätze", f"In diesen Ordner kann nicht geschrieben werden:\n{new_shared}\n\n{e}"
        return None

    def ok(self):
        problem = self._problem()
        if problem:
            self.tabs.select(problem[0])
            messagebox.showerror(APP_NAME, problem[1], parent=self)
            return False
        if self.vars["s4t_host"].get().strip() and not self.pin:
            self.tabs.select("Speed4Trade")
            if not self._confirm_cert():
                return False
        colors = any(self.cfg[k] != v for k, v in (("color_theme", self.theme.get()),
                                                   *((k, v.get()) for k, v in self.custom.items())))
        shared_changed = self.shared_dir.get().strip() != self.cfg["shared_dir"].strip()
        self._apply(self.cfg)
        self.cfg.save()
        if BACKEND is not None and not shared_changed:
            try:
                BACKEND.put_settings({k: self.cfg[k] for k in SHARED_SETTINGS})
            except OSError as e:
                messagebox.showwarning(APP_NAME, f"Einstellungen lokal gespeichert, aber nicht im gemeinsamen "
                                                 f"Ordner:\n{e}", parent=self)
        self.destroy()
        if colors or shared_changed:
            self._root().restart_pending = True  # App.settings() startet nach dem Fenster neu
            what = " und ".join(w for w, on in (("die neuen Farben", colors),
                                                ("der gemeinsame Ordner", shared_changed)) if on)
            messagebox.showinfo(APP_NAME, f"Das Tool startet jetzt kurz neu, damit {what} übernommen werden.",
                                parent=self.master)
        return True

    def _check_now(self):
        self.cfg["update_repo"] = self._repo()
        self.cfg.save()
        self.master.check_updates(manual=True)

    def reload_hist(self):
        if self.ok():
            self.master.reload_history(notify=True)

    def open_log(self):
        if BACKEND is not None:
            os.startfile(BACKEND.dirs["protokoll"])
        elif os.path.exists(LOG_PATH):
            os.startfile(LOG_PATH)
        else:
            messagebox.showinfo(APP_NAME, "Noch kein Protokoll vorhanden.", parent=self)


TRACK_FILTERS = ["Alle", "Probleme", "Unterwegs", "Zugestellt"]
TRACK_TABS = {f: f for f in TRACK_FILTERS}
TRACK_COLS = [("sent", "Versendet", 120), ("ref", "Auftrag", 70), ("name", "Empfänger", 190),
              ("tracking", "Sendungsnummer", 170), ("state", "Status", 330), ("last", "Letztes Ereignis", 120),
              ("via", "Über", 85)]


class TrackingWindow(tk.Toplevel):
    """Sendungsverfolgung aller Sendungen aus dem Versandprotokoll mit Status und Verlauf von DHL.
    Nicht modal: das Hauptfenster bleibt bedienbar."""

    def __init__(self, master, cfg: Config):
        super().__init__(master)
        self.cfg, self.q = cfg, queue.Queue()
        self.archive, self.info, self.rows, self.running = {}, {}, [], False
        self.title(f"Sendungsverfolgung – {APP_NAME}")
        self.minsize(900, 500)

        top = tk.Frame(self, bg=C["hall"], padx=14, pady=10)
        top.pack(fill="x")
        box = tk.Frame(top, bg=C["paper"], highlightthickness=1, highlightbackground=C["line_strong"],
                       highlightcolor=C["focus"])
        box.pack(side="left")
        tk.Label(box, text="⌕", bg=C["paper"], fg=C["muted"], font=(F["body"][0], 13)).pack(side="left", padx=(8, 2))
        self.search = tk.StringVar()
        e = tk.Entry(box, textvariable=self.search, width=30, relief="flat", bd=0, bg=C["paper"], font=F["body_l"])
        e.pack(side="left", ipady=5, padx=(0, 8))
        e.bind("<Escape>", lambda ev: self.search.set(""))
        self.search.trace_add("write", lambda *a: self._fill())
        tk.Label(top, text="Zeitraum", bg=C["hall"], fg=C["muted"], font=F["body"]).pack(side="left", padx=(18, 6))
        self.days = tk.StringVar(value="30 Tage")
        cb = ttk.Combobox(top, textvariable=self.days, values=("14 Tage", "30 Tage", "60 Tage", "90 Tage"),
                          width=9, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda ev: self.reload())
        self.btn_refresh = FlatButton(top, "Bei DHL aktualisieren", lambda: self.reload(force=True), "primary")
        self.btn_refresh.pack(side="right")
        self.info_label = tk.Label(top, text="", bg=C["hall"], fg=C["muted"], font=F["small"])
        self.info_label.pack(side="right", padx=(0, 12))

        self.filter = tk.StringVar(value="Alle")
        self.tabs = FilterTabs(self, self.filter, self._fill, TRACK_FILTERS, TRACK_TABS)
        self.tabs.pack(fill="x", padx=14)

        frm = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        frm.pack(fill="both", expand=True, padx=14, pady=(6, 0))
        self.tree = ttk.Treeview(frm, columns=[c[0] for c in TRACK_COLS], show="headings", selectmode="browse")
        for key, title, w in TRACK_COLS:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=w, minwidth=40, stretch=key in ("name", "state"), anchor="w")
        ys = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)
        self.tree.tag_configure("problem", background=C["red_bg"], foreground=C["red_ink"])
        self.tree.tag_configure("achtung", background=C["amber_bg"], foreground=C["amber_ink"])
        self.tree.tag_configure("zugestellt", foreground=C["green_ink"])
        self.tree.bind("<<TreeviewSelect>>", lambda ev: self._show_events())
        self.tree.bind("<Double-1>", lambda ev: self.open_dhl())
        self.tree.bind("<Button-3>", self._context_menu)

        det = tk.Frame(self, bg=C["paper"], highlightthickness=1, highlightbackground=C["line"])
        det.pack(fill="x", padx=14, pady=12)
        head = tk.Frame(det, bg=C["paper"], padx=16, pady=10)
        head.pack(fill="x")
        self.det_title = tk.Label(head, text="Sendungsverlauf", bg=C["paper"], fg=C["ink"], font=F["section"])
        self.det_title.pack(side="left")
        self.btn_dhl = FlatButton(head, "Bei DHL öffnen ↗", self.open_dhl, "ghost")
        self.btn_dhl.pack(side="right")
        self.btn_copy = FlatButton(head, "Nummer kopieren", self.copy_number, "ghost")
        self.btn_copy.pack(side="right", padx=(0, 6))
        self.btn_new = FlatButton(head, "Neue Sendung daraus…", self.new_from, "ghost")
        self.btn_new.pack(side="right", padx=(0, 6))
        self.btn_return = FlatButton(head, "Retourenlabel…", self.return_from, "ghost")
        self.btn_return.pack(side="right", padx=(0, 6))
        self.events = tk.Text(det, height=8, relief="flat", bd=0, bg=C["paper"], font=F["body"], padx=16,
                              wrap="word", cursor="arrow")
        self.events.pack(fill="x", pady=(0, 10))
        self.events.tag_configure("time", foreground=C["muted"], font=F["mono"])
        self.events.tag_configure("place", foreground=C["muted"])
        self.events.tag_configure("muted", foreground=C["muted"])
        self.events.configure(state="disabled")

        self.bind("<Escape>", lambda ev: self.destroy())
        center_over(self, master, (1180, 720))
        self.after(100, self._poll)
        self.reload()

    # ----- Daten
    def _selected_days(self):
        return int(self.days.get().split()[0])

    def reload(self, force=False):
        """Archiv neu lesen, gespeicherte DHL-Stände anzeigen und offene Sendungen bei DHL abfragen."""
        if self.running:
            return
        trk = DHLTracking(self.cfg)
        if trk.missing:
            messagebox.showinfo(APP_NAME, "Für die Sendungsverfolgung fehlt noch:\n\n• " + "\n• ".join(trk.missing)
                                + "\n\nBitte unter Einstellungen → DHL-Zugang… eintragen.", parent=self)
            return
        self.running = True
        self.btn_refresh.state(["disabled"])
        self.info_label.configure(text="Lade Sendungen…")
        days = self._selected_days()

        def run():
            try:
                store = TrackingStore()
                archive, cache = tracking_archive(days), store.load()
                self.q.put(("data", (archive, cache)))
                limit = (dt.datetime.now() - dt.timedelta(minutes=TRACK_RECHECK_MIN)).isoformat(timespec="seconds")
                todo = [n for n in archive if not cache.get(n, {}).get("delivered")
                        and (force or cache.get(n, {}).get("checked", "") < limit)]
                for i in range(0, len(todo), trk.BATCH):
                    part = todo[i:i + trk.BATCH]
                    self.q.put(("progress", f"Frage DHL ab… {i + len(part)} von {len(todo)}"))
                    now = dt.datetime.now().isoformat(timespec="seconds")
                    got = {n: {**r, "checked": now} for n, r in trk.query(part).items() if n in part}
                    cache = store.save(got)
                    self.q.put(("data", (archive, cache)))
                    time.sleep(0.4)  # DHL erlaubt höchstens 3 Abfragen je Sekunde
                self.q.put(("done", len(todo)))
            except Exception as e:
                self.q.put(("error", e))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "data":
                    self.archive, self.info = val
                    self._fill()
                elif kind == "progress":
                    self.info_label.configure(text=val)
                elif kind in ("done", "error"):
                    self.running = False
                    self.btn_refresh.state(["!disabled"])
                    stamp = dt.datetime.now().strftime("%H:%M")
                    if kind == "done":
                        self.info_label.configure(text=f"Stand {stamp} – {val} Sendungen bei DHL abgefragt"
                                                  if val else f"Stand {stamp} – alles aktuell")
                    else:
                        self.info_label.configure(text="Abfrage fehlgeschlagen")
                        messagebox.showerror(APP_NAME, f"Sendungsverfolgung fehlgeschlagen:\n\n{val}", parent=self)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    # ----- Anzeige
    def _fill(self):
        keep = self.tree.focus()
        words = self.search.get().lower().split()
        self.rows, counts = [], dict.fromkeys(TRACK_FILTERS, 0)
        for num, a in sorted(self.archive.items(), key=lambda kv: kv[1]["sent"], reverse=True):
            info = self.info.get(num)
            cat, text = tracking_state(info, a["sent"])
            hay = " ".join((num, a["ref"], a["name"], text)).lower()
            if words and not all(w in hay for w in words):
                continue
            tab = {"problem": "Probleme", "achtung": "Probleme", "zugestellt": "Zugestellt"}.get(cat, "Unterwegs")
            counts["Alle"] += 1
            counts[tab] += 1
            if self.filter.get() in ("Alle", tab):
                self.rows.append((num, a, info, cat, text))
        self.tabs.update_counts(counts)
        self.tree.delete(*self.tree.get_children())
        mark = {"problem": "⚠  ", "achtung": "●  ", "zugestellt": "✓  "}
        for num, a, info, cat, text in self.rows:
            last = (info or {}).get("time", "")
            self.tree.insert("", "end", iid=num, tags=(cat,), values=(
                a["sent"].strftime("%d.%m.%Y %H:%M"), a["ref"], a["name"], num, mark.get(cat, "") + text,
                last[:16], a["via"]))
        if keep and self.tree.exists(keep):
            self.tree.focus(keep)
            self.tree.selection_set(keep)
        self._show_events()

    def _current(self):
        num = self.tree.focus()
        return num if num and num in self.archive else None

    def _show_events(self):
        num = self._current()
        t = self.events
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not num:
            self.det_title.configure(text="Sendungsverlauf")
            t.insert("end", "Sendung in der Liste auswählen.", ("muted",))
        else:
            a, info = self.archive[num], self.info.get(num) or {}
            self.det_title.configure(text=f"Sendungsverlauf {num} – Auftrag {a['ref']}, {a['name']}")
            if info.get("error"):
                t.insert("end", info["error"], ("muted",))
            elif not info.get("events"):
                t.insert("end", "Noch keine Ereignisse von DHL.", ("muted",))
            for n, ev in enumerate(reversed(info.get("events", []))):  # neuestes oben
                t.insert("end", ("\n" if n else "") + f"{ev['time'][:16]:<16}  ", ("time",))
                t.insert("end", ev["text"])
                if ev["place"]:
                    t.insert("end", f"  ·  {ev['place']}", ("place",))
        t.configure(state="disabled")
        for b in (self.btn_dhl, self.btn_copy, self.btn_new, self.btn_return):
            b.state(["!disabled"] if num else ["disabled"])

    def open_dhl(self):
        num = self._current()
        if num:
            import webbrowser
            webbrowser.open(DHL_TRACK_PAGE.format(urllib.parse.quote(num)))

    def copy_number(self):
        num = self._current()
        if num:
            self.clipboard_clear()
            self.clipboard_append(num)
            self.info_label.configure(text=f"{num} kopiert")

    def _context_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.focus(iid)
        self.tree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Bei DHL öffnen", command=self.open_dhl)
        m.add_command(label="Sendungsnummer kopieren", command=self.copy_number)
        m.add_separator()
        m.add_command(label="Neue Sendung daraus…", command=self.new_from)
        m.add_command(label="Retourenlabel erstellen…", command=self.return_from)
        m.tk_popup(e.x_root, e.y_root)

    def new_from(self, action="new"):
        """Neue Sendung an denselben Empfänger (z. B. Nachlieferung) oder Retourenlabel – Daten aus Liste, Ordner
        oder Speed4Trade."""
        num = self._current()
        if not num:
            return
        a, app, err = self.archive[num], self.master, ""
        self.configure(cursor="watch")
        self.update()
        try:
            template = app.archived_order(a["ref"])
        except Exception as e:
            template, err = None, str(e)
        finally:
            self.configure(cursor="")
        if template is None:
            messagebox.showinfo(APP_NAME, f"Zu Auftrag {a['ref']} wurden keine Empfängerdaten gefunden"
                                + (f":\n\n{err}" if err else " (weder in der Liste noch in Speed4Trade).")
                                + ("\n\nBitte die Sendung über „Sendung erfassen…“ von Hand anlegen."
                                   if action == "new" else ""), parent=self)
            return
        if action == "return":
            app.create_return(template, parent=self)
            self.reload()  # neue Retouren-Sendungsnummer in der Liste zeigen
        else:
            app.new_shipment(template, parent=self)

    def return_from(self):
        self.new_from("return")


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
    if len(sys.argv) > 3 and sys.argv[1] == "--install":
        sys.exit(run_installer(sys.argv[2], int(sys.argv[3])))
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
    else:
        App().mainloop()
