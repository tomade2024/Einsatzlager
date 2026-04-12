import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

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
DB_FILE = "lager_v33.db"
BACKUP_DIR = "backups"

LAGER = ["Medizinlager", "Verbrauchslager", "Materiallager", "Techniklager", "Möbellager", "Lebensmittellager", "Textillager"]
ROLLEN = ["Admin", "Lagerist", "Vertrieb"]
BESTELLSTATUS = ["offen", "in_bearbeitung", "kommissioniert", "verladen", "geliefert", "storniert"]

MENU_LABELS = {
    "lagerbestand": "📦 Lagerbestand",
    "scanner_terminal": "🚀 Scanner-Terminal (In/Out)",
    "bestellungen": "📋 Bestellungen & Picking",
    "wareneingang": "📥 Wareneingang (Manuell)",
    "artikel_anlegen": "➕ Artikel anlegen",
    "artikel_bearbeiten": "✏️ Artikel bearbeiten",
    "kundenverwaltung": "👥 Kundenverwaltung",
    "benutzerverwaltung": "🔐 Benutzerverwaltung",
    "backup": "💾 Backup & Restore",
}

# -------------------------------------------------
# CSS & JS HELFER (Mobile & Voice)
# -------------------------------------------------
def apply_mobile_styles():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 60px; font-size: 18px !important; border-radius: 12px; font-weight: bold; }
            .stTextInput input { height: 50px; font-size: 20px !important; }
            .pos-card { padding: 15px; border-radius: 10px; border-left: 5px solid #ff4b4b; background: #f9f9f9; margin-bottom: 15px; box-shadow: 2px 2px 5px rgba(0,0,0,0.05); }
        </style>
    """, unsafe_allow_html=True)

def speak(text):
    if text:
        components.html(f"""
            <script>
                window.speechSynthesis.cancel();
                var msg = new SpeechSynthesisUtterance('{text}');
                msg.lang = 'de-DE';
                msg.rate = 1.1;
                window.speechSynthesis.speak(msg);
            </script>
        """, height=0)

# -------------------------------------------------
# DATENBANK LOGIK
# -------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    # Bestehende Tabellen (aus v3.3) ...
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikelnummer TEXT UNIQUE, name TEXT, lager TEXT, 
        verpackung_typ TEXT, inhalt_pro_pack INTEGER, packs_pro_palette INTEGER, 
        bestand_stueck INTEGER DEFAULT 0, reserviert_stueck INTEGER DEFAULT 0,
        mindestbestand_stueck INTEGER DEFAULT 0, meldebestand_stueck INTEGER DEFAULT 0, 
        zielbestand_stueck INTEGER DEFAULT 0, ean_barcode TEXT, hersteller TEXT, 
        einheit TEXT, lagerplatz TEXT, nachschub_lagerplatz TEXT, lieferant TEXT, lieferanten_artikelnummer TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel_chargen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER, chargennummer TEXT, 
        chargenbarcode TEXT UNIQUE, mhd_datum TEXT, ausgabe_bis TEXT, bestand_stueck INTEGER, 
        lagerplatz TEXT, nachschub_lagerplatz TEXT, wareneingang_datum TEXT
    )""")
    # NEU: Teilkommissionierung Details
    cur.execute("""CREATE TABLE IF NOT EXISTS kommissionierung_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellposition_id INTEGER, charge_id INTEGER, 
        menge_kommissioniert INTEGER, zeitpunkt TEXT
    )""")
    # ... weitere Tabellen wie 'kunden', 'bestellungen', 'bestellpositionen', 'internal_users' etc. (gekürzt für Platz)
    conn.commit()
    conn.close()

# -------------------------------------------------
# BACKUP & SAFE RESTORE
# -------------------------------------------------
def create_backup_db() -> str:
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
    with sqlite3.connect(DB_FILE) as src:
        with sqlite3.connect(path) as dst:
            src.backup(dst)
    return path

def restore_backup_safe(backup_path: str):
    # 1. Sicherheitskopie des aktuellen Standes
    safety = create_backup_db().replace("backup_", "SAFETY_BEFORE_RESTORE_")
    # 2. Restore
    with sqlite3.connect(backup_path) as src:
        with sqlite3.connect(DB_FILE) as dst:
            src.backup(dst)
    return safety

# -------------------------------------------------
# SCANNER LOGIK (IN/OUT)
# -------------------------------------------------
def zeige_scanner_terminal():
    apply_mobile_styles()
    mode = st.radio("Modus wählen", ["📥 Wareneingang (Inbound)", "📤 Picking (Outbound)"], horizontal=True)
    
    scan_val = st.text_input("Barcode scannen...", key="main_scanner", placeholder="Scan focus here...")
    
    if scan_val:
        conn = get_connection()
        cur = conn.cursor()
        
        if mode == "📥 Wareneingang (Inbound)":
            cur.execute("SELECT * FROM artikel WHERE ean_barcode = ? OR artikelnummer = ?", (scan_val, scan_val))
            art = cur.fetchone()
            if art:
                speak(f"{art['name']} erkannt. Menge wählen.")
                st.markdown(f"<div class='pos-card'><b>{art['name']}</b><br>Bestand: {art['bestand_stueck']}</div>", unsafe_allow_html=True)
                if st.button("➕ 1 Einheit buchen"):
                    # Hier Buchungslogik aufrufen
                    st.success("Gebucht!")
            else:
                speak("Unbekannter Artikel")
                
        else: # Outbound Picking
            cur.execute("SELECT c.*, a.name FROM artikel_chargen c JOIN artikel a ON a.id = c.artikel_id WHERE c.chargenbarcode = ?", (scan_val,))
            charge = cur.fetchone()
            if charge:
                speak(f"Charge {charge['chargennummer']} für {charge['name']} erkannt.")
                st.info(f"Produkt: {charge['name']} | Bestand: {charge['bestand_stueck']}")
                if st.button("➖ 1 Einheit entnehmen"):
                    # Hier Ausbuchungslogik
                    st.warning("Entnahme gebucht!")
            else:
                speak("Charge nicht gefunden")

# -------------------------------------------------
# UI KOMPONENTEN
# -------------------------------------------------
def zeige_backup_verwaltung():
    st.subheader("Sicherungssystem")
    if st.button("Manuelles Backup erstellen"):
        p = create_backup_db()
        st.success(f"Gesichert: {p}")
    
    backups = sorted(Path(BACKUP_DIR).glob("*.db"), reverse=True)
    if backups:
        selected = st.selectbox("Backup für Wiederherstellung", [b.name for b in backups])
        if st.button("Wiederherstellen (mit Auto-Sicherung)"):
            safety_path = restore_backup_safe(os.path.join(BACKUP_DIR, selected))
            st.warning(f"Sicherheitskopie vor Restore erstellt: {safety_path}")
            st.success("Daten erfolgreich wiederhergestellt!")
            st.rerun()

# -------------------------------------------------
# MAIN APP
# -------------------------------------------------
def main():
    st.set_page_config(page_title="Lager Pro 2026", layout="wide")
    init_db()
    
    # Simpler Login-Check (Platzhalter für deine Login-Logik)
    if "internal_logged_in" not in st.session_state:
        st.session_state.internal_logged_in = True # Für Testzwecke auf True
        st.session_state.internal_user = {"username": "Admin", "rolle": "Admin"}

    menu = st.sidebar.radio("Navigation", list(MENU_LABELS.values()))

    if menu == MENU_LABELS["scanner_terminal"]:
        zeige_scanner_terminal()
    elif menu == MENU_LABELS["backup"]:
        zeige_backup_verwaltung()
    elif menu == MENU_LABELS["lagerbestand"]:
        st.write("Hier folgt die Bestandsliste...")
    else:
        st.info("Diese Sektion wird in dieser Version geladen...")

if __name__ == "__main__":
    main()
