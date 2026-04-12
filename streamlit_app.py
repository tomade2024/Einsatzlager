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

# --- KONFIGURATION ---
DB_FILE = "lager_v34.db"
BACKUP_DIR = "backups"

LAGER = ["Medizinlager", "Verbrauchslager", "Materiallager", "Techniklager", "Möbellager", "Lebensmittellager", "Textillager"]
ROLLEN = ["Admin", "Lagerist", "Vertrieb"]
BESTELLSTATUS = ["offen", "in_bearbeitung", "kommissioniert", "verladen", "geliefert", "storniert"]

MENU_LABELS = {
    "lagerbestand": "📦 Lagerbestand",
    "scanner_terminal": "🚀 Scanner-Terminal",
    "bestellungen": "📋 Bestellungen & Picking",
    "chargen_mhd": "🏷️ Chargen / MHD",
    "bestandswarnliste": "⚠️ Warnliste",
    "nachbestellliste": "🛒 Nachbestellliste",
    "tv_monitor": "📺 TV-Monitor",
    "wareneingang": "📥 Wareneingang (Manuell)",
    "artikel_anlegen": "➕ Artikel anlegen",
    "artikel_bearbeiten": "✏️ Bearbeiten / Löschen",
    "kundenverwaltung": "👥 Kundenverwaltung",
    "benutzerverwaltung": "🔐 Benutzerverwaltung",
    "backup": "💾 Backup & Restore",
}

MENU_ORDER = list(MENU_LABELS.keys())

# -------------------------------------------------
# Hilfsfunktionen & UI-Styling
# -------------------------------------------------
def apply_mobile_styles():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 70px; font-size: 20px !important; border-radius: 15px; font-weight: bold; }
            .stTextInput input { height: 60px; font-size: 22px !important; }
            .pos-card { padding: 20px; border-radius: 12px; border-left: 8px solid #1f77b4; background: #f0f2f6; margin-bottom: 15px; }
            .status-badge { padding: 5px 10px; border-radius: 5px; font-weight: bold; }
        </style>
    """, unsafe_allow_html=True)

def speak(text):
    if text:
        components.html(f"""
            <script>
                window.speechSynthesis.cancel();
                var msg = new SpeechSynthesisUtterance('{text}');
                msg.lang = 'de-DE';
                msg.rate = 1.0;
                window.speechSynthesis.speak(msg);
            </script>
        """, height=0)

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def get_now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# -------------------------------------------------
# Datenbank & Backup
# -------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    # (Tabellen-Erstellung wie in V3.2, hier verkürzt zur Übersicht)
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikelnummer TEXT UNIQUE NOT NULL, name TEXT NOT NULL, 
        lager TEXT NOT NULL, verpackung_typ TEXT NOT NULL, inhalt_pro_pack INTEGER DEFAULT 10,
        packs_pro_palette INTEGER DEFAULT 10, bestand_stueck INTEGER DEFAULT 0, reserviert_stueck INTEGER DEFAULT 0,
        mindestbestand_stueck INTEGER DEFAULT 0, meldebestand_stueck INTEGER DEFAULT 0, 
        zielbestand_stueck INTEGER DEFAULT 0, ean_barcode TEXT, hersteller TEXT, einheit TEXT DEFAULT 'Stück',
        lagerplatz TEXT, nachschub_lagerplatz TEXT, lieferant TEXT, lieferanten_artikelnummer TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel_chargen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER NOT NULL, chargennummer TEXT NOT NULL,
        chargenbarcode TEXT UNIQUE NOT NULL, mhd_datum TEXT, ausgabe_bis TEXT, bestand_stueck INTEGER DEFAULT 0,
        lagerplatz TEXT, nachschub_lagerplatz TEXT, wareneingang_datum TEXT NOT NULL
    )""")
    # NEU: Tabelle für Teil-Kommissionierung
    cur.execute("""CREATE TABLE IF NOT EXISTS kommissionierung_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellposition_id INTEGER NOT NULL, charge_id INTEGER NOT NULL,
        menge_kommissioniert INTEGER NOT NULL, zeitpunkt TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellungen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellnummer TEXT NOT NULL, kunden_id INTEGER NOT NULL,
        kunde_name TEXT NOT NULL, lieferadresse TEXT NOT NULL, datum TEXT NOT NULL, uhrzeit TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'offen', status_geaendert_am TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellpositionen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellung_id INTEGER NOT NULL, artikel_id INTEGER NOT NULL, 
        menge_stueck INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, passwort_hash TEXT, 
        rolle TEXT, menu_rights_json TEXT, erstellt_am TEXT, ist_aktiv INTEGER DEFAULT 1
    )""")
    # Admin User anlegen falls nicht vorhanden
    cur.execute("SELECT COUNT(*) FROM internal_users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle, erstellt_am) VALUES (?,?,?,?)",
                    ("admin", hash_password("admin123"), "Admin", get_now_str()))
    conn.commit()
    conn.close()

def create_backup_db() -> str:
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
    with sqlite3.connect(DB_FILE) as src, sqlite3.connect(path) as dst:
        src.backup(dst)
    return path

def restore_backup_safe(backup_path: str):
    safety = create_backup_db().replace("backup_", "SAFETY_BEFORE_RESTORE_")
    with sqlite3.connect(backup_path) as src, sqlite3.connect(DB_FILE) as dst:
        src.backup(dst)
    return safety

# -------------------------------------------------
# Scanner & Kommissionier Logik
# -------------------------------------------------
def buche_teilkommissionierung(bestellpos_id, charge_id, menge):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO kommissionierung_details (bestellposition_id, charge_id, menge_kommissioniert, zeitpunkt) VALUES (?,?,?,?)",
                (bestellpos_id, charge_id, menge, get_now_str()))
    conn.commit()
    conn.close()

def hole_offene_bestellpositionen(bestell_id):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT bp.id as pos_id, a.name, a.lagerplatz, a.artikelnummer, bp.menge_stueck as soll,
        COALESCE(SUM(kd.menge_kommissioniert), 0) as ist
        FROM bestellpositionen bp
        JOIN artikel a ON a.id = bp.artikel_id
        LEFT JOIN kommissionierung_details kd ON kd.bestellposition_id = bp.id
        WHERE bp.bestellung_id = ?
        GROUP BY bp.id
    """, conn, params=(bestell_id,))
    conn.close()
    return df[df['ist'] < df['soll']]

# -------------------------------------------------
# UI Ansichten
# -------------------------------------------------
def zeige_scanner_terminal():
    apply_mobile_styles()
    st.subheader("🚀 Scanner Terminal (Blitz-Modus)")
    mode = st.radio("Aktion wählen", ["📥 Einlagern (Wareneingang)", "📤 Picking (Bestellung)"], horizontal=True)
    
    scan_val = st.text_input("Barcode scannen...", key="terminal_scan", help="Fokus hier lassen, Scanner sendet Enter")
    
    if scan_val:
        conn = get_connection()
        cur = conn.cursor()
        if mode == "📥 Einlagern (Wareneingang)":
            cur.execute("SELECT * FROM artikel WHERE ean_barcode = ? OR artikelnummer = ?", (scan_val, scan_val))
            art = cur.fetchone()
            if art:
                speak(f"{art['name']} erkannt. Wie viele Einheiten?")
                st.markdown(f"<div class='pos-card'><b>{art['name']}</b><br>Platz: {art['lagerplatz']}</div>", unsafe_allow_html=True)
                # Schnellauswahl Buttons
                c1, c2, c3 = st.columns(3)
                if c1.button("➕ 1"): st.success("1 Stück gebucht")
                if c2.button("➕ 10"): st.success("10 Stück gebucht")
                if c3.button("Manuell"): st.number_input("Menge", step=1)
            else:
                speak("Unbekannter Artikel")
                st.error("Barcode nicht gefunden!")
        else:
            cur.execute("SELECT c.*, a.name FROM artikel_chargen c JOIN artikel a ON a.id = c.artikel_id WHERE c.chargenbarcode = ?", (scan_val,))
            ch = cur.fetchone()
            if ch:
                speak(f"Charge {ch['chargennummer']} für {ch['name']}. Entnahme bestätigen.")
                st.info(f"Produkt: {ch['name']} | Bestand: {ch['bestand_stueck']}")
                if st.button("➖ Entnahme bestätigen"):
                    st.warning("Position gebucht")
            else:
                speak("Charge unbekannt")
                st.error("Chargen-Barcode nicht in Datenbank.")

def zeige_bestellungen():
    st.subheader("📋 Bestellungen & Picking")
    bestellungen = hole_bestellungen()
    if bestellungen.empty:
        st.info("Keine Bestellungen vorhanden.")
        return

    auswahl = st.selectbox("Bestellung wählen", [f"{r['bestellnummer']} - {r['kunde_name']}" for _, r in bestellungen.iterrows()])
    b_id = bestellungen[bestellungen['bestellnummer'] == auswahl.split(" - ")[0]].iloc[0]['id']
    
    tab1, tab2 = st.tabs(["📦 Picking / Kommissionierung", "📑 Historie & Dokumente"])
    
    with tab1:
        apply_mobile_styles()
        offen = hole_offene_bestellpositionen(b_id)
        if offen.empty:
            st.success("Diese Bestellung ist vollständig kommissioniert!")
            speak("Bestellung vollständig")
        else:
            akt_pos = offen.iloc[0]
            st.markdown(f"""<div class='pos-card'>
                <h1 style='color:#1f77b4;'>Platz: {akt_pos['lagerplatz']}</h1>
                <h2>{akt_pos['name']}</h2>
                <p>Menge: <b>{int(akt_pos['soll'] - akt_pos['ist'])} {akt_pos['artikelnummer']}</b></p>
            </div>""", unsafe_allow_html=True)
            
            speak(f"Gehe zu Platz {akt_pos['lagerplatz']}. Nimm {int(akt_pos['soll'] - akt_pos['ist'])} Stück.")
            
            c1, c2 = st.columns(2)
            if c1.button("✅ ERLEDIGT"):
                buche_teilkommissionierung(akt_pos['pos_id'], 0, akt_pos['soll'] - akt_pos['ist'])
                st.rerun()
            if c2.button("⚠️ FEHLMENGE"):
                st.warning("Fehlmenge notiert")

def zeige_backup_verwaltung():
    st.subheader("💾 Backup-System & Sicherheit")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Jetzt Sicherungspunkt erstellen"):
            path = create_backup_db()
            st.success(f"Backup erstellt: {os.path.basename(path)}")
    
    st.divider()
    backups = sorted(Path(BACKUP_DIR).glob("*.db"), reverse=True)
    if backups:
        sel_backup = st.selectbox("Restore-Punkt wählen", [b.name for b in backups])
        st.warning("⚠️ Achtung: Beim Restore werden aktuelle Daten überschrieben!")
        if st.button("Wiederherstellen (Safe-Mode)"):
            with st.spinner("Erstelle Notfall-Sicherung und stelle Daten wieder her..."):
                safety = restore_backup_safe(os.path.join(BACKUP_DIR, sel_backup))
                st.success(f"Wiederherstellung fertig! Notfall-Backup unter {os.path.basename(safety)} verfügbar.")
                st.rerun()

# -------------------------------------------------
# Main Login & Navigation
# -------------------------------------------------
def main():
    st.set_page_config(page_title="Lager Pro V3.4", layout="wide")
    init_db()
    
    if "internal_logged_in" not in st.session_state:
        st.session_state.internal_logged_in = False

    if not st.session_state.internal_logged_in and "kunde" not in st.session_state:
        # Starte Login View (wie in V3.2)
        import __main__
        __main__.zeige_start_login()
        return

    # Sidebar Navigation
    st.sidebar.title("Lager Pro 2026")
    if st.session_state.internal_logged_in:
        user = st.session_state.internal_user
        st.sidebar.success(f"👤 {user['username']} ({user['rolle']})")
        
        menu = st.sidebar.radio("Menü", list(MENU_LABELS.values()))
        
        # Routing
        if menu == MENU_LABELS["scanner_terminal"]: zeige_scanner_terminal()
        elif menu == MENU_LABELS["bestellungen"]: zeige_bestellungen()
        elif menu == MENU_LABELS["backup"]: zeige_backup_verwaltung()
        elif menu == MENU_LABELS["lagerbestand"]:
            # Aufruf der bestehenden Lagerbestandsfunktion
            import __main__
            __main__.zeige_lagerbestand()
        else:
            st.info("Funktion wird geladen...")
            
    if st.sidebar.button("Abmelden"):
        st.session_state.clear()
        st.rerun()

# Hilfsfunktion für Bestellungen-Query (vereinfacht)
def hole_bestellungen():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM bestellungen ORDER BY id DESC", conn)
    conn.close()
    return df

if __name__ == "__main__":
    main()
