import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from reportlab.graphics.barcode import code128
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DB_FILE = "lager_v32.db"
BACKUP_DIR = "backups"

LAGER = [
    "Medizinlager",
    "Verbrauchslager",
    "Materiallager",
    "Techniklager",
    "Möbellager",
    "Lebensmittellager",
    "Textillager",
]

ROLLEN = ["Admin", "Lagerist", "Vertrieb"]

BESTELLSTATUS = [
    "offen",
    "in_bearbeitung",
    "kommissioniert",
    "verladen",
    "geliefert",
    "storniert",
]

MENU_LABELS = {
    "lagerbestand": "Lagerbestand",
    "lagerplatzuebersicht": "Lagerplatzübersicht",
    "chargen_mhd": "Chargen / MHD",
    "bestandswarnliste": "Bestandswarnliste",
    "nachbestellliste": "Nachbestellliste",
    "lieferantenuebersicht": "Lieferantenübersicht",
    "einkaufsmonitor": "Einkaufsmonitor",
    "gesamtmonitor": "Gesamtmonitor",
    "tv_monitor": "TV-Monitor",
    "bestellungen": "Bestellungen",
    "wareneingang": "Wareneingang",
    "artikel_anlegen": "Artikel anlegen",
    "artikel_bearbeiten": "Artikel bearbeiten / löschen",
    "kundenverwaltung": "Kundenverwaltung",
    "benutzerverwaltung": "Benutzerverwaltung",
    "backup": "Backup",
}

MENU_ORDER = [
    "lagerbestand",
    "lagerplatzuebersicht",
    "chargen_mhd",
    "bestandswarnliste",
    "nachbestellliste",
    "lieferantenuebersicht",
    "einkaufsmonitor",
    "gesamtmonitor",
    "tv_monitor",
    "bestellungen",
    "wareneingang",
    "artikel_anlegen",
    "artikel_bearbeiten",
    "kundenverwaltung",
    "benutzerverwaltung",
    "backup",
]


# -------------------------------------------------
# Hilfsfunktionen
# -------------------------------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def row_to_dict(row):
    return dict(row) if row is not None else None


def html_escape(text):
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def get_now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_date_str() -> str:
    return datetime.now().strftime("%d.%m.%Y")


def get_time_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def interner_user_eingeloggt() -> bool:
    return st.session_state.get("internal_logged_in", False)


def kunde_eingeloggt() -> bool:
    return "kunde" in st.session_state


def current_internal_user() -> dict:
    return st.session_state.get("internal_user", {})


def current_role() -> Optional[str]:
    if not interner_user_eingeloggt():
        return None
    return current_internal_user().get("rolle")


def current_internal_username() -> Optional[str]:
    if not interner_user_eingeloggt():
        return None
    return current_internal_user().get("username")


def default_rights_for_role(rolle: str) -> List[str]:
    if rolle == "Admin":
        return list(MENU_ORDER)
    if rolle == "Lagerist":
        return [
            "lagerbestand",
            "lagerplatzuebersicht",
            "chargen_mhd",
            "bestandswarnliste",
            "nachbestellliste",
            "gesamtmonitor",
            "tv_monitor",
            "bestellungen",
            "wareneingang",
            "artikel_anlegen",
            "artikel_bearbeiten",
            "backup",
        ]
    if rolle == "Vertrieb":
        return [
            "lagerbestand",
            "bestandswarnliste",
            "nachbestellliste",
            "lieferantenuebersicht",
            "einkaufsmonitor",
            "gesamtmonitor",
            "tv_monitor",
            "bestellungen",
            "kundenverwaltung",
            "backup",
        ]
    return []


def load_rights_json(rights_json: Optional[str], rolle: str) -> List[str]:
    if not rights_json:
        return default_rights_for_role(rolle)
    try:
        rights = json.loads(rights_json)
        if isinstance(rights, list):
            return [r for r in rights if r in MENU_ORDER]
    except Exception:
        pass
    return default_rights_for_role(rolle)


def get_current_menu_rights() -> List[str]:
    user = current_internal_user()
    return load_rights_json(user.get("menu_rights_json"), user.get("rolle", ""))


def require_menu_right(menu_key: str):
    if not interner_user_eingeloggt():
        st.error("Bitte zuerst als interner Benutzer einloggen.")
        st.stop()
    if current_role() == "Admin":
        return
    if menu_key not in get_current_menu_rights():
        st.error("Keine Berechtigung für diesen Bereich.")
        st.stop()


def kunde_name(kunde_row):
    firmenname = kunde_row["firmenname"] or ""
    vorname = kunde_row["vorname"] or ""
    nachname = kunde_row["nachname"] or ""
    if firmenname.strip():
        return f"{firmenname} / {vorname} {nachname}".strip()
    return f"{vorname} {nachname}".strip()


def kunde_lieferadresse(kunde_row):
    teile = []
    if kunde_row["firmenname"]:
        teile.append(kunde_row["firmenname"])
    teile.append(f"{kunde_row['vorname']} {kunde_row['nachname']}".strip())
    teile.append(kunde_row["strasse"])
    teile.append(f"{kunde_row['plz']} {kunde_row['ort']}")
    return "\n".join(teile)


def warenkorb_zusammenfassen(warenkorb):
    zusammen = {}
    for item in warenkorb:
        key = (item["artikel_id"], item.get("bestell_typ", "Stück"))
        if key not in zusammen:
            zusammen[key] = item.copy()
        else:
            zusammen[key]["menge_stueck"] += item["menge_stueck"]
            zusammen[key]["eingabe_menge"] = (
                float(zusammen[key].get("eingabe_menge", 0))
                + float(item.get("eingabe_menge", 0))
            )
    return list(zusammen.values())


def warnstatus_text(verfuegbar: int, mindest: int, melde: int) -> str:
    if verfuegbar <= mindest:
        return "Mindestbestand unterschritten"
    if verfuegbar <= melde:
        return "Meldebestand erreicht"
    return "OK"


def status_style(status: str) -> str:
    if status == "offen":
        return "background-color: #fff3cd; color: #856404;"
    if status == "in_bearbeitung":
        return "background-color: #fde2b5; color: #8a4b08;"
    if status == "kommissioniert":
        return "background-color: #d1ecf1; color: #0c5460;"
    if status == "verladen":
        return "background-color: #d6d8ff; color: #2f3b8f;"
    if status == "geliefert":
        return "background-color: #d4edda; color: #155724;"
    if status == "storniert":
        return "background-color: #f8d7da; color: #721c24;"
    return ""


def suche_artikel_df(df: pd.DataFrame, suchtext: str):
    if df.empty:
        return df
    suchtext = (suchtext or "").strip().lower()
    if not suchtext:
        return df
    return df[
        df["artikelnummer"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["name"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["lager"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["ean_barcode"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["hersteller"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["einheit"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["lagerplatz"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["nachschub_lagerplatz"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["lieferant"].astype(str).str.lower().str.contains(suchtext, na=False)
        | df["lieferanten_artikelnummer"].astype(str).str.lower().str.contains(suchtext, na=False)
    ].copy()


# -------------------------------------------------
# Datenbank
# -------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column_exists(table_name: str, column_name: str, column_sql: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    cols = [r["name"] for r in cur.fetchall()]
    if column_name not in cols:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS artikel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikelnummer TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            lager TEXT NOT NULL,
            verpackung_typ TEXT NOT NULL,
            inhalt_pro_pack INTEGER NOT NULL DEFAULT 10,
            packs_pro_palette INTEGER NOT NULL DEFAULT 10,
            bestand_stueck INTEGER NOT NULL DEFAULT 0,
            reserviert_stueck INTEGER NOT NULL DEFAULT 0,
            mindestbestand_stueck INTEGER NOT NULL DEFAULT 0,
            meldebestand_stueck INTEGER NOT NULL DEFAULT 0,
            zielbestand_stueck INTEGER NOT NULL DEFAULT 0,
            ean_barcode TEXT,
            hersteller TEXT,
            einheit TEXT DEFAULT 'Stück',
            lagerplatz TEXT,
            nachschub_lagerplatz TEXT,
            lieferant TEXT,
            lieferanten_artikelnummer TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS artikel_chargen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikel_id INTEGER NOT NULL,
            chargennummer TEXT NOT NULL,
            chargenbarcode TEXT UNIQUE NOT NULL,
            mhd_datum TEXT,
            ausgabe_bis TEXT,
            bestand_stueck INTEGER NOT NULL DEFAULT 0,
            lagerplatz TEXT,
            nachschub_lagerplatz TEXT,
            wareneingang_datum TEXT NOT NULL,
            FOREIGN KEY (artikel_id) REFERENCES artikel(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wareneingang (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikel_id INTEGER NOT NULL,
            charge_id INTEGER,
            menge_stueck INTEGER NOT NULL,
            buchungs_typ TEXT,
            eingabe_menge REAL,
            datum TEXT NOT NULL,
            FOREIGN KEY (artikel_id) REFERENCES artikel(id),
            FOREIGN KEY (charge_id) REFERENCES artikel_chargen(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS kunden (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kunden_nr TEXT UNIQUE NOT NULL,
            firmenname TEXT,
            anrede TEXT,
            vorname TEXT NOT NULL,
            nachname TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            telefon TEXT,
            strasse TEXT NOT NULL,
            plz TEXT NOT NULL,
            ort TEXT NOT NULL,
            passwort_hash TEXT NOT NULL,
            erstellt_am TEXT NOT NULL,
            ist_aktiv INTEGER NOT NULL DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS kunden_lager_freigaben (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kunden_id INTEGER NOT NULL,
            lager TEXT NOT NULL,
            erlaubt INTEGER NOT NULL DEFAULT 1,
            UNIQUE(kunden_id, lager),
            FOREIGN KEY (kunden_id) REFERENCES kunden(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bestellungen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bestellnummer TEXT NOT NULL,
            kunden_id INTEGER NOT NULL,
            kunde_name TEXT NOT NULL,
            lieferadresse TEXT NOT NULL,
            datum TEXT NOT NULL,
            uhrzeit TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'offen',
            status_geaendert_am TEXT,
            FOREIGN KEY (kunden_id) REFERENCES kunden(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bestellpositionen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bestellung_id INTEGER NOT NULL,
            artikel_id INTEGER NOT NULL,
            menge_stueck INTEGER NOT NULL,
            FOREIGN KEY (bestellung_id) REFERENCES bestellungen(id),
            FOREIGN KEY (artikel_id) REFERENCES artikel(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bestellstatus_historie (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bestellung_id INTEGER NOT NULL,
            alter_status TEXT,
            neuer_status TEXT NOT NULL,
            geaendert_am TEXT NOT NULL,
            geaendert_von TEXT,
            geaendert_von_rolle TEXT,
            bemerkung TEXT,
            FOREIGN KEY (bestellung_id) REFERENCES bestellungen(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS internal_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            passwort_hash TEXT NOT NULL,
            rolle TEXT NOT NULL,
            menu_rights_json TEXT,
            erstellt_am TEXT NOT NULL,
            ist_aktiv INTEGER NOT NULL DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS artikel_alternativen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikel_id INTEGER NOT NULL,
            alternativ_artikel_id INTEGER NOT NULL,
            UNIQUE(artikel_id, alternativ_artikel_id),
            FOREIGN KEY (artikel_id) REFERENCES artikel(id),
            FOREIGN KEY (alternativ_artikel_id) REFERENCES artikel(id)
        )
    """)

    conn.commit()
    conn.close()

    ensure_column_exists("artikel", "nachschub_lagerplatz", "TEXT")
    ensure_column_exists("internal_users", "menu_rights_json", "TEXT")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS anzahl FROM artikel")
    if cur.fetchone()["anzahl"] == 0:
        demo = [
            ("MED-1001", "Verbandskasten", "Medizinlager", "Pack", 10, 12, 50, 0, 10, 20, 60, "4012345678901", "MediCare", "Stück", "A-01-01", "A-99-01", "Sanität Nord", "SN-1001"),
            ("VER-1002", "Einweghandschuhe", "Verbrauchslager", "Pack", 10, 20, 120, 0, 30, 50, 150, "4012345678902", "SafeHand", "Stück", "B-02-03", "B-99-02", "Hygiene Plus", "HP-2200"),
            ("MAT-1003", "Schrauben Set", "Materiallager", "Pack", 10, 30, 80, 0, 15, 25, 100, "4012345678903", "FixPro", "Stück", "C-03-02", "C-99-01", "Werkshop GmbH", "WG-330"),
        ]
        cur.executemany("""
            INSERT INTO artikel (
                artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, packs_pro_palette,
                bestand_stueck, reserviert_stueck, mindestbestand_stueck, meldebestand_stueck,
                zielbestand_stueck, ean_barcode, hersteller, einheit, lagerplatz, nachschub_lagerplatz,
                lieferant, lieferanten_artikelnummer
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, demo)

        cur.execute("""
            INSERT INTO artikel_chargen (
                artikel_id, chargennummer, chargenbarcode, mhd_datum, ausgabe_bis,
                bestand_stueck, lagerplatz, nachschub_lagerplatz, wareneingang_datum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (1, "CH-MED-001", "BC-CH-MED-001", "2026-12-31", "2026-11-30", 50, "A-01-01", "A-99-01", get_now_str()))
        cur.execute("""
            INSERT INTO artikel_chargen (
                artikel_id, chargennummer, chargenbarcode, mhd_datum, ausgabe_bis,
                bestand_stueck, lagerplatz, nachschub_lagerplatz, wareneingang_datum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (2, "CH-VER-001", "BC-CH-VER-001", "2027-06-30", "2027-05-31", 120, "B-02-03", "B-99-02", get_now_str()))
        cur.execute("""
            INSERT INTO artikel_chargen (
                artikel_id, chargennummer, chargenbarcode, mhd_datum, ausgabe_bis,
                bestand_stueck, lagerplatz, nachschub_lagerplatz, wareneingang_datum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (3, "CH-MAT-001", "BC-CH-MAT-001", "", "", 80, "C-03-02", "C-99-01", get_now_str()))
        conn.commit()

    cur.execute("SELECT COUNT(*) AS anzahl FROM internal_users")
    if cur.fetchone()["anzahl"] == 0:
        now_str = get_now_str()
        defaults = [
            ("admin", hash_password("admin123"), "Admin", json.dumps(default_rights_for_role("Admin")), now_str, 1),
            ("lager", hash_password("lager123"), "Lagerist", json.dumps(default_rights_for_role("Lagerist")), now_str, 1),
            ("vertrieb", hash_password("vertrieb123"), "Vertrieb", json.dumps(default_rights_for_role("Vertrieb")), now_str, 1),
        ]
        cur.executemany("""
            INSERT INTO internal_users (username, passwort_hash, rolle, menu_rights_json, erstellt_am, ist_aktiv)
            VALUES (?, ?, ?, ?, ?, ?)
        """, defaults)
        conn.commit()

    conn.close()


# -------------------------------------------------
# Backup
# -------------------------------------------------
def create_backup_db() -> str:
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")

    src = sqlite3.connect(DB_FILE)
    dst = sqlite3.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()

    set_setting("last_backup_ts", datetime.now().isoformat())
    set_setting("last_backup_file", backup_path)
    return backup_path


def auto_backup_if_due():
    last_ts = get_setting("last_backup_ts", "")
    if not last_ts:
        create_backup_db()
        return

    try:
        last_dt = datetime.fromisoformat(last_ts)
    except Exception:
        create_backup_db()
        return

    if datetime.now() - last_dt >= timedelta(hours=1):
        create_backup_db()


def list_backups() -> List[str]:
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    files = sorted(Path(BACKUP_DIR).glob("backup_*.db"), reverse=True)
    return [str(f) for f in files]


# -------------------------------------------------
# Interne Benutzer / Rollen
# -------------------------------------------------
def internal_login(username: str, passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM internal_users
        WHERE username = ? AND passwort_hash = ? AND ist_aktiv = 1
    """, (username.strip(), hash_password(passwort)))
    row = cur.fetchone()
    conn.close()
    return row


def hole_interne_benutzer():
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT id, username, rolle, menu_rights_json, erstellt_am, ist_aktiv
        FROM internal_users
        ORDER BY rolle, username
    """, conn)
    conn.close()
    return df


def internen_benutzer_anlegen(username: str, passwort: str, rolle: str, rechte: List[str]):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO internal_users (username, passwort_hash, rolle, menu_rights_json, erstellt_am, ist_aktiv)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (
        username.strip(),
        hash_password(passwort),
        rolle,
        json.dumps(rechte),
        get_now_str(),
    ))
    conn.commit()
    conn.close()


def internes_passwort_aendern(user_id: int, neues_passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE internal_users SET passwort_hash = ? WHERE id = ?", (hash_password(neues_passwort), user_id))
    conn.commit()
    conn.close()


def internen_benutzer_aktualisieren(user_id: int, rolle: str, rechte: List[str], ist_aktiv: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE internal_users
        SET rolle = ?, menu_rights_json = ?, ist_aktiv = ?
        WHERE id = ?
    """, (rolle, json.dumps(rechte), int(ist_aktiv), int(user_id)))
    conn.commit()
    conn.close()


# -------------------------------------------------
# Kunden
# -------------------------------------------------
def kunde_registrieren(
    firmenname,
    anrede,
    vorname,
    nachname,
    email,
    telefon,
    strasse,
    plz,
    ort,
    passwort
):
    conn = get_connection()
    cur = conn.cursor()
    kunden_nr = f"K-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    cur.execute("""
        INSERT INTO kunden (
            kunden_nr, firmenname, anrede, vorname, nachname, email, telefon,
            strasse, plz, ort, passwort_hash, erstellt_am, ist_aktiv
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        kunden_nr,
        firmenname.strip(),
        anrede.strip(),
        vorname.strip(),
        nachname.strip(),
        email.strip().lower(),
        telefon.strip(),
        strasse.strip(),
        plz.strip(),
        ort.strip(),
        hash_password(passwort),
        get_now_str()
    ))

    kunden_id = cur.lastrowid
    for lager in LAGER:
        cur.execute("""
            INSERT INTO kunden_lager_freigaben (kunden_id, lager, erlaubt)
            VALUES (?, ?, 1)
        """, (kunden_id, lager))

    conn.commit()
    conn.close()
    return kunden_nr


def kunde_login(email: str, passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM kunden
        WHERE lower(email) = ? AND passwort_hash = ? AND ist_aktiv = 1
    """, (email.strip().lower(), hash_password(passwort)))
    row = cur.fetchone()
    conn.close()
    return row


def hole_alle_kunden():
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT id, kunden_nr, firmenname, anrede, vorname, nachname, email, telefon,
               strasse, plz, ort, erstellt_am, ist_aktiv
        FROM kunden
        ORDER BY nachname, vorname
    """, conn)
    conn.close()
    return df


def hole_kunde_by_id(kunden_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM kunden WHERE id = ?", (kunden_id,))
    row = cur.fetchone()
    conn.close()
    return row


def hole_erlaubte_lager_fuer_kunde(kunden_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT lager
        FROM kunden_lager_freigaben
        WHERE kunden_id = ? AND erlaubt = 1
        ORDER BY lager
    """, conn, params=(kunden_id,))
    conn.close()
    return df["lager"].tolist()


def setze_lagerfreigaben_fuer_kunde(kunden_id: int, erlaubte_lager: list):
    conn = get_connection()
    cur = conn.cursor()
    for lager in LAGER:
        erlaubt = 1 if lager in erlaubte_lager else 0
        cur.execute("""
            INSERT INTO kunden_lager_freigaben (kunden_id, lager, erlaubt)
            VALUES (?, ?, ?)
            ON CONFLICT(kunden_id, lager)
            DO UPDATE SET erlaubt = excluded.erlaubt
        """, (kunden_id, lager, erlaubt))
    conn.commit()
    conn.close()


def kunde_passwort_aendern(kunden_id: int, altes_passwort: str, neues_passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT passwort_hash FROM kunden WHERE id = ?", (kunden_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise ValueError("Kunde wurde nicht gefunden.")
    if row["passwort_hash"] != hash_password(altes_passwort):
        conn.close()
        raise ValueError("Das aktuelle Passwort ist falsch.")

    cur.execute("UPDATE kunden SET passwort_hash = ? WHERE id = ?", (hash_password(neues_passwort), kunden_id))
    conn.commit()
    conn.close()


def kunde_passwort_admin_reset(kunden_id: int, neues_passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE kunden SET passwort_hash = ? WHERE id = ?", (hash_password(neues_passwort), kunden_id))
    conn.commit()
    conn.close()


def kunde_aktualisieren(kunden_id: int, daten: dict, ist_aktiv: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE kunden
        SET firmenname = ?, anrede = ?, vorname = ?, nachname = ?, email = ?, telefon = ?,
            strasse = ?, plz = ?, ort = ?, ist_aktiv = ?
        WHERE id = ?
    """, (
        daten["firmenname"].strip(),
        daten["anrede"].strip(),
        daten["vorname"].strip(),
        daten["nachname"].strip(),
        daten["email"].strip().lower(),
        daten["telefon"].strip(),
        daten["strasse"].strip(),
        daten["plz"].strip(),
        daten["ort"].strip(),
        int(ist_aktiv),
        int(kunden_id),
    ))
    conn.commit()
    conn.close()


# -------------------------------------------------
# Artikel / Lager / Charge
# -------------------------------------------------
def artikel_df():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM artikel ORDER BY lager, lagerplatz, name", conn)
    conn.close()

    if not df.empty:
        df["verfuegbar_stueck"] = df["bestand_stueck"] - df["reserviert_stueck"]
        df["bestand_pack"] = (df["bestand_stueck"] / df["inhalt_pro_pack"]).round(2)
        df["reserviert_pack"] = (df["reserviert_stueck"] / df["inhalt_pro_pack"]).round(2)
        df["verfuegbar_pack"] = (df["verfuegbar_stueck"] / df["inhalt_pro_pack"]).round(2)
        df["stueck_pro_palette"] = df["inhalt_pro_pack"] * df["packs_pro_palette"]
        df["bestand_palette"] = (df["bestand_stueck"] / df["stueck_pro_palette"]).round(2)
        df["reserviert_palette"] = (df["reserviert_stueck"] / df["stueck_pro_palette"]).round(2)
        df["verfuegbar_palette"] = (df["verfuegbar_stueck"] / df["stueck_pro_palette"]).round(2)
        df["bestandsstatus"] = df.apply(
            lambda r: warnstatus_text(
                int(r["verfuegbar_stueck"]),
                int(r["mindestbestand_stueck"]),
                int(r["meldebestand_stueck"]),
            ),
            axis=1,
        )
        df["empf_nachbestellmenge_stueck"] = df.apply(
            lambda r: max(int(r["zielbestand_stueck"]) - int(r["verfuegbar_stueck"]), 0),
            axis=1,
        )
    return df


def hole_artikel_by_id(artikel_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,))
    row = cur.fetchone()
    conn.close()
    return row


def artikel_speichern(
    artikelnummer,
    name,
    lager,
    verpackung_typ,
    inhalt_pro_pack,
    packs_pro_palette,
    bestand_stueck,
    mindestbestand_stueck,
    meldebestand_stueck,
    zielbestand_stueck,
    ean_barcode,
    hersteller,
    einheit,
    lagerplatz,
    nachschub_lagerplatz,
    lieferant,
    lieferanten_artikelnummer,
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO artikel (
            artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, packs_pro_palette,
            bestand_stueck, reserviert_stueck, mindestbestand_stueck, meldebestand_stueck,
            zielbestand_stueck, ean_barcode, hersteller, einheit, lagerplatz, nachschub_lagerplatz,
            lieferant, lieferanten_artikelnummer
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        artikelnummer.strip(),
        name.strip(),
        lager,
        verpackung_typ,
        int(inhalt_pro_pack),
        int(packs_pro_palette),
        int(bestand_stueck),
        int(mindestbestand_stueck),
        int(meldebestand_stueck),
        int(zielbestand_stueck),
        (ean_barcode or "").strip(),
        (hersteller or "").strip(),
        (einheit or "Stück").strip(),
        (lagerplatz or "").strip(),
        (nachschub_lagerplatz or "").strip(),
        (lieferant or "").strip(),
        (lieferanten_artikelnummer or "").strip(),
    ))
    conn.commit()
    conn.close()


def artikel_aktualisieren(
    artikel_id,
    artikelnummer,
    name,
    lager,
    verpackung_typ,
    inhalt_pro_pack,
    packs_pro_palette,
    bestand_stueck,
    mindestbestand_stueck,
    meldebestand_stueck,
    zielbestand_stueck,
    ean_barcode,
    hersteller,
    einheit,
    lagerplatz,
    nachschub_lagerplatz,
    lieferant,
    lieferanten_artikelnummer,
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT reserviert_stueck FROM artikel WHERE id = ?", (int(artikel_id),))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise ValueError("Artikel wurde nicht gefunden.")

    if int(bestand_stueck) < int(row["reserviert_stueck"]):
        conn.close()
        raise ValueError("Bestand kann nicht kleiner als der reservierte Bestand sein.")

    cur.execute("""
        UPDATE artikel
        SET artikelnummer = ?, name = ?, lager = ?, verpackung_typ = ?, inhalt_pro_pack = ?,
            packs_pro_palette = ?, bestand_stueck = ?, mindestbestand_stueck = ?, meldebestand_stueck = ?,
            zielbestand_stueck = ?, ean_barcode = ?, hersteller = ?, einheit = ?, lagerplatz = ?,
            nachschub_lagerplatz = ?, lieferant = ?, lieferanten_artikelnummer = ?
        WHERE id = ?
    """, (
        artikelnummer.strip(),
        name.strip(),
        lager,
        verpackung_typ,
        int(inhalt_pro_pack),
        int(packs_pro_palette),
        int(bestand_stueck),
        int(mindestbestand_stueck),
        int(meldebestand_stueck),
        int(zielbestand_stueck),
        (ean_barcode or "").strip(),
        (hersteller or "").strip(),
        (einheit or "Stück").strip(),
        (lagerplatz or "").strip(),
        (nachschub_lagerplatz or "").strip(),
        (lieferant or "").strip(),
        (lieferanten_artikelnummer or "").strip(),
        int(artikel_id),
    ))
    conn.commit()
    conn.close()


def artikel_loeschen(artikel_id: int):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS anzahl FROM bestellpositionen WHERE artikel_id = ?", (artikel_id,))
    bestellungen = cur.fetchone()["anzahl"]
    cur.execute("SELECT COUNT(*) AS anzahl FROM wareneingang WHERE artikel_id = ?", (artikel_id,))
    wareneingaenge = cur.fetchone()["anzahl"]
    cur.execute("SELECT COUNT(*) AS anzahl FROM artikel_chargen WHERE artikel_id = ?", (artikel_id,))
    chargen = cur.fetchone()["anzahl"]
    cur.execute("""
        SELECT COUNT(*) AS anzahl
        FROM artikel_alternativen
        WHERE artikel_id = ? OR alternativ_artikel_id = ?
    """, (artikel_id, artikel_id))
    alternativen = cur.fetchone()["anzahl"]
    cur.execute("SELECT reserviert_stueck FROM artikel WHERE id = ?", (artikel_id,))
    artikel = cur.fetchone()
    reserviert = int(artikel["reserviert_stueck"]) if artikel else 0

    if bestellungen > 0 or wareneingaenge > 0 or chargen > 0 or alternativen > 0 or reserviert > 0:
        conn.close()
        raise ValueError("Artikel kann nicht gelöscht werden, weil bereits Belege, Chargen, Reservierungen oder Alternativen existieren.")

    cur.execute("DELETE FROM artikel WHERE id = ?", (artikel_id,))
    conn.commit()
    conn.close()


def hole_artikel_alternativen_ids(artikel_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT alternativ_artikel_id
        FROM artikel_alternativen
        WHERE artikel_id = ?
        ORDER BY alternativ_artikel_id
    """, conn, params=(artikel_id,))
    conn.close()
    return [] if df.empty else df["alternativ_artikel_id"].tolist()


def hole_artikel_alternativen_df(artikel_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT a.*
        FROM artikel_alternativen aa
        JOIN artikel a ON a.id = aa.alternativ_artikel_id
        WHERE aa.artikel_id = ?
        ORDER BY a.name
    """, conn, params=(artikel_id,))
    conn.close()

    if not df.empty:
        df["verfuegbar_stueck"] = df["bestand_stueck"] - df["reserviert_stueck"]
    return df


def setze_artikel_alternativen(artikel_id: int, alternative_ids: list):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM artikel_alternativen WHERE artikel_id = ?", (artikel_id,))
    for alt_id in alternative_ids:
        if int(alt_id) != int(artikel_id):
            cur.execute("""
                INSERT OR IGNORE INTO artikel_alternativen (artikel_id, alternativ_artikel_id)
                VALUES (?, ?)
            """, (int(artikel_id), int(alt_id)))
    conn.commit()
    conn.close()


def menge_zu_stueck(artikel_row, buchungs_typ: str, eingabe_menge: float) -> int:
    if buchungs_typ == "Stück":
        return int(eingabe_menge)
    if buchungs_typ == "Pack":
        return int(eingabe_menge * int(artikel_row["inhalt_pro_pack"]))
    if buchungs_typ == "Palette":
        return int(eingabe_menge * int(artikel_row["inhalt_pro_pack"]) * int(artikel_row["packs_pro_palette"]))
    raise ValueError("Ungültiger Buchungstyp.")


def bestellmenge_zu_stueck(artikel_row, bestell_typ: str, eingabe_menge: float) -> int:
    return menge_zu_stueck(artikel_row, bestell_typ, eingabe_menge)


def wareneingang_buchen(
    artikel_id: int,
    buchungs_typ: str,
    eingabe_menge: float,
    chargennummer: str,
    chargenbarcode: str,
    mhd_datum: str,
    ausgabe_bis: str,
    lagerplatz: str,
    nachschub_lagerplatz: str,
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,))
    artikel = cur.fetchone()
    if artikel is None:
        conn.close()
        raise ValueError("Artikel wurde nicht gefunden.")

    menge_stueck = menge_zu_stueck(artikel, buchungs_typ, eingabe_menge)

    cur.execute("""
        INSERT INTO artikel_chargen (
            artikel_id, chargennummer, chargenbarcode, mhd_datum, ausgabe_bis,
            bestand_stueck, lagerplatz, nachschub_lagerplatz, wareneingang_datum
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        artikel_id,
        chargennummer.strip(),
        chargenbarcode.strip(),
        mhd_datum.strip(),
        ausgabe_bis.strip(),
        menge_stueck,
        lagerplatz.strip(),
        nachschub_lagerplatz.strip(),
        get_now_str(),
    ))
    charge_id = cur.lastrowid

    cur.execute(
        "UPDATE artikel SET bestand_stueck = bestand_stueck + ? WHERE id = ?",
        (menge_stueck, artikel_id),
    )
    cur.execute("""
        INSERT INTO wareneingang (artikel_id, charge_id, menge_stueck, buchungs_typ, eingabe_menge, datum)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        artikel_id,
        charge_id,
        menge_stueck,
        buchungs_typ,
        float(eingabe_menge),
        get_now_str(),
    ))
    conn.commit()
    conn.close()
    return charge_id


def hole_chargen_df():
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT
            c.id,
            a.artikelnummer,
            a.name,
            a.lager,
            c.chargennummer,
            c.chargenbarcode,
            c.mhd_datum,
            c.ausgabe_bis,
            c.bestand_stueck,
            c.lagerplatz,
            c.nachschub_lagerplatz,
            c.wareneingang_datum
        FROM artikel_chargen c
        JOIN artikel a ON a.id = c.artikel_id
        ORDER BY a.lager, c.ausgabe_bis, c.mhd_datum, c.chargennummer
    """, conn)
    conn.close()
    return df


def hole_chargen_fuer_artikel(artikel_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT *
        FROM artikel_chargen
        WHERE artikel_id = ? AND bestand_stueck > 0
        ORDER BY
            CASE WHEN ausgabe_bis IS NULL OR ausgabe_bis = '' THEN 1 ELSE 0 END,
            ausgabe_bis ASC,
            CASE WHEN mhd_datum IS NULL OR mhd_datum = '' THEN 1 ELSE 0 END,
            mhd_datum ASC,
            wareneingang_datum ASC
    """, conn, params=(artikel_id,))
    conn.close()
    return df


def hole_charge_by_id(charge_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.*, a.artikelnummer, a.name, a.lager, a.einheit
        FROM artikel_chargen c
        JOIN artikel a ON a.id = c.artikel_id
        WHERE c.id = ?
    """, (charge_id,))
    row = cur.fetchone()
    conn.close()
    return row


# -------------------------------------------------
# Bestellungen / Historie / Ausbuchung nach Charge
# -------------------------------------------------
def log_bestellstatus(bestellung_id: int, alter_status, neuer_status: str, username=None, rolle=None, bemerkung=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bestellstatus_historie (
            bestellung_id, alter_status, neuer_status, geaendert_am, geaendert_von, geaendert_von_rolle, bemerkung
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        int(bestellung_id),
        alter_status,
        neuer_status,
        get_now_str(),
        username,
        rolle,
        bemerkung,
    ))
    conn.commit()
    conn.close()


def hole_bestellstatus_historie(bestellung_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT alter_status, neuer_status, geaendert_am, geaendert_von, geaendert_von_rolle, bemerkung
        FROM bestellstatus_historie
        WHERE bestellung_id = ?
        ORDER BY id DESC
    """, conn, params=(bestellung_id,))
    conn.close()
    return df


def bestellung_speichern(kunden_id: int, kunde_name_text: str, lieferadresse: str, warenkorb: list):
    jetzt = datetime.now()
    bestellnummer = f"B-{jetzt.strftime('%Y%m%d%H%M%S')}"

    conn = get_connection()
    cur = conn.cursor()

    erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunden_id)

    for pos in warenkorb:
        cur.execute("SELECT bestand_stueck, reserviert_stueck, lager FROM artikel WHERE id = ?", (pos["artikel_id"],))
        artikel = cur.fetchone()

        if artikel is None:
            conn.close()
            raise ValueError(f"Artikel {pos['name']} wurde nicht gefunden.")
        if artikel["lager"] not in erlaubte_lager:
            conn.close()
            raise ValueError(f"Das Lager {artikel['lager']} ist für diesen Kunden gesperrt.")

        verfuegbar = int(artikel["bestand_stueck"]) - int(artikel["reserviert_stueck"])
        if verfuegbar < pos["menge_stueck"]:
            conn.close()
            raise ValueError(f"Zu wenig verfügbarer Bestand für {pos['name']}. Verfügbar: {verfuegbar} Stück.")

    cur.execute("""
        INSERT INTO bestellungen (
            bestellnummer, kunden_id, kunde_name, lieferadresse, datum, uhrzeit, status, status_geaendert_am
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        bestellnummer,
        kunden_id,
        kunde_name_text,
        lieferadresse,
        jetzt.strftime("%d.%m.%Y"),
        jetzt.strftime("%H:%M:%S"),
        "offen",
        get_now_str(),
    ))
    bestellung_id = cur.lastrowid

    for pos in warenkorb:
        cur.execute("""
            INSERT INTO bestellpositionen (bestellung_id, artikel_id, menge_stueck)
            VALUES (?, ?, ?)
        """, (bestellung_id, pos["artikel_id"], pos["menge_stueck"]))

        cur.execute("""
            UPDATE artikel
            SET reserviert_stueck = reserviert_stueck + ?
            WHERE id = ?
        """, (pos["menge_stueck"], pos["artikel_id"]))

    conn.commit()
    conn.close()

    log_bestellstatus(
        bestellung_id=bestellung_id,
        alter_status=None,
        neuer_status="offen",
        username=kunde_name_text,
        rolle="Kunde",
        bemerkung="Bestellung angelegt, Ware reserviert",
    )
    return bestellnummer


def hole_bestellungen():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM bestellungen ORDER BY id DESC", conn)
    conn.close()
    return df


def hole_bestellungen_fuer_kunde(kunden_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT *
        FROM bestellungen
        WHERE kunden_id = ?
        ORDER BY id DESC
    """, conn, params=(kunden_id,))
    conn.close()
    return df


def allocate_from_charges_for_delivery(cur, artikel_id: int, menge: int):
    cur.execute("""
        SELECT id, bestand_stueck
        FROM artikel_chargen
        WHERE artikel_id = ? AND bestand_stueck > 0
        ORDER BY
            CASE WHEN ausgabe_bis IS NULL OR ausgabe_bis = '' THEN 1 ELSE 0 END,
            ausgabe_bis ASC,
            CASE WHEN mhd_datum IS NULL OR mhd_datum = '' THEN 1 ELSE 0 END,
            mhd_datum ASC,
            wareneingang_datum ASC
    """, (artikel_id,))
    chargen = cur.fetchall()

    rest = menge
    for charge in chargen:
        wenn = min(int(charge["bestand_stueck"]), rest)
        if wenn > 0:
            cur.execute("""
                UPDATE artikel_chargen
                SET bestand_stueck = bestand_stueck - ?
                WHERE id = ?
            """, (wenn, int(charge["id"])))
            rest -= wenn
        if rest <= 0:
            break

    if rest > 0:
        raise ValueError("Nicht genügend Chargenbestand für die Auslieferung vorhanden.")


def bestellstatus_setzen(bestellung_id: int, neuer_status: str, bemerkung: str = None):
    if neuer_status not in BESTELLSTATUS:
        raise ValueError("Ungültiger Bestellstatus.")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM bestellungen WHERE id = ?", (int(bestellung_id),))
    bestellung = cur.fetchone()
    if bestellung is None:
        conn.close()
        raise ValueError("Bestellung wurde nicht gefunden.")

    alter_status = bestellung["status"]
    if alter_status == neuer_status:
        conn.close()
        return

    cur.execute("""
        SELECT artikel_id, menge_stueck
        FROM bestellpositionen
        WHERE bestellung_id = ?
    """, (int(bestellung_id),))
    positionen = cur.fetchall()

    if neuer_status == "geliefert":
        if alter_status == "storniert":
            conn.close()
            raise ValueError("Eine stornierte Bestellung kann nicht geliefert werden.")

        for pos in positionen:
            artikel_id = int(pos["artikel_id"])
            menge = int(pos["menge_stueck"])

            cur.execute("SELECT bestand_stueck, reserviert_stueck FROM artikel WHERE id = ?", (artikel_id,))
            artikel = cur.fetchone()
            if artikel is None:
                conn.close()
                raise ValueError("Artikel in Bestellung wurde nicht gefunden.")
            if int(artikel["reserviert_stueck"]) < menge:
                conn.close()
                raise ValueError("Reservierter Bestand ist für die Auslieferung nicht ausreichend.")

            allocate_from_charges_for_delivery(cur, artikel_id, menge)

            cur.execute("""
                UPDATE artikel
                SET bestand_stueck = bestand_stueck - ?,
                    reserviert_stueck = reserviert_stueck - ?
                WHERE id = ?
            """, (menge, menge, artikel_id))

    elif neuer_status == "storniert":
        if alter_status == "geliefert":
            conn.close()
            raise ValueError("Eine gelieferte Bestellung kann nicht storniert werden.")

        for pos in positionen:
            artikel_id = int(pos["artikel_id"])
            menge = int(pos["menge_stueck"])
            cur.execute("SELECT reserviert_stueck FROM artikel WHERE id = ?", (artikel_id,))
            artikel = cur.fetchone()
            if artikel is None:
                conn.close()
                raise ValueError("Artikel in Bestellung wurde nicht gefunden.")
            if int(artikel["reserviert_stueck"]) < menge:
                conn.close()
                raise ValueError("Reservierter Bestand ist für die Freigabe nicht ausreichend.")
            cur.execute("""
                UPDATE artikel
                SET reserviert_stueck = reserviert_stueck - ?
                WHERE id = ?
            """, (menge, artikel_id))

    cur.execute("""
        UPDATE bestellungen
        SET status = ?, status_geaendert_am = ?
        WHERE id = ?
    """, (
        neuer_status,
        get_now_str(),
        int(bestellung_id),
    ))

    conn.commit()
    conn.close()

    log_bestellstatus(
        bestellung_id=bestellung_id,
        alter_status=alter_status,
        neuer_status=neuer_status,
        username=current_internal_username(),
        rolle=current_role(),
        bemerkung=bemerkung,
    )


def hole_bestellpositionen(bestell_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT
            bp.menge_stueck,
            a.artikelnummer,
            a.name,
            a.lager,
            a.lagerplatz,
            a.einheit,
            a.verpackung_typ,
            a.inhalt_pro_pack,
            a.packs_pro_palette
        FROM bestellpositionen bp
        JOIN artikel a ON a.id = bp.artikel_id
        WHERE bp.bestellung_id = ?
    """, conn, params=(bestell_id,))
    conn.close()

    if df.empty:
        return df

    df["lagerplatz_sort"] = df["lagerplatz"].fillna("").astype(str).str.upper()
    df = df.sort_values(by=["lager", "lagerplatz_sort", "name"], ascending=[True, True, True]).reset_index(drop=True)
    df["kommissionier_reihenfolge"] = range(1, len(df) + 1)
    return df


# -------------------------------------------------
# PDFs
# -------------------------------------------------
def pdf_download_button(pdf_bytes: bytes, filename: str, label: str):
    st.download_button(label=label, data=pdf_bytes, file_name=filename, mime="application/pdf")


def render_print_button(title: str, text_content: str, button_label: str = "Drucken"):
    safe_title = html_escape(title)
    safe_content = html_escape(text_content).replace("\n", "<br>")
    html = f"""
    <html>
    <head>
        <style>
            .print-btn {{
                background-color: #1f77b4;
                color: white;
                border: none;
                padding: 10px 18px;
                border-radius: 6px;
                font-size: 16px;
                cursor: pointer;
            }}
        </style>
    </head>
    <body>
        <button class="print-btn" onclick="printDocument()">{html_escape(button_label)}</button>
        <script>
            function printDocument() {{
                var content = `
                    <html>
                    <head>
                        <title>{safe_title}</title>
                        <style>
                            body {{
                                font-family: Arial, sans-serif;
                                padding: 30px;
                                line-height: 1.5;
                            }}
                            h1 {{
                                font-size: 22px;
                                margin-bottom: 20px;
                            }}
                        </style>
                    </head>
                    <body>
                        <h1>{safe_title}</h1>
                        <div>{safe_content}</div>
                    </body>
                    </html>
                `;
                var printWindow = window.open('', '', 'width=900,height=700');
                printWindow.document.write(content);
                printWindow.document.close();
                printWindow.focus();
                printWindow.print();
            }}
        </script>
    </body>
    </html>
    """
    components.html(html, height=70)


def generate_pdf_lieferschein(bestellung, positionen):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("<b>LIEFERSCHEIN</b>", styles["Title"]))
    story.append(Spacer(1, 8))

    info = [
        ["Bestellnummer", bestellung["bestellnummer"]],
        ["Kunde", bestellung["kunde_name"]],
        ["Lieferadresse", bestellung["lieferadresse"].replace("\n", "<br/>")],
        ["Datum", bestellung["datum"]],
        ["Uhrzeit", bestellung["uhrzeit"]],
        ["Status", bestellung["status"]],
    ]
    story.append(Table(info, colWidths=[45 * mm, 120 * mm], style=TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ])))
    story.append(Spacer(1, 12))

    data = [["Artikelnummer", "Bezeichnung", "Lager", "Menge"]]
    for _, pos in positionen.iterrows():
        data.append([str(pos["artikelnummer"]), str(pos["name"]), str(pos["lager"]), str(pos["menge_stueck"])])

    story.append(Table(data, colWidths=[35 * mm, 70 * mm, 45 * mm, 30 * mm], style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9eaf7")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ])))
    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def generate_pdf_kommissionierliste(bestellung, positionen):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("<b>KOMMISSIONIERLISTE</b>", styles["Title"]))
    story.append(Spacer(1, 8))

    info = [
        ["Bestellnummer", bestellung["bestellnummer"]],
        ["Kunde", bestellung["kunde_name"]],
        ["Datum", bestellung["datum"]],
        ["Uhrzeit", bestellung["uhrzeit"]],
        ["Status", bestellung["status"]],
    ]
    story.append(Table(info, colWidths=[45 * mm, 120 * mm], style=TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 6),
    ])))
    story.append(Spacer(1, 12))

    data = [["Pos.", "Artikelnummer", "Bezeichnung", "Lager", "Lagerplatz", "Menge", "Erledigt"]]
    for _, pos in positionen.iterrows():
        data.append([
            str(pos["kommissionier_reihenfolge"]),
            str(pos["artikelnummer"]),
            str(pos["name"]),
            str(pos["lager"]),
            str(pos["lagerplatz"] or ""),
            str(pos["menge_stueck"]),
            "_____",
        ])

    story.append(Table(data, colWidths=[15 * mm, 22 * mm, 45 * mm, 25 * mm, 28 * mm, 18 * mm, 22 * mm], style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f3e8")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ])))
    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def generate_pdf_nachbestellvorschlag(nach_df: pd.DataFrame):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("<b>NACHBESTELLVORSCHLAG</b>", styles["Title"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Erstellt am: {get_now_str()}", styles["Normal"]))
    story.append(Spacer(1, 12))

    if nach_df.empty:
        story.append(Paragraph("Aktuell ist keine Nachbestellung erforderlich.", styles["Normal"]))
        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()
        return pdf

    gruppiert = nach_df.copy()
    gruppiert["lieferant_group"] = gruppiert["lieferant"].fillna("").replace("", "Ohne Lieferant")

    for lieferant, gruppe in gruppiert.groupby("lieferant_group"):
        story.append(Paragraph(f"<b>Lieferant: {lieferant}</b>", styles["Heading2"]))
        story.append(Spacer(1, 4))

        data = [[
            "Artikelnummer",
            "Lief.-Art.-Nr.",
            "Bezeichnung",
            "Lager",
            "Platz",
            "Verfügbar",
            "Zielbestand",
            "Nachbestellmenge",
        ]]

        for _, row in gruppe.iterrows():
            data.append([
                str(row["artikelnummer"]),
                str(row["lieferanten_artikelnummer"] or ""),
                str(row["name"]),
                str(row["lager"]),
                str(row["lagerplatz"] or ""),
                str(row["verfuegbar_stueck"]),
                str(row["zielbestand_stueck"]),
                str(row["nachbestellmenge_stueck"]),
            ])

        table = Table(
            data,
            colWidths=[28 * mm, 28 * mm, 65 * mm, 28 * mm, 25 * mm, 22 * mm, 24 * mm, 30 * mm]
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ddebf7")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("PADDING", (0, 0), (-1, -1), 4),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(table)
        story.append(Spacer(1, 10))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def generate_charge_label_pdf(charge_row):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(100 * mm, 60 * mm))

    c.setFont("Helvetica-Bold", 14)
    c.drawString(10 * mm, 52 * mm, "Chargenetikett")

    c.setFont("Helvetica", 10)
    c.drawString(10 * mm, 45 * mm, f"Artikel: {charge_row['artikelnummer']} - {charge_row['name']}")
    c.drawString(10 * mm, 40 * mm, f"Charge: {charge_row['chargennummer']}")
    c.drawString(10 * mm, 35 * mm, f"Barcode: {charge_row['chargenbarcode']}")
    c.drawString(10 * mm, 30 * mm, f"MHD: {charge_row['mhd_datum'] or '-'}")
    c.drawString(10 * mm, 25 * mm, f"Ausgabe bis: {charge_row['ausgabe_bis'] or '-'}")
    c.drawString(10 * mm, 20 * mm, f"Bestand: {charge_row['bestand_stueck']} {charge_row['einheit']}")
    c.drawString(10 * mm, 15 * mm, f"Lagerplatz: {charge_row['lagerplatz'] or '-'}")

    barcode = code128.Code128(
        charge_row["chargenbarcode"],
        barHeight=12 * mm,
        barWidth=0.35 * mm,
        humanReadable=True
    )
    barcode.drawOn(c, 10 * mm, 2 * mm)

    c.showPage()
    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


# -------------------------------------------------
# Auswertungen
# -------------------------------------------------
def kritische_artikel_df():
    df = artikel_df()
    if df.empty:
        return df
    return df[
        (df["verfuegbar_stueck"] <= df["meldebestand_stueck"]) |
        (df["verfuegbar_stueck"] <= df["mindestbestand_stueck"])
    ].copy()


def nachbestellliste_df():
    df = kritische_artikel_df()
    if df.empty:
        return df
    df["warnstatus"] = df.apply(
        lambda r: warnstatus_text(
            int(r["verfuegbar_stueck"]),
            int(r["mindestbestand_stueck"]),
            int(r["meldebestand_stueck"]),
        ),
        axis=1,
    )
    df["nachbestellmenge_stueck"] = df.apply(
        lambda r: max(int(r["zielbestand_stueck"]) - int(r["verfuegbar_stueck"]), 0),
        axis=1,
    )
    df = df[df["nachbestellmenge_stueck"] > 0].copy()
    return df.sort_values(by=["lieferant", "lager", "lagerplatz", "name"], ascending=[True, True, True, True])


def lieferantenuebersicht_df():
    df = artikel_df()
    if df.empty:
        return df
    df["lieferant_group"] = df["lieferant"].fillna("").replace("", "Ohne Lieferant")
    gruppiert = df.groupby("lieferant_group").agg(
        artikel_anzahl=("id", "count"),
        kritische_artikel=("bestandsstatus", lambda s: int((s != "OK").sum())),
        gesamt_empf_nachbestellung=("empf_nachbestellmenge_stueck", "sum"),
    ).reset_index()
    return gruppiert.sort_values(by=["lieferant_group"])


# -------------------------------------------------
# Login Views
# -------------------------------------------------
def zeige_start_login():
    st.title("📦 Lagerwirtschaft Version 3.2")
    st.write("Bitte zuerst einloggen.")

    tab1, tab2 = st.tabs(["Interner Login", "Kunden Login / Registrierung"])

    with tab1:
        with st.form("internal_login_form"):
            username = st.text_input("Benutzername")
            passwort = st.text_input("Passwort", type="password")
            senden = st.form_submit_button("Einloggen")
        if senden:
            user = internal_login(username, passwort)
            if user:
                st.session_state.internal_logged_in = True
                st.session_state.internal_user = row_to_dict(user)
                st.session_state.pop("kunde", None)
                st.session_state.warenkorb = []
                st.success("Interner Login erfolgreich.")
                st.rerun()
            else:
                st.error("Ungültiger Benutzername oder Passwort.")

    with tab2:
        t1, t2 = st.tabs(["Kunden Login", "Registrierung"])
        with t1:
            with st.form("customer_login_form"):
                email = st.text_input("E-Mail")
                passwort = st.text_input("Passwort", type="password")
                senden = st.form_submit_button("Einloggen")
            if senden:
                kunde = kunde_login(email, passwort)
                if kunde:
                    st.session_state.kunde = row_to_dict(kunde)
                    st.session_state.internal_logged_in = False
                    st.session_state.pop("internal_user", None)
                    st.session_state.warenkorb = []
                    st.success("Kundenlogin erfolgreich.")
                    st.rerun()
                else:
                    st.error("Ungültige E-Mail oder Passwort.")

        with t2:
            with st.form("register_form"):
                firmenname = st.text_input("Firmenname")
                anrede = st.selectbox("Anrede", ["", "Herr", "Frau", "Divers"])
                vorname = st.text_input("Vorname")
                nachname = st.text_input("Nachname")
                email = st.text_input("E-Mail")
                telefon = st.text_input("Telefon")
                strasse = st.text_input("Straße und Hausnummer")
                plz = st.text_input("PLZ")
                ort = st.text_input("Ort")
                passwort = st.text_input("Passwort", type="password")
                passwort2 = st.text_input("Passwort wiederholen", type="password")
                registrieren = st.form_submit_button("Registrieren")

            if registrieren:
                if not vorname.strip() or not nachname.strip() or not email.strip():
                    st.error("Bitte Vorname, Nachname und E-Mail ausfüllen.")
                elif not strasse.strip() or not plz.strip() or not ort.strip():
                    st.error("Bitte vollständige Adresse ausfüllen.")
                elif not passwort:
                    st.error("Bitte ein Passwort vergeben.")
                elif passwort != passwort2:
                    st.error("Die Passwörter stimmen nicht überein.")
                else:
                    try:
                        kunden_nr = kunde_registrieren(
                            firmenname, anrede, vorname, nachname, email, telefon, strasse, plz, ort, passwort
                        )
                        st.success(f"Registrierung erfolgreich. Kundennummer: {kunden_nr}")
                    except sqlite3.IntegrityError:
                        st.error("Diese E-Mail ist bereits registriert.")


# -------------------------------------------------
# Interne Bereiche
# -------------------------------------------------
def zeige_lagerbestand():
    require_menu_right("lagerbestand")
    st.subheader("Lagerbestand")

    suchtext = st.text_input(
        "Artikel suchen / Barcode scannen",
        placeholder="Artikelnummer, Name, EAN/Barcode, Lagerplatz, Nachschubplatz, Lieferant"
    )

    df = artikel_df()
    df = suche_artikel_df(df, suchtext)

    lager_filter = st.selectbox("Unterlager filtern", ["Alle"] + LAGER)
    if lager_filter != "Alle":
        df = df[df["lager"] == lager_filter]

    if df.empty:
        st.info("Keine Artikel gefunden.")
        return

    anzeigen = df[[
        "artikelnummer", "name", "hersteller", "ean_barcode", "einheit", "lager", "lagerplatz",
        "nachschub_lagerplatz", "lieferant", "lieferanten_artikelnummer",
        "bestand_stueck", "reserviert_stueck", "verfuegbar_stueck",
        "mindestbestand_stueck", "meldebestand_stueck", "zielbestand_stueck",
        "bestandsstatus"
    ]].copy()

    anzeigen.columns = [
        "Artikelnummer", "Bezeichnung", "Hersteller", "EAN / Barcode", "Einheit", "Lager", "Lagerplatz",
        "Nachschubplatz", "Lieferant", "Lief.-Art.-Nr.",
        "Bestand Stück", "Reserviert Stück", "Verfügbar Stück",
        "Mindestbestand", "Meldebestand", "Zielbestand",
        "Status"
    ]
    st.dataframe(anzeigen, use_container_width=True)


def zeige_lagerplatzuebersicht():
    require_menu_right("lagerplatzuebersicht")
    st.subheader("Lagerplatzübersicht und Nachschublagerplätze")

    df = artikel_df()
    if df.empty:
        st.info("Keine Artikel vorhanden.")
        return

    anzeige = df[[
        "artikelnummer", "name", "lager", "lagerplatz", "nachschub_lagerplatz",
        "bestand_stueck", "reserviert_stueck", "verfuegbar_stueck"
    ]].copy()
    anzeige.columns = [
        "Artikelnummer", "Bezeichnung", "Lager", "Entnahmeplatz", "Nachschubplatz",
        "Bestand Stück", "Reserviert Stück", "Verfügbar Stück"
    ]
    st.dataframe(anzeige, use_container_width=True)


def zeige_chargen_mhd():
    require_menu_right("chargen_mhd")
    st.subheader("Chargen / MHD / Barcode")

    suchtext = st.text_input(
        "Charge suchen / Barcode scannen",
        placeholder="Chargennummer, Chargenbarcode, Artikelnummer, Bezeichnung"
    ).strip().lower()

    df = hole_chargen_df()
    if not df.empty and suchtext:
        df = df[
            df["chargennummer"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["chargenbarcode"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["artikelnummer"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["name"].astype(str).str.lower().str.contains(suchtext, na=False)
        ].copy()

    if df.empty:
        st.info("Keine Chargen gefunden.")
        return

    anzeige = df[[
        "artikelnummer", "name", "lager", "chargennummer", "chargenbarcode", "mhd_datum",
        "ausgabe_bis", "bestand_stueck", "lagerplatz", "nachschub_lagerplatz", "wareneingang_datum"
    ]].copy()
    anzeige.columns = [
        "Artikelnummer", "Bezeichnung", "Lager", "Charge", "Chargenbarcode", "MHD",
        "Ausgabe bis", "Bestand Stück", "Lagerplatz", "Nachschubplatz", "Wareneingang"
    ]
    st.dataframe(anzeige, use_container_width=True)

    charge_map = {
        f"{row['chargennummer']} | {row['chargenbarcode']} | {row['artikelnummer']} | {row['name']}": int(row["id"])
        for _, row in df.iterrows()
    }
    auswahl = st.selectbox("Charge auswählen", list(charge_map.keys()))
    charge_id = charge_map[auswahl]
    charge_row = hole_charge_by_id(charge_id)

    pdf = generate_charge_label_pdf(charge_row)
    pdf_download_button(
        pdf,
        f"charge_{charge_row['chargennummer']}.pdf",
        "📄 Chargenetikett herunterladen"
    )


def zeige_bestandswarnliste():
    require_menu_right("bestandswarnliste")
    st.subheader("Bestandswarnliste")

    df = kritische_artikel_df()
    if df.empty:
        st.success("Aktuell sind keine Artikel unter Melde- oder Mindestbestand.")
        return

    anzeige = df[[
        "artikelnummer", "name", "hersteller", "lager", "lagerplatz", "nachschub_lagerplatz",
        "bestand_stueck", "reserviert_stueck", "verfuegbar_stueck",
        "mindestbestand_stueck", "meldebestand_stueck", "zielbestand_stueck",
        "bestandsstatus"
    ]].copy()
    anzeige.columns = [
        "Artikelnummer", "Bezeichnung", "Hersteller", "Lager", "Entnahmeplatz", "Nachschubplatz",
        "Bestand Stück", "Reserviert Stück", "Verfügbar Stück",
        "Mindestbestand", "Meldebestand", "Zielbestand", "Warnstatus"
    ]
    st.dataframe(anzeige, use_container_width=True)


def zeige_nachbestellliste():
    require_menu_right("nachbestellliste")
    st.subheader("Nachbestellliste")

    df = nachbestellliste_df()
    if df.empty:
        st.success("Aktuell müssen keine Artikel nachbestellt werden.")
        return

    anzeige = df[[
        "lieferant", "artikelnummer", "lieferanten_artikelnummer", "name", "hersteller",
        "ean_barcode", "lager", "lagerplatz", "nachschub_lagerplatz", "verfuegbar_stueck",
        "zielbestand_stueck", "nachbestellmenge_stueck", "warnstatus"
    ]].copy()
    anzeige.columns = [
        "Lieferant", "Artikelnummer", "Lief.-Art.-Nr.", "Bezeichnung", "Hersteller",
        "EAN / Barcode", "Lager", "Entnahmeplatz", "Nachschubplatz", "Verfügbar Stück",
        "Zielbestand", "Empf. Nachbestellmenge", "Warnstatus"
    ]

    st.dataframe(anzeige, use_container_width=True)

    csv = anzeige.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 Nachbestellliste als CSV herunterladen",
        data=csv,
        file_name=f"nachbestellliste_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv"
    )

    pdf = generate_pdf_nachbestellvorschlag(df)
    pdf_download_button(
        pdf,
        f"nachbestellvorschlag_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
        "📄 PDF-Nachbestellvorschlag herunterladen"
    )


def zeige_lieferantenuebersicht():
    require_menu_right("lieferantenuebersicht")
    st.subheader("Lieferantenübersicht")

    df = lieferantenuebersicht_df()
    if df.empty:
        st.info("Keine Artikel vorhanden.")
        return

    anzeige = df.copy()
    anzeige.columns = ["Lieferant", "Artikelanzahl", "Kritische Artikel", "Gesamt empf. Nachbestellung"]
    st.dataframe(anzeige, use_container_width=True)

    artikel = artikel_df()
    artikel["lieferant_group"] = artikel["lieferant"].fillna("").replace("", "Ohne Lieferant")
    auswahl = st.selectbox("Lieferant auswählen", anzeige["Lieferant"].tolist())
    detail = artikel[artikel["lieferant_group"] == auswahl].copy()

    if not detail.empty:
        detail_anzeige = detail[[
            "artikelnummer", "lieferanten_artikelnummer", "name", "lager", "lagerplatz",
            "nachschub_lagerplatz", "verfuegbar_stueck", "zielbestand_stueck",
            "empf_nachbestellmenge_stueck", "bestandsstatus"
        ]].copy()
        detail_anzeige.columns = [
            "Artikelnummer", "Lief.-Art.-Nr.", "Bezeichnung", "Lager", "Entnahmeplatz",
            "Nachschubplatz", "Verfügbar Stück", "Zielbestand", "Empf. Nachbestellmenge", "Status"
        ]
        st.dataframe(detail_anzeige, use_container_width=True)


def zeige_einkaufsmonitor():
    require_menu_right("einkaufsmonitor")
    st.subheader("Einkaufsmonitor")

    bestellungen = hole_bestellungen()
    nach_df = nachbestellliste_df()

    c1, c2, c3, c4 = st.columns(4)
    offene = len(bestellungen[bestellungen["status"].isin(["offen", "in_bearbeitung", "kommissioniert", "verladen"])]) if not bestellungen.empty else 0
    c1.metric("Offene Kundenbestellungen", offene)
    c2.metric("Nachbestellartikel", len(nach_df))
    c3.metric("Betroffene Lieferanten", len(nach_df["lieferant"].fillna("").replace("", pd.NA).dropna().unique()) if not nach_df.empty else 0)
    c4.metric("Empf. Nachbestellmenge gesamt", int(nach_df["nachbestellmenge_stueck"].sum()) if not nach_df.empty else 0)

    tab1, tab2 = st.tabs(["Was bestellt worden ist", "Was demnächst bestellt werden sollte"])

    with tab1:
        if bestellungen.empty:
            st.info("Es gibt noch keine Bestellungen.")
        else:
            anzeige = bestellungen[[
                "bestellnummer", "datum", "uhrzeit", "kunde_name", "status", "lieferadresse"
            ]].copy()
            anzeige.columns = [
                "Bestellnummer", "Datum", "Uhrzeit", "Kunde", "Status", "Lieferadresse"
            ]
            st.dataframe(anzeige, use_container_width=True, height=420)

    with tab2:
        if nach_df.empty:
            st.success("Aktuell ist keine Nachbestellung erforderlich.")
        else:
            for lieferant, gruppe in nach_df.groupby(nach_df["lieferant"].fillna("").replace("", "Ohne Lieferant")):
                st.markdown(f"### Lieferant: {lieferant}")
                anzeige = gruppe[[
                    "artikelnummer", "lieferanten_artikelnummer", "name", "lager", "lagerplatz",
                    "nachschub_lagerplatz", "verfuegbar_stueck", "zielbestand_stueck",
                    "nachbestellmenge_stueck", "warnstatus"
                ]].copy()
                anzeige.columns = [
                    "Artikelnummer", "Lief.-Art.-Nr.", "Bezeichnung", "Lager", "Entnahmeplatz",
                    "Nachschubplatz", "Verfügbar Stück", "Zielbestand", "Empf. Nachbestellmenge", "Status"
                ]
                st.dataframe(anzeige, use_container_width=True)


def zeige_gesamtmonitor():
    require_menu_right("gesamtmonitor")
    st.subheader("Gesamtmonitor Bestellübersicht")

    bestellungen = hole_bestellungen()
    if bestellungen.empty:
        st.info("Es gibt noch keine Bestellungen.")
        return

    c = st.columns(6)
    for i, status in enumerate(BESTELLSTATUS):
        c[i].metric(status.replace("_", " ").title(), len(bestellungen[bestellungen["status"] == status]))

    status_filter = st.selectbox("Status filtern", ["Alle"] + BESTELLSTATUS, key="monitor_status_filter")
    suchtext = st.text_input("Suche", placeholder="Bestellnummer, Kunde, Lieferadresse, Datum", key="monitor_suche").strip().lower()

    df = bestellungen.copy()
    if status_filter != "Alle":
        df = df[df["status"] == status_filter]
    if suchtext:
        df = df[
            df["bestellnummer"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["kunde_name"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["lieferadresse"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["datum"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["uhrzeit"].astype(str).str.lower().str.contains(suchtext, na=False)
            | df["status"].astype(str).str.lower().str.contains(suchtext, na=False)
        ]

    if df.empty:
        st.info("Keine Bestellungen für den aktuellen Filter gefunden.")
        return

    anzeige = df[["bestellnummer", "datum", "uhrzeit", "kunde_name", "status", "lieferadresse"]].copy()
    anzeige.columns = ["Lieferscheinnummer", "Datum", "Uhrzeit", "Kunde", "Status", "Lieferadresse"]
    st.dataframe(anzeige, use_container_width=True, height=550)


def zeige_tv_monitor():
    require_menu_right("tv_monitor")

    bestellungen = hole_bestellungen()
    counts = {status: len(bestellungen[bestellungen["status"] == status]) if not bestellungen.empty else 0 for status in BESTELLSTATUS}

    df = bestellungen.copy()
    if not df.empty:
        df = df[["bestellnummer", "datum", "uhrzeit", "kunde_name", "status"]].copy()
        df.columns = ["Lieferscheinnummer", "Datum", "Uhrzeit", "Kunde", "Status"]

    components.html("""
        <script>
            setTimeout(function() {
                window.location.reload();
            }, 30000);
        </script>
    """, height=0)

    st.markdown("""
    <style>
        header[data-testid="stHeader"] {display: none;}
        section[data-testid="stSidebar"] {display: none;}
        div[data-testid="stToolbar"] {display: none;}
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        .block-container {padding-top:1rem;padding-left:1.5rem;padding-right:1.5rem;max-width:100%;}
        .tv-title {font-size:42px;font-weight:800;margin-bottom:10px;}
        .tv-subtitle {font-size:20px;color:#666;margin-bottom:20px;}
        .tv-grid {display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:24px;}
        .tv-card {border-radius:18px;padding:22px;color:#111;box-shadow:0 4px 14px rgba(0,0,0,0.08);text-align:center;}
        .tv-card h3 {margin:0;font-size:20px;font-weight:700;}
        .tv-card .value {margin-top:10px;font-size:42px;font-weight:800;}
        .tv-offen {background:#fff3cd;}
        .tv-in_bearbeitung {background:#fde2b5;}
        .tv-kommissioniert {background:#d1ecf1;}
        .tv-verladen {background:#d6d8ff;}
        .tv-geliefert {background:#d4edda;}
        .tv-storniert {background:#f8d7da;}
    </style>
    """, unsafe_allow_html=True)

    h1, h2 = st.columns([8, 2])
    with h1:
        st.markdown('<div class="tv-title">📺 TV-Monitor Bestellstatus</div>', unsafe_allow_html=True)
        st.markdown('<div class="tv-subtitle">Automatische Aktualisierung alle 30 Sekunden</div>', unsafe_allow_html=True)
    with h2:
        if st.button("Zurück zur Hauptseite", use_container_width=True):
            rechte = get_current_menu_rights()
            ziel = "lagerbestand" if "lagerbestand" in rechte else (rechte[0] if rechte else "gesamtmonitor")
            st.session_state["internal_menu"] = MENU_LABELS.get(ziel, "Lagerbestand")
            st.rerun()

    st.markdown(f"""
    <div class="tv-grid">
        <div class="tv-card tv-offen"><h3>Offen</h3><div class="value">{counts["offen"]}</div></div>
        <div class="tv-card tv-in_bearbeitung"><h3>In Bearbeitung</h3><div class="value">{counts["in_bearbeitung"]}</div></div>
        <div class="tv-card tv-kommissioniert"><h3>Kommissioniert</h3><div class="value">{counts["kommissioniert"]}</div></div>
        <div class="tv-card tv-verladen"><h3>Verladen</h3><div class="value">{counts["verladen"]}</div></div>
        <div class="tv-card tv-geliefert"><h3>Geliefert</h3><div class="value">{counts["geliefert"]}</div></div>
        <div class="tv-card tv-storniert"><h3>Storniert</h3><div class="value">{counts["storniert"]}</div></div>
    </div>
    """, unsafe_allow_html=True)

    if df.empty:
        st.info("Es sind aktuell keine Bestellungen vorhanden.")
        return

    st.dataframe(df, use_container_width=True, height=700)


def zeige_wareneingang():
    require_menu_right("wareneingang")
    st.subheader("Wareneingang buchen")

    suchtext = st.text_input(
        "Artikel suchen / Barcode scannen",
        placeholder="Barcode, Artikelnummer, Name, Lagerplatz",
        key="wareneingang_suche"
    )

    df = artikel_df()
    df = suche_artikel_df(df, suchtext)

    if df.empty:
        st.warning("Keine passenden Artikel vorhanden.")
        return

    artikel_map = {
        f"{row['artikelnummer']} | {row['name']} | {row['lager']} | Platz {row['lagerplatz']}": row
        for _, row in df.iterrows()
    }

    auswahl = st.selectbox("Artikel", list(artikel_map.keys()))
    artikel = artikel_map[auswahl]

    st.info(
        f"Artikel: {artikel['name']} | Stück/Pack: {artikel['inhalt_pro_pack']} | "
        f"Pack/Palette: {artikel['packs_pro_palette']} | Platz: {artikel['lagerplatz']} | Nachschub: {artikel['nachschub_lagerplatz']}"
    )

    buchungs_typ = st.selectbox("Wareneingang buchen als", ["Stück", "Pack", "Palette"])
    label = "Anzahl Paletten" if buchungs_typ == "Palette" else f"Anzahl {buchungs_typ}"
    eingabe_menge = st.number_input(label, min_value=1.0, step=1.0, value=1.0)

    st.markdown("### Charge / MHD")
    chargennummer = st.text_input("Chargennummer")
    chargenbarcode = st.text_input("Chargenbarcode")
    mhd_datum = st.text_input("MHD (YYYY-MM-DD)", value="")
    ausgabe_bis = st.text_input("Ausgabe bis (YYYY-MM-DD)", value="")
    lagerplatz = st.text_input("Lagerplatz", value=artikel["lagerplatz"] or "")
    nachschub_lagerplatz = st.text_input("Nachschubplatz", value=artikel["nachschub_lagerplatz"] or "")

    if st.button("Wareneingang buchen"):
        if not chargennummer.strip() or not chargenbarcode.strip():
            st.error("Bitte Chargennummer und Chargenbarcode eingeben.")
        else:
            try:
                charge_id = wareneingang_buchen(
                    int(artikel["id"]),
                    buchungs_typ,
                    float(eingabe_menge),
                    chargennummer,
                    chargenbarcode,
                    mhd_datum,
                    ausgabe_bis,
                    lagerplatz,
                    nachschub_lagerplatz,
                )
                st.success("Wareneingang erfolgreich gebucht.")
                charge_row = hole_charge_by_id(charge_id)
                pdf = generate_charge_label_pdf(charge_row)
                pdf_download_button(pdf, f"charge_{charge_row['chargennummer']}.pdf", "📄 Chargenetikett herunterladen")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Chargenbarcode ist bereits vorhanden.")
            except ValueError as e:
                st.error(str(e))


def zeige_artikel_anlegen():
    require_menu_right("artikel_anlegen")
    st.subheader("Neuen Artikel anlegen")

    with st.form("artikel_form"):
        artikelnummer = st.text_input("Artikelnummer")
        name = st.text_input("Artikelbezeichnung")
        lager = st.selectbox("Unterlager", LAGER)
        verpackung_typ = st.selectbox("Verpackungsart", ["Stück", "Pack"])
        inhalt_pro_pack = st.number_input("Stück pro Pack", min_value=1, value=10, step=1)
        packs_pro_palette = st.number_input("Pack pro Palette", min_value=1, value=10, step=1)
        bestand_stueck = st.number_input("Startbestand in Stück", min_value=0, value=0, step=1)

        st.markdown("### Erweiterte Stammdaten")
        mindestbestand_stueck = st.number_input("Mindestbestand in Stück", min_value=0, value=0, step=1)
        meldebestand_stueck = st.number_input("Meldebestand in Stück", min_value=0, value=0, step=1)
        zielbestand_stueck = st.number_input("Zielbestand in Stück", min_value=0, value=0, step=1)
        ean_barcode = st.text_input("EAN / Barcode")
        hersteller = st.text_input("Hersteller")
        einheit = st.text_input("Einheit", value="Stück")
        lagerplatz = st.text_input("Lagerplatz / Regalplatz", placeholder="z. B. A-01-03")
        nachschub_lagerplatz = st.text_input("Nachschublagerplatz", placeholder="z. B. A-99-03")
        lieferant = st.text_input("Lieferant")
        lieferanten_artikelnummer = st.text_input("Lieferanten-Artikelnummer")

        senden = st.form_submit_button("Artikel speichern")

    if senden:
        if not artikelnummer.strip() or not name.strip():
            st.error("Bitte Artikelnummer und Bezeichnung ausfüllen.")
        elif meldebestand_stueck < mindestbestand_stueck:
            st.error("Meldebestand sollte größer oder gleich Mindestbestand sein.")
        elif zielbestand_stueck < meldebestand_stueck:
            st.error("Zielbestand sollte größer oder gleich Meldebestand sein.")
        else:
            try:
                artikel_speichern(
                    artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, packs_pro_palette,
                    bestand_stueck, mindestbestand_stueck, meldebestand_stueck, zielbestand_stueck,
                    ean_barcode, hersteller, einheit, lagerplatz, nachschub_lagerplatz,
                    lieferant, lieferanten_artikelnummer
                )
                st.success("Artikel wurde gespeichert.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Die Artikelnummer existiert bereits.")


def zeige_artikel_bearbeiten_loeschen():
    require_menu_right("artikel_bearbeiten")
    st.subheader("Artikel bearbeiten / löschen")

    suchtext = st.text_input(
        "Artikel suchen / Barcode scannen",
        placeholder="Artikelnummer, Name, EAN, Lagerplatz",
        key="artikel_edit_suche"
    )

    df = artikel_df()
    df = suche_artikel_df(df, suchtext)
    if df.empty:
        st.info("Keine Artikel gefunden.")
        return

    artikel_map = {
        f"{row['artikelnummer']} | {row['name']} | {row['lager']} | {row['lagerplatz']}": row
        for _, row in df.iterrows()
    }
    auswahl = st.selectbox("Artikel auswählen", list(artikel_map.keys()))
    artikel = artikel_map[auswahl]

    tab1, tab2, tab3 = st.tabs(["Artikel bearbeiten", "Alternativen", "Artikel löschen"])

    with tab1:
        st.info(
            f"Bestand: {int(artikel['bestand_stueck'])} | Reserviert: {int(artikel['reserviert_stueck'])} | "
            f"Verfügbar: {int(artikel['verfuegbar_stueck'])}"
        )

        with st.form("artikel_bearbeiten_form"):
            artikelnummer = st.text_input("Artikelnummer", value=artikel["artikelnummer"])
            name = st.text_input("Artikelbezeichnung", value=artikel["name"])
            lager = st.selectbox("Unterlager", LAGER, index=LAGER.index(artikel["lager"]))
            verpackung_typ = st.selectbox("Verpackungsart", ["Stück", "Pack"], index=["Stück", "Pack"].index(artikel["verpackung_typ"]))
            inhalt_pro_pack = st.number_input("Stück pro Pack", min_value=1, value=int(artikel["inhalt_pro_pack"]), step=1)
            packs_pro_palette = st.number_input("Pack pro Palette", min_value=1, value=int(artikel["packs_pro_palette"]), step=1)
            bestand_stueck = st.number_input("Bestand in Stück", min_value=0, value=int(artikel["bestand_stueck"]), step=1)

            st.markdown("### Erweiterte Stammdaten")
            mindestbestand_stueck = st.number_input("Mindestbestand in Stück", min_value=0, value=int(artikel["mindestbestand_stueck"]), step=1)
            meldebestand_stueck = st.number_input("Meldebestand in Stück", min_value=0, value=int(artikel["meldebestand_stueck"]), step=1)
            zielbestand_stueck = st.number_input("Zielbestand in Stück", min_value=0, value=int(artikel["zielbestand_stueck"]), step=1)
            ean_barcode = st.text_input("EAN / Barcode", value=artikel["ean_barcode"] or "")
            hersteller = st.text_input("Hersteller", value=artikel["hersteller"] or "")
            einheit = st.text_input("Einheit", value=artikel["einheit"] or "Stück")
            lagerplatz = st.text_input("Lagerplatz / Regalplatz", value=artikel["lagerplatz"] or "")
            nachschub_lagerplatz = st.text_input("Nachschublagerplatz", value=artikel["nachschub_lagerplatz"] or "")
            lieferant = st.text_input("Lieferant", value=artikel["lieferant"] or "")
            lieferanten_artikelnummer = st.text_input("Lieferanten-Artikelnummer", value=artikel["lieferanten_artikelnummer"] or "")
            speichern = st.form_submit_button("Änderungen speichern")

        if speichern:
            if not artikelnummer.strip() or not name.strip():
                st.error("Bitte Artikelnummer und Bezeichnung ausfüllen.")
            elif meldebestand_stueck < mindestbestand_stueck:
                st.error("Meldebestand sollte größer oder gleich Mindestbestand sein.")
            elif zielbestand_stueck < meldebestand_stueck:
                st.error("Zielbestand sollte größer oder gleich Meldebestand sein.")
            else:
                try:
                    artikel_aktualisieren(
                        int(artikel["id"]), artikelnummer, name, lager, verpackung_typ,
                        inhalt_pro_pack, packs_pro_palette, bestand_stueck,
                        mindestbestand_stueck, meldebestand_stueck, zielbestand_stueck,
                        ean_barcode, hersteller, einheit, lagerplatz, nachschub_lagerplatz,
                        lieferant, lieferanten_artikelnummer
                    )
                    st.success("Artikel wurde aktualisiert.")
                    st.rerun()
                except (sqlite3.IntegrityError, ValueError) as e:
                    st.error(str(e))

    with tab2:
        st.markdown("### Alternative Artikel hinterlegen")
        alle_artikel = artikel_df()
        alle_artikel = alle_artikel[alle_artikel["id"] != artikel["id"]].copy()
        vorhandene_ids = hole_artikel_alternativen_ids(int(artikel["id"]))

        alt_options = {}
        for _, row in alle_artikel.iterrows():
            label = f"{row['artikelnummer']} | {row['name']} | {row['lager']} | Verfügbar: {row['verfuegbar_stueck']}"
            alt_options[label] = int(row["id"])

        default_labels = [label for label, aid in alt_options.items() if aid in vorhandene_ids]
        neue_auswahl = st.multiselect("Alternative Artikel", options=list(alt_options.keys()), default=default_labels)

        if st.button("Alternativen speichern"):
            alternative_ids = [alt_options[label] for label in neue_auswahl]
            setze_artikel_alternativen(int(artikel["id"]), alternative_ids)
            st.success("Alternative Artikel wurden gespeichert.")
            st.rerun()

    with tab3:
        st.warning("Löschen ist nur möglich, wenn keine Belege, Chargen, Reservierungen oder Alternativen existieren.")
        if st.button("Artikel endgültig löschen"):
            try:
                artikel_loeschen(int(artikel["id"]))
                st.success("Artikel wurde gelöscht.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))


def zeige_kundenverwaltung():
    require_menu_right("kundenverwaltung")
    st.subheader("Kundenverwaltung")

    kunden_df = hole_alle_kunden()

    tab1, tab2 = st.tabs(["Kundenübersicht / bearbeiten", "Kunde anlegen"])

    with tab1:
        if kunden_df.empty:
            st.info("Noch keine Kunden registriert.")
        else:
            st.dataframe(kunden_df, use_container_width=True)

            kunden_map = {}
            for _, row in kunden_df.iterrows():
                kunden_map[f"{row['kunden_nr']} | {row['vorname']} {row['nachname']} | {row['email']}"] = int(row["id"])

            auswahl = st.selectbox("Kunde auswählen", list(kunden_map.keys()))
            kunden_id = kunden_map[auswahl]

            kunde = hole_kunde_by_id(kunden_id)
            erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunden_id)

            t1, t2, t3 = st.tabs(["Lagerfreigaben", "Kundendaten", "Passwort / Aktiv"])

            with t1:
                neue_freigaben = st.multiselect("Erlaubte Unterlager", options=LAGER, default=erlaubte_lager)
                if st.button("Lagerfreigaben speichern"):
                    setze_lagerfreigaben_fuer_kunde(kunden_id, neue_freigaben)
                    st.success("Lagerfreigaben gespeichert.")
                    st.rerun()

            with t2:
                with st.form("kunde_bearbeiten_form"):
                    firmenname = st.text_input("Firmenname", value=kunde["firmenname"] or "")
                    anrede = st.selectbox("Anrede", ["", "Herr", "Frau", "Divers"], index=["", "Herr", "Frau", "Divers"].index(kunde["anrede"] or ""))
                    vorname = st.text_input("Vorname", value=kunde["vorname"] or "")
                    nachname = st.text_input("Nachname", value=kunde["nachname"] or "")
                    email = st.text_input("E-Mail", value=kunde["email"] or "")
                    telefon = st.text_input("Telefon", value=kunde["telefon"] or "")
                    strasse = st.text_input("Straße", value=kunde["strasse"] or "")
                    plz = st.text_input("PLZ", value=kunde["plz"] or "")
                    ort = st.text_input("Ort", value=kunde["ort"] or "")
                    ist_aktiv = st.checkbox("Kunde aktiv", value=bool(kunde["ist_aktiv"]))
                    speichern = st.form_submit_button("Kundendaten speichern")

                if speichern:
                    daten = {
                        "firmenname": firmenname,
                        "anrede": anrede,
                        "vorname": vorname,
                        "nachname": nachname,
                        "email": email,
                        "telefon": telefon,
                        "strasse": strasse,
                        "plz": plz,
                        "ort": ort,
                    }
                    try:
                        kunde_aktualisieren(kunden_id, daten, 1 if ist_aktiv else 0)
                        st.success("Kundendaten gespeichert.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Diese E-Mail ist bereits vergeben.")

            with t3:
                with st.form("kunde_reset_pw_form"):
                    neues_passwort = st.text_input("Neues Passwort für Kunden", type="password")
                    neues_passwort2 = st.text_input("Neues Passwort wiederholen", type="password")
                    reset_btn = st.form_submit_button("Passwort zurücksetzen")

                if reset_btn:
                    if not neues_passwort:
                        st.error("Bitte ein neues Passwort eingeben.")
                    elif neues_passwort != neues_passwort2:
                        st.error("Die Passwörter stimmen nicht überein.")
                    else:
                        kunde_passwort_admin_reset(kunden_id, neues_passwort)
                        st.success("Kundenpasswort wurde zurückgesetzt.")

    with tab2:
        with st.form("admin_kunde_anlegen"):
            firmenname = st.text_input("Firmenname")
            anrede = st.selectbox("Anrede", ["", "Herr", "Frau", "Divers"])
            vorname = st.text_input("Vorname")
            nachname = st.text_input("Nachname")
            email = st.text_input("E-Mail")
            telefon = st.text_input("Telefon")
            strasse = st.text_input("Straße")
            plz = st.text_input("PLZ")
            ort = st.text_input("Ort")
            passwort = st.text_input("Startpasswort", type="password")
            passwort2 = st.text_input("Startpasswort wiederholen", type="password")
            anlegen = st.form_submit_button("Kunde anlegen")

        if anlegen:
            if not vorname.strip() or not nachname.strip() or not email.strip():
                st.error("Bitte Vorname, Nachname und E-Mail ausfüllen.")
            elif not strasse.strip() or not plz.strip() or not ort.strip():
                st.error("Bitte vollständige Adresse ausfüllen.")
            elif not passwort:
                st.error("Bitte ein Passwort vergeben.")
            elif passwort != passwort2:
                st.error("Die Passwörter stimmen nicht überein.")
            else:
                try:
                    kunden_nr = kunde_registrieren(
                        firmenname, anrede, vorname, nachname, email, telefon, strasse, plz, ort, passwort
                    )
                    st.success(f"Kunde erfolgreich angelegt. Kundennummer: {kunden_nr}")
                except sqlite3.IntegrityError:
                    st.error("Diese E-Mail ist bereits registriert.")


def zeige_bestellungen():
    require_menu_right("bestellungen")
    st.subheader("Bestellungen, Status, Historie und Dokumente")

    bestellungen = hole_bestellungen()
    if bestellungen.empty:
        st.info("Es gibt noch keine Bestellungen.")
        return

    status_filter = st.selectbox("Status filtern", ["Alle"] + BESTELLSTATUS)
    if status_filter != "Alle":
        bestellungen = bestellungen[bestellungen["status"] == status_filter]
    if bestellungen.empty:
        st.info("Keine Bestellungen für diesen Filter gefunden.")
        return

    auswahl_map = {
        f"{row['bestellnummer']} | {row['kunde_name']} | {row['datum']} {row['uhrzeit']} | {row['status']}": row
        for _, row in bestellungen.iterrows()
    }

    auswahl = st.selectbox("Bestellung auswählen", list(auswahl_map.keys()))
    bestellung = auswahl_map[auswahl]
    positionen = hole_bestellpositionen(int(bestellung["id"]))
    historie = hole_bestellstatus_historie(int(bestellung["id"]))

    c1, c2 = st.columns(2)
    with c1:
        st.write(f"**Bestellnummer:** {bestellung['bestellnummer']}")
        st.write(f"**Kunde:** {bestellung['kunde_name']}")
        st.write(f"**Datum:** {bestellung['datum']}")
        st.write(f"**Uhrzeit:** {bestellung['uhrzeit']}")
        st.write(f"**Status:** {bestellung['status']}")
    with c2:
        st.write("**Lieferadresse:**")
        st.write(bestellung["lieferadresse"])
        if bestellung["status_geaendert_am"]:
            st.write(f"**Status geändert am:** {bestellung['status_geaendert_am']}")

    st.markdown("### Bestellstatus ändern")
    s1, s2 = st.columns([1, 2])
    with s1:
        neuer_status = st.selectbox(
            "Neuen Status wählen",
            BESTELLSTATUS,
            index=BESTELLSTATUS.index(bestellung["status"]) if bestellung["status"] in BESTELLSTATUS else 0
        )
    with s2:
        bemerkung = st.text_input("Bemerkung zur Statusänderung", value="")

    if st.button("Bestellstatus speichern"):
        try:
            bestellstatus_setzen(int(bestellung["id"]), neuer_status, bemerkung=bemerkung.strip() or None)
            st.success("Bestellstatus wurde aktualisiert.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    st.markdown("### Positionen in Kommissionier-Reihenfolge")
    if not positionen.empty:
        anzeigen_pos = positionen[[
            "kommissionier_reihenfolge", "artikelnummer", "name", "lager", "lagerplatz", "menge_stueck", "einheit"
        ]].copy()
        anzeigen_pos.columns = ["Reihenfolge", "Artikelnummer", "Bezeichnung", "Lager", "Lagerplatz", "Menge", "Einheit"]
        st.dataframe(anzeigen_pos, use_container_width=True)

    st.markdown("### Statushistorie")
    if historie.empty:
        st.info("Noch keine Historie vorhanden.")
    else:
        st.dataframe(historie, use_container_width=True)

    kom_text = build_kommissionierliste_text(bestellung, positionen)
    lief_text = build_lieferschein_text(bestellung, positionen)
    kom_pdf = generate_pdf_kommissionierliste(bestellung, positionen)
    lief_pdf = generate_pdf_lieferschein(bestellung, positionen)

    t1, t2 = st.tabs(["Kommissionierliste", "Lieferschein"])
    with t1:
        st.text_area("Kommissionierliste Vorschau", value=kom_text, height=350)
        a, b = st.columns(2)
        with a:
            pdf_download_button(kom_pdf, f"Kommissionierliste_{bestellung['bestellnummer']}.pdf", "📄 PDF-Kommissionierliste herunterladen")
        with b:
            render_print_button(f"Kommissionierliste {bestellung['bestellnummer']}", kom_text, "🖨️ Kommissionierliste drucken")

    with t2:
        st.text_area("Lieferschein Vorschau", value=lief_text, height=350)
        a, b = st.columns(2)
        with a:
            pdf_download_button(lief_pdf, f"Lieferschein_{bestellung['bestellnummer']}.pdf", "📄 PDF-Lieferschein herunterladen")
        with b:
            render_print_button(f"Lieferschein {bestellung['bestellnummer']}", lief_text, "🖨️ Lieferschein drucken")


def zeige_benutzerverwaltung():
    require_menu_right("benutzerverwaltung")
    st.subheader("Interne Benutzerverwaltung")

    df = hole_interne_benutzer()
    if not df.empty:
        anzeige = df.copy()
        anzeige["menu_rechte"] = anzeige["menu_rights_json"].apply(lambda x: ", ".join(load_rights_json(x, "Admin")))
        anzeige = anzeige[["id", "username", "rolle", "ist_aktiv", "menu_rechte", "erstellt_am"]]
        anzeige.columns = ["ID", "Benutzername", "Rolle", "Aktiv", "Menürechte", "Erstellt am"]
        st.dataframe(anzeige, use_container_width=True)

    tab1, tab2 = st.tabs(["Benutzer anlegen", "Benutzer bearbeiten"])

    with tab1:
        with st.form("internal_user_form"):
            username = st.text_input("Benutzername")
            rolle = st.selectbox("Rolle", ROLLEN)
            rechte = st.multiselect("Sichtbare Hauptmenüs", options=MENU_ORDER, default=default_rights_for_role(rolle), format_func=lambda x: MENU_LABELS[x])
            passwort = st.text_input("Passwort", type="password")
            passwort2 = st.text_input("Passwort wiederholen", type="password")
            speichern = st.form_submit_button("Benutzer anlegen")

        if speichern:
            if not username.strip() or not passwort:
                st.error("Bitte Benutzername und Passwort ausfüllen.")
            elif passwort != passwort2:
                st.error("Die Passwörter stimmen nicht überein.")
            else:
                try:
                    internen_benutzer_anlegen(username, passwort, rolle, rechte)
                    st.success("Interner Benutzer wurde angelegt.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Benutzername existiert bereits.")

    with tab2:
        if df.empty:
            st.info("Keine internen Benutzer vorhanden.")
        else:
            user_map = {f"{row['username']} ({row['rolle']})": row for _, row in df.iterrows()}
            auswahl = st.selectbox("Benutzer auswählen", list(user_map.keys()))
            user = user_map[auswahl]
            aktuelle_rechte = load_rights_json(user["menu_rights_json"], user["rolle"])

            with st.form("internal_user_edit_form"):
                neue_rolle = st.selectbox("Rolle", ROLLEN, index=ROLLEN.index(user["rolle"]))
                neue_rechte = st.multiselect(
                    "Sichtbare Hauptmenüs",
                    options=MENU_ORDER,
                    default=aktuelle_rechte,
                    format_func=lambda x: MENU_LABELS[x]
                )
                aktiv = st.checkbox("Benutzer aktiv", value=bool(user["ist_aktiv"]))
                neues_pw = st.text_input("Neues Passwort (optional)", type="password")
                neues_pw2 = st.text_input("Neues Passwort wiederholen", type="password")
                speichern = st.form_submit_button("Benutzer speichern")

            if speichern:
                if neues_pw and neues_pw != neues_pw2:
                    st.error("Die neuen Passwörter stimmen nicht überein.")
                else:
                    internen_benutzer_aktualisieren(int(user["id"]), neue_rolle, neue_rechte, 1 if aktiv else 0)
                    if neues_pw:
                        internes_passwort_aendern(int(user["id"]), neues_pw)
                    st.success("Benutzer wurde aktualisiert.")
                    st.rerun()


def zeige_backup_verwaltung():
    require_menu_right("backup")
    st.subheader("Backup")

    last_backup = get_setting("last_backup_file", "")
    if last_backup:
        st.write(f"**Letztes Backup:** {last_backup}")

    if st.button("Jetzt Backup erstellen"):
        path = create_backup_db()
        st.success(f"Backup erstellt: {path}")

    backups = list_backups()
    if backups:
        st.markdown("### Vorhandene Backups")
        for path in backups[:20]:
            st.write(path)
    else:
        st.info("Noch keine Backups vorhanden.")


# -------------------------------------------------
# Kundenbereiche
# -------------------------------------------------
def zeige_mein_konto():
    if not kunde_eingeloggt():
        st.error("Bitte als Kunde einloggen.")
        st.stop()

    kunde = st.session_state.kunde
    st.subheader("Mein Konto")

    t1, t2 = st.tabs(["Kundendaten", "Passwort ändern"])

    with t1:
        st.write(f"**Kundennummer:** {kunde['kunden_nr']}")
        st.write(f"**Name:** {kunde_name(kunde)}")
        st.write(f"**E-Mail:** {kunde['email']}")
        st.write(f"**Telefon:** {kunde['telefon']}")
        st.write("**Adresse:**")
        st.text(kunde_lieferadresse(kunde))
        erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunde["id"])
        st.write("**Freigegebene Unterlager:**")
        for lager in erlaubte_lager:
            st.write(f"- {lager}")

    with t2:
        with st.form("kunde_passwort_aendern_form"):
            altes_passwort = st.text_input("Aktuelles Passwort", type="password")
            neues_passwort = st.text_input("Neues Passwort", type="password")
            neues_passwort2 = st.text_input("Neues Passwort wiederholen", type="password")
            speichern = st.form_submit_button("Passwort ändern")

        if speichern:
            if not altes_passwort or not neues_passwort:
                st.error("Bitte alle Passwortfelder ausfüllen.")
            elif neues_passwort != neues_passwort2:
                st.error("Die neuen Passwörter stimmen nicht überein.")
            else:
                try:
                    kunde_passwort_aendern(kunde["id"], altes_passwort, neues_passwort)
                    st.success("Passwort wurde geändert.")
                except ValueError as e:
                    st.error(str(e))


def zeige_shop():
    if not kunde_eingeloggt():
        st.error("Bitte zuerst als Kunde einloggen.")
        st.stop()

    kunde = st.session_state.kunde
    erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunde["id"])

    st.subheader("Shop")
    st.success(f"Eingeloggt als: {kunde['vorname']} {kunde['nachname']} ({kunde['email']})")

    if not erlaubte_lager:
        st.error("Für diesen Kunden sind aktuell keine Unterlager freigeschaltet.")
        return

    df = artikel_df()
    sichtbare_artikel = df[df["lager"].isin(erlaubte_lager)].copy()
    st.info("Sichtbar sind nur die für den Kunden freigegebenen Unterlager.")

    suchtext = st.text_input(
        "Artikel suchen / Barcode scannen",
        placeholder="Artikelnummer, Name, Barcode, Lagerplatz",
        key="shop_suche"
    )
    sichtbare_artikel = suche_artikel_df(sichtbare_artikel, suchtext)

    lager_filter = st.selectbox("Lager auswählen", ["Alle"] + erlaubte_lager, key="shop_lager")
    if lager_filter != "Alle":
        sichtbare_artikel = sichtbare_artikel[sichtbare_artikel["lager"] == lager_filter]

    if sichtbare_artikel.empty:
        st.info("Keine Artikel gefunden.")
        return

    artikel_map = {
        (
            f"{row['artikelnummer']} | {row['name']} | Lager: {row['lager']} | "
            f"Platz: {row['lagerplatz']} | Verfügbar: {row['verfuegbar_stueck']} Stück"
        ): row
        for _, row in sichtbare_artikel.iterrows()
    }

    ausgewaehlt = st.selectbox("Artikel auswählen", list(artikel_map.keys()))
    row = artikel_map[ausgewaehlt]

    st.write(f"**Stück pro Pack:** {int(row['inhalt_pro_pack'])}")
    st.write(f"**Pack pro Palette:** {int(row['packs_pro_palette'])}")
    st.write(f"**Lagerplatz:** {row['lagerplatz']}")
    st.write(f"**Gesamtbestand:** {int(row['bestand_stueck'])} Stück")
    st.write(f"**Reserviert:** {int(row['reserviert_stueck'])} Stück")
    st.write(f"**Verfügbar:** {int(row['verfuegbar_stueck'])} Stück")

    bestell_typ = st.selectbox("Bestellen als", ["Stück", "Pack", "Palette"])
    menge_eingabe = st.number_input("Bestellmenge", min_value=1.0, value=1.0, step=1.0)
    menge_stueck = bestellmenge_zu_stueck(row, bestell_typ, float(menge_eingabe))
    st.write(f"**Umgerechnete Bestellmenge:** {menge_stueck} Stück")

    genug_bestand = menge_stueck <= int(row["verfuegbar_stueck"])

    if genug_bestand:
        if st.button("In den Warenkorb"):
            st.session_state.warenkorb.append({
                "artikel_id": int(row["id"]),
                "artikelnummer": row["artikelnummer"],
                "name": row["name"],
                "lager": row["lager"],
                "menge_stueck": int(menge_stueck),
                "bestell_typ": bestell_typ,
                "eingabe_menge": float(menge_eingabe),
            })
            st.session_state.warenkorb = warenkorb_zusammenfassen(st.session_state.warenkorb)
            st.success("Artikel wurde in den Warenkorb gelegt.")
            st.rerun()
    else:
        st.error(f"Nicht genug verfügbarer Bestand. Verfügbar sind nur {int(row['verfuegbar_stueck'])} Stück.")
        st.markdown("### Alternative Artikel")
        alt_df = hole_artikel_alternativen_df(int(row["id"]))
        if not alt_df.empty:
            alt_df = alt_df[(alt_df["lager"].isin(erlaubte_lager)) & (alt_df["verfuegbar_stueck"] > 0)].copy()

        if alt_df.empty:
            st.info("Keine verfügbaren Alternativen hinterlegt.")
        else:
            alt_map = {
                f"{alt['artikelnummer']} | {alt['name']} | Platz: {alt['lagerplatz']} | Verfügbar: {alt['verfuegbar_stueck']}": alt
                for _, alt in alt_df.iterrows()
            }
            alt_auswahl = st.selectbox("Alternative auswählen", list(alt_map.keys()))
            alt_row = alt_map[alt_auswahl]

            alt_menge_stueck = bestellmenge_zu_stueck(alt_row, bestell_typ, float(menge_eingabe))
            st.write(f"**Umgerechnete Menge für Alternative:** {alt_menge_stueck} Stück")

            if alt_menge_stueck <= int(alt_row["verfuegbar_stueck"]):
                if st.button("Alternative in den Warenkorb"):
                    st.session_state.warenkorb.append({
                        "artikel_id": int(alt_row["id"]),
                        "artikelnummer": alt_row["artikelnummer"],
                        "name": alt_row["name"],
                        "lager": alt_row["lager"],
                        "menge_stueck": int(alt_menge_stueck),
                        "bestell_typ": bestell_typ,
                        "eingabe_menge": float(menge_eingabe),
                    })
                    st.session_state.warenkorb = warenkorb_zusammenfassen(st.session_state.warenkorb)
                    st.success("Alternative wurde in den Warenkorb gelegt.")
                    st.rerun()
            else:
                st.warning("Die Alternative hat ebenfalls nicht genug verfügbaren Bestand.")

    st.markdown("### Warenkorb")
    if st.session_state.warenkorb:
        warenkorb_df = pd.DataFrame(st.session_state.warenkorb)
        if "bestell_typ" not in warenkorb_df.columns:
            warenkorb_df["bestell_typ"] = "Stück"
        if "eingabe_menge" not in warenkorb_df.columns:
            warenkorb_df["eingabe_menge"] = warenkorb_df["menge_stueck"]

        anzeigen = warenkorb_df[["artikelnummer", "name", "lager", "bestell_typ", "eingabe_menge", "menge_stueck"]].copy()
        anzeigen.columns = ["Artikelnummer", "Bezeichnung", "Lager", "Bestellt als", "Eingabemenge", "Menge Stück"]
        st.dataframe(anzeigen, use_container_width=True)

        remove_options = {
            f"{item['artikelnummer']} | {item['name']} | Menge: {item['menge_stueck']} Stück": idx
            for idx, item in enumerate(st.session_state.warenkorb)
        }
        remove_selection = st.selectbox("Position zum Entfernen", list(remove_options.keys()))

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Gewählte Position entfernen"):
                st.session_state.warenkorb.pop(remove_options[remove_selection])
                st.success("Position entfernt.")
                st.rerun()
        with c2:
            if st.button("Warenkorb leeren"):
                st.session_state.warenkorb = []
                st.success("Warenkorb geleert.")
                st.rerun()

        lieferadresse = st.text_area("Lieferadresse", value=kunde_lieferadresse(kunde), height=120)

        if st.button("Bestellung abschließen", type="primary"):
            try:
                bestellnummer = bestellung_speichern(
                    kunden_id=kunde["id"],
                    kunde_name_text=kunde_name(kunde),
                    lieferadresse=lieferadresse.strip(),
                    warenkorb=st.session_state.warenkorb,
                )
                st.session_state.warenkorb = []
                st.success(f"Bestellung {bestellnummer} wurde gespeichert. Die Ware wurde reserviert.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    else:
        st.info("Der Warenkorb ist leer.")


def zeige_meine_bestellungen():
    if not kunde_eingeloggt():
        st.error("Bitte zuerst als Kunde einloggen.")
        st.stop()

    kunde = st.session_state.kunde
    st.subheader("Meine Bestellungen")

    bestellungen = hole_bestellungen_fuer_kunde(kunde["id"])
    if bestellungen.empty:
        st.info("Es liegen noch keine Bestellungen vor.")
        return

    status_filter = st.selectbox("Status filtern", ["Alle"] + BESTELLSTATUS)
    if status_filter != "Alle":
        bestellungen = bestellungen[bestellungen["status"] == status_filter]
    if bestellungen.empty:
        st.info("Keine Bestellungen für diesen Filter gefunden.")
        return

    auswahl_map = {
        f"{row['bestellnummer']} | {row['datum']} {row['uhrzeit']} | Status: {row['status']}": row
        for _, row in bestellungen.iterrows()
    }
    auswahl = st.selectbox("Bestellung auswählen", list(auswahl_map.keys()))
    bestellung = auswahl_map[auswahl]
    positionen = hole_bestellpositionen(int(bestellung["id"]))
    historie = hole_bestellstatus_historie(int(bestellung["id"]))

    c1, c2 = st.columns(2)
    with c1:
        st.write(f"**Bestellnummer:** {bestellung['bestellnummer']}")
        st.write(f"**Datum:** {bestellung['datum']}")
        st.write(f"**Uhrzeit:** {bestellung['uhrzeit']}")
        st.write(f"**Status:** {bestellung['status']}")
    with c2:
        st.write("**Lieferadresse:**")
        st.write(bestellung["lieferadresse"])

    if not positionen.empty:
        anzeigen_pos = positionen[["kommissionier_reihenfolge", "artikelnummer", "name", "menge_stueck", "einheit"]].copy()
        anzeigen_pos.columns = ["Reihenfolge", "Artikelnummer", "Bezeichnung", "Menge", "Einheit"]
        st.dataframe(anzeigen_pos, use_container_width=True)

    st.markdown("### Statushistorie")
    if historie.empty:
        st.info("Noch keine Historie vorhanden.")
    else:
        st.dataframe(historie, use_container_width=True)

    st.info("Kommissionierlisten und Lieferscheine sind nur für interne Benutzer verfügbar.")


# -------------------------------------------------
# Navigation
# -------------------------------------------------
def zeige_sidebar_internal():
    user = current_internal_user()
    rolle = user.get("rolle")
    rechte = get_current_menu_rights()

    st.sidebar.success(f"Interner Benutzer: {user.get('username')} ({rolle})")

    if st.sidebar.button("Logout"):
        st.session_state.internal_logged_in = False
        st.session_state.pop("internal_user", None)
        st.session_state.warenkorb = []
        st.rerun()

    erlaubte_menu_keys = [k for k in MENU_ORDER if k in rechte or rolle == "Admin"]
    erlaubte_labels = [MENU_LABELS[k] for k in erlaubte_menu_keys]

    if "internal_menu" not in st.session_state or st.session_state["internal_menu"] not in erlaubte_labels:
        st.session_state["internal_menu"] = erlaubte_labels[0] if erlaubte_labels else "Lagerbestand"

    return st.sidebar.radio("Bereich auswählen", erlaubte_labels, key="internal_menu")


def zeige_sidebar_kunde():
    kunde = st.session_state.kunde
    st.sidebar.success(f"Kunde: {kunde['vorname']} {kunde['nachname']}")

    if st.sidebar.button("Logout"):
        st.session_state.pop("kunde", None)
        st.session_state.warenkorb = []
        st.rerun()

    return st.sidebar.radio("Bereich auswählen", ["Shop", "Mein Konto", "Meine Bestellungen"])


# -------------------------------------------------
# Main
# -------------------------------------------------
def main():
    st.set_page_config(page_title="Lagerwirtschaft V3.2", layout="wide")
    init_db()
    auto_backup_if_due()

    if "warenkorb" not in st.session_state:
        st.session_state.warenkorb = []

    if not interner_user_eingeloggt() and not kunde_eingeloggt():
        zeige_start_login()
        return

    if kunde_eingeloggt():
        menue = zeige_sidebar_kunde()
        if menue == "Shop":
            zeige_shop()
        elif menue == "Mein Konto":
            zeige_mein_konto()
        elif menue == "Meine Bestellungen":
            zeige_meine_bestellungen()
        return

    if interner_user_eingeloggt():
        menue = zeige_sidebar_internal()

        if menue == MENU_LABELS["lagerbestand"]:
            zeige_lagerbestand()
        elif menue == MENU_LABELS["lagerplatzuebersicht"]:
            zeige_lagerplatzuebersicht()
        elif menue == MENU_LABELS["chargen_mhd"]:
            zeige_chargen_mhd()
        elif menue == MENU_LABELS["bestandswarnliste"]:
            zeige_bestandswarnliste()
        elif menue == MENU_LABELS["nachbestellliste"]:
            zeige_nachbestellliste()
        elif menue == MENU_LABELS["lieferantenuebersicht"]:
            zeige_lieferantenuebersicht()
        elif menue == MENU_LABELS["einkaufsmonitor"]:
            zeige_einkaufsmonitor()
        elif menue == MENU_LABELS["gesamtmonitor"]:
            zeige_gesamtmonitor()
        elif menue == MENU_LABELS["tv_monitor"]:
            zeige_tv_monitor()
        elif menue == MENU_LABELS["bestellungen"]:
            zeige_bestellungen()
        elif menue == MENU_LABELS["wareneingang"]:
            zeige_wareneingang()
        elif menue == MENU_LABELS["artikel_anlegen"]:
            zeige_artikel_anlegen()
        elif menue == MENU_LABELS["artikel_bearbeiten"]:
            zeige_artikel_bearbeiten_loeschen()
        elif menue == MENU_LABELS["kundenverwaltung"]:
            zeige_kundenverwaltung()
        elif menue == MENU_LABELS["benutzerverwaltung"]:
            zeige_benutzerverwaltung()
        elif menue == MENU_LABELS["backup"]:
            zeige_backup_verwaltung()
        return


if __name__ == "__main__":
    main()
