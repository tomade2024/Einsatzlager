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
DB_FILE = "lager_v39_final.db"
BACKUP_DIR = "backups"

LAGER = ["Medizinlager", "Verbrauchslager", "Materiallager", "Techniklager", "Möbellager", "Lebensmittellager", "Textillager"]
ROLLEN = ["Admin", "Lagerist", "Vertrieb"]
BESTELLSTATUS = ["offen", "in_bearbeitung", "kommissioniert", "verladen", "geliefert", "storniert"]

MENU_LABELS = {
    "dashboard": "📊 Gesamt-Monitor",
    "scanner_terminal": "🚀 Scanner-Terminal",
    "bestellungen": "📋 Picking / Aufträge",
    "lagerbestand": "📦 Lagerbestand",
    "reporting": "📉 Berichte & Export",
    "artikel_anlegen": "➕ Artikel anlegen",
    "kundenverwaltung": "👥 Kundenverwaltung (Admin)",
    "benutzerverwaltung": "🔐 Benutzerverwaltung (Admin)",
    "backup": "💾 Backup & Restore",
}

# -------------------------------------------------
# Hilfsfunktionen & UI-Styling
# -------------------------------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def apply_mobile_styles():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 65px; font-size: 18px !important; border-radius: 12px; font-weight: bold; margin-bottom: 5px; }
            .stTextInput input { height: 55px; font-size: 20px !important; }
            .pos-card { padding: 15px; border-radius: 10px; border-left: 8px solid #1f77b4; background: #f9f9f9; margin-bottom: 15px; box-shadow: 2px 2px 5px rgba(0,0,0,0.05); }
            .monitor-box { padding: 15px; border-radius: 8px; color: white; text-align: center; margin-bottom: 10px; }
        </style>
    """, unsafe_allow_html=True)

def speak(text):
    if text:
        components.html(f"""
            <script>
                window.speechSynthesis.cancel();
                var msg = new SpeechSynthesisUtterance('{text}');
                msg.lang = 'de-DE';
                window.speechSynthesis.speak(msg);
            </script>
        """, height=0)

# -------------------------------------------------
# Datenbank-Kernfunktionen
# -------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    # Stammdaten Tabellen
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikelnummer TEXT UNIQUE, name TEXT, lager TEXT, 
        inhalt_pro_pack INTEGER DEFAULT 10, packs_pro_palette INTEGER DEFAULT 50, 
        bestand_stueck INTEGER DEFAULT 0, meldebestand_stueck INTEGER DEFAULT 10, zielbestand_stueck INTEGER DEFAULT 50,
        lagerplatz TEXT, ean_barcode TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kunden (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kunden_nr TEXT UNIQUE, name TEXT, email TEXT, passwort_hash TEXT, aktiv INTEGER DEFAULT 1
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, passwort_hash TEXT, rolle TEXT, ist_aktiv INTEGER DEFAULT 1
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellungen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellnummer TEXT UNIQUE, kunde_name TEXT, status TEXT DEFAULT 'offen', datum TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellpositionen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellung_id INTEGER, artikel_id INTEGER, menge_stueck INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kommissionierung_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellposition_id INTEGER, menge_kommissioniert INTEGER, zeitpunkt TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS lager_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER, menge INTEGER, typ TEXT, zeitpunkt TEXT, benutzer TEXT
    )""")
    
    # Admin User Check
    cur.execute("SELECT COUNT(*) FROM internal_users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle) VALUES (?,?,?)",
                    ("admin", hash_password("admin123"), "Admin"))
    conn.commit()
    conn.close()

# -------------------------------------------------
# Backup & Reporting Logik
# -------------------------------------------------
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

def to_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Lagerbericht')
    return output.getvalue()

# -------------------------------------------------
# UI SEKTIONEN
# -------------------------------------------------

def zeige_dashboard():
    st.title("📊 Live Logistik Monitor")
    conn = get_connection()
    
    # Metriken
    c1, c2, c3 = st.columns(3)
    offen = conn.execute("SELECT COUNT(*) FROM bestellungen WHERE status='offen'").fetchone()[0]
    laufend = conn.execute("SELECT COUNT(*) FROM bestellungen WHERE status='in_bearbeitung'").fetchone()[0]
    warn = conn.execute("SELECT COUNT(*) FROM artikel WHERE bestand_stueck <= meldebestand_stueck").fetchone()[0]
    
    c1.metric("Offene Aufträge", offen)
    c2.metric("In Arbeit", laufend)
    c3.metric("Nachzubestellen", warn, delta=f"{warn} Artikel", delta_color="inverse")

    st.divider()
    
    k1, k2, k3 = st.columns(3)
    bestell_df = pd.read_sql_query("SELECT bestellnummer, status FROM bestellungen", conn)
    with k1:
        st.markdown("<div class='monitor-box' style='background-color: #ff4b4b;'><h3>Neu</h3></div>", unsafe_allow_html=True)
        st.write(bestell_df[bestell_df['status'] == 'offen'][['bestellnummer']])
    with k2:
        st.markdown("<div class='monitor-box' style='background-color: #ffa500;'><h3>Picking</h3></div>", unsafe_allow_html=True)
        st.write(bestell_df[bestell_df['status'] == 'in_bearbeitung'][['bestellnummer']])
    with k3:
        st.markdown("<div class='monitor-box' style='background-color: #28a745;'><h3>Fertig</h3></div>", unsafe_allow_html=True)
        st.write(bestell_df[bestell_df['status'] == 'kommissioniert'][['bestellnummer']])
    conn.close()

def zeige_scanner_terminal():
    apply_mobile_styles()
    st.subheader("🚀 Scanner-Terminal Pro")
    scan_input = st.text_input("Barcode scannen...", key="terminal_input")
    
    if scan_input:
        conn = get_connection()
        art = conn.execute("SELECT * FROM artikel WHERE artikelnummer=? OR ean_barcode=?", (scan_input, scan_input)).fetchone()
        
        if art:
            st.markdown(f"<div class='pos-card'><h2>{art['name']}</h2><p>Platz: {art['lagerplatz']} | Bestand: {art['bestand_stueck']} Stk</p></div>", unsafe_allow_html=True)
            speak(f"{art['name']} erkannt.")
            
            tab1, tab2, tab3 = st.tabs(["📥 Eingang", "📤 Entnahme", "🚨 Bruch"])
            pack = art['inhalt_pro_pack']
            pal = art['inhalt_pro_pack'] * art['packs_pro_palette']
            
            with tab1:
                c1, c2, c3 = st.columns(3)
                if c1.button("+1 Stk"): buche(art['id'], 1, "Zulauf")
                if c2.button(f"+1 Pack ({pack})"): buche(art['id'], pack, "Zulauf")
                if c3.button(f"+1 Pal ({pal})"): buche(art['id'], pal, "Zulauf")
            with tab2:
                c1, c2, c3 = st.columns(3)
                if c1.button("-1 Stk"): buche(art['id'], -1, "Entnahme")
                if c2.button(f"-1 Pack ({pack})"): buche(art['id'], -pack, "Entnahme")
                if c3.button(f"-1 Pal ({pal})"): buche(art['id'], -pal, "Entnahme")
            with tab3:
                menge = st.number_input("Bruch-Menge", min_value=1, value=1)
                if st.button("🚨 Bruch buchen"): buche(art['id'], -menge, "Bruch")
        conn.close()

def buche(art_id, menge, typ):
    conn = get_connection()
    user = st.session_state.internal_user['username']
    conn.execute("UPDATE artikel SET bestand_stueck = bestand_stueck + ? WHERE id=?", (menge, art_id))
    conn.execute("INSERT INTO lager_log (artikel_id, menge, typ, zeitpunkt, benutzer) VALUES (?,?,?,?,?)",
                 (art_id, menge, typ, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user))
    conn.commit()
    conn.close()
    st.success("Buchung erfolgreich!")
    st.rerun()

def zeige_reporting():
    st.subheader("📊 Reporting & Export")
    conn = get_connection()
    df = pd.read_sql_query("""SELECT l.zeitpunkt, a.name, l.menge, l.typ, l.benutzer FROM lager_log l 
                           JOIN artikel a ON l.artikel_id = a.id ORDER BY l.id DESC""", conn)
    
    st.dataframe(df, use_container_width=True)
    if not df.empty:
        st.download_button("📥 Als Excel exportieren", to_excel(df), "Lagerbericht.xlsx")
    conn.close()

def zeige_kundenverwaltung():
    st.subheader("👥 Kundenverwaltung")
    with st.form("new_kunde"):
        nr, name = st.columns(2)
        k_nr = nr.text_input("Kundennummer")
        k_name = name.text_input("Firma/Name")
        mail, pw = st.columns(2)
        k_mail = mail.text_input("Email")
        k_pw = pw.text_input("Passwort", type="password")
        if st.form_submit_button("Kunde anlegen"):
            conn = get_connection()
            conn.execute("INSERT INTO kunden (kunden_nr, name, email, passwort_hash) VALUES (?,?,?,?)",
                         (k_nr, k_name, k_mail, hash_password(k_pw)))
            conn.commit()
            conn.close()
            st.success("Kunde erstellt!")

# -------------------------------------------------
# Main Logic
# -------------------------------------------------
def main():
    st.set_page_config(page_title="Lager Pro 2026", layout="wide")
    init_db()
    
    if "internal_logged_in" not in st.session_state:
        st.session_state.internal_logged_in = False

    if not st.session_state.internal_logged_in:
        st.title("📦 Lager-Login")
        with st.form("login"):
            u = st.text_input("Benutzer")
            p = st.text_input("Passwort", type="password")
            if st.form_submit_button("Anmelden"):
                conn = get_connection()
                user = conn.execute("SELECT * FROM internal_users WHERE username=? AND passwort_hash=?", (u, hash_password(p))).fetchone()
                if user:
                    st.session_state.internal_logged_in = True
                    st.session_state.internal_user = dict(user)
                    st.rerun()
                else: st.error("Falsche Daten")
        return

    # Sidebar Navigation
    menu = st.sidebar.radio("Navigation", list(MENU_LABELS.values()))
    
    if menu == MENU_LABELS["dashboard"]: zeige_dashboard()
    elif menu == MENU_LABELS["scanner_terminal"]: zeige_scanner_terminal()
    elif menu == MENU_LABELS["reporting"]: zeige_reporting()
    elif menu == MENU_LABELS["kundenverwaltung"]: zeige_kundenverwaltung()
    elif menu == MENU_LABELS["artikel_anlegen"]:
        # Vereinfachtes Artikel-Formular (Integration Stück/Pack/Palette)
        st.subheader("➕ Artikel anlegen")
        with st.form("art_anlegen"):
            art_nr = st.text_input("Artikelnummer")
            art_name = st.text_input("Name")
            in_p = st.number_input("Stück pro Pack", value=10)
            in_pal = st.number_input("Packs pro Palette", value=50)
            if st.form_submit_button("Speichern"):
                conn = get_connection()
                conn.execute("INSERT INTO artikel (artikelnummer, name, inhalt_pro_pack, packs_pro_palette) VALUES (?,?,?,?)",
                             (art_nr, art_name, in_p, in_pal))
                conn.commit()
                conn.close()
                st.success("Artikel angelegt")
    
    if st.sidebar.button("Logout"):
        st.session_state.clear()
        st.rerun()

if __name__ == "__main__":
    main()
