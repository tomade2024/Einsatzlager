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
DB_FILE = "lager_v35.db"
BACKUP_DIR = "backups"

LAGER = ["Medizinlager", "Verbrauchslager", "Materiallager", "Techniklager", "Möbellager", "Lebensmittellager", "Textillager"]
ROLLEN = ["Admin", "Lagerist", "Vertrieb"]
BESTELLSTATUS = ["offen", "in_bearbeitung", "kommissioniert", "verladen", "geliefert", "storniert"]

MENU_LABELS = {
    "dashboard": "📊 Gesamt-Monitor",
    "lagerbestand": "📦 Lagerbestand",
    "scanner_terminal": "🚀 Scanner-Terminal",
    "bestellungen": "📋 Picking / Aufträge",
    "wareneingang": "📥 Wareneingang",
    "kundenverwaltung": "👥 Kundenverwaltung (Admin)",
    "benutzerverwaltung": "🔐 Benutzerverwaltung (Admin)",
    "backup": "💾 Backup & Restore",
}

# -------------------------------------------------
# Hilfsfunktionen & Styling
# -------------------------------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def apply_mobile_styles():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 60px; font-size: 18px !important; border-radius: 12px; font-weight: bold; }
            .pos-card { padding: 15px; border-radius: 10px; border-left: 5px solid #1f77b4; background: #f9f9f9; margin-bottom: 10px; box-shadow: 2px 2px 5px rgba(0,0,0,0.05); }
            .monitor-box { padding: 20px; border-radius: 10px; color: white; text-align: center; margin-bottom: 10px; }
        </style>
    """, unsafe_allow_html=True)

def speak(text):
    if text:
        components.html(f"<script>window.speechSynthesis.cancel(); var msg = new SpeechSynthesisUtterance('{text}'); msg.lang = 'de-DE'; window.speechSynthesis.speak(msg);</script>", height=0)

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
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikelnummer TEXT UNIQUE, name TEXT, lager TEXT, 
        bestand_stueck INTEGER DEFAULT 0, meldebestand_stueck INTEGER DEFAULT 10, zielbestand_stueck INTEGER DEFAULT 50,
        ean_barcode TEXT, lagerplatz TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kunden (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kunden_nr TEXT UNIQUE, name TEXT, email TEXT, passwort_hash TEXT, aktiv INTEGER DEFAULT 1
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, passwort_hash TEXT, rolle TEXT, ist_aktiv INTEGER DEFAULT 1
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellungen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellnummer TEXT, kunden_id INTEGER, status TEXT DEFAULT 'offen', datum TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellpositionen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellung_id INTEGER, artikel_id INTEGER, menge_stueck INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kommissionierung_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellposition_id INTEGER, menge_kommissioniert INTEGER, zeitpunkt TEXT
    )""")
    # Admin & Test-Daten
    cur.execute("SELECT COUNT(*) FROM internal_users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle) VALUES (?,?,?)", ("admin", hash_password("admin123"), "Admin"))
    conn.commit()
    conn.close()

# -------------------------------------------------
# Monitor / Dashboard Logik
# -------------------------------------------------
def zeige_dashboard():
    st.title("📊 Logistik-Monitor")
    conn = get_connection()
    
    # 1. Bestands-Monitor (Einkauf)
    st.subheader("🛒 Einkaufs-Monitor")
    df_art = pd.read_sql_query("SELECT name, bestand_stueck, meldebestand_stueck, zielbestand_stueck FROM artikel", conn)
    muss_bestellt_werden = df_art[df_art['bestand_stueck'] <= df_art['meldebestand_stueck']]
    
    c1, c2 = st.columns(2)
    with c1:
        st.error(f"Kritisch: {len(muss_bestellt_werden)} Artikel unter Meldebestand")
        if not muss_bestellt_werden.empty:
            st.dataframe(muss_bestellt_werden[['name', 'bestand_stueck', 'zielbestand_stueck']], use_container_width=True)
    with c2:
        offene_bestellungen = pd.read_sql_query("SELECT bestellnummer, status FROM bestellungen WHERE status='offen'", conn)
        st.info(f"Offene Kundenaufträge: {len(offene_bestellungen)}")
        st.dataframe(offene_bestellungen, use_container_width=True)

    st.divider()

    # 2. Kommissionier-Monitor
    st.subheader("📦 Kommissionier-Status")
    bestell_df = pd.read_sql_query("SELECT id, bestellnummer, status FROM bestellungen", conn)
    
    k1, k2, k3 = st.columns(3)
    with k1:
        st.markdown("<div class='monitor-box' style='background-color: #ff4b4b;'><h3>Neu / Offen</h3></div>", unsafe_allow_html=True)
        neu = bestell_df[bestell_df['status'] == 'offen']
        st.write(neu[['bestellnummer']])
    with k2:
        st.markdown("<div class='monitor-box' style='background-color: #ffa500;'><h3>In Bearbeitung</h3></div>", unsafe_allow_html=True)
        laufend = bestell_df[bestell_df['status'] == 'in_bearbeitung']
        st.write(laufend[['bestellnummer']])
    with k3:
        st.markdown("<div class='monitor-box' style='background-color: #28a745;'><h3>Fertig</h3></div>", unsafe_allow_html=True)
        fertig = bestell_df[bestell_df['status'] == 'kommissioniert']
        st.write(fertig[['bestellnummer']])
    conn.close()

# -------------------------------------------------
# Kunden- & Benutzerverwaltung (Admin)
# -------------------------------------------------
def zeige_kundenverwaltung():
    st.subheader("👥 Kundenverwaltung")
    with st.expander("➕ Neuen Kunden anlegen"):
        with st.form("neuer_kunde"):
            k_nr = st.text_input("Kundennummer (z.B. K1000)")
            k_name = st.text_input("Name / Firma")
            k_email = st.text_input("Email")
            k_pw = st.text_input("Passwort", type="password")
            if st.form_submit_button("Kunde speichern"):
                conn = get_connection()
                try:
                    conn.execute("INSERT INTO kunden (kunden_nr, name, email, passwort_hash) VALUES (?,?,?,?)",
                                 (k_nr, k_name, k_email, hash_password(k_pw)))
                    conn.commit()
                    st.success(f"Kunde {k_name} angelegt!")
                except: st.error("Fehler: Kundennummer oder Email existiert bereits.")
                finally: conn.close()
    
    conn = get_connection()
    kunden = pd.read_sql_query("SELECT kunden_nr, name, email, aktiv FROM kunden", conn)
    st.dataframe(kunden, use_container_width=True)
    conn.close()

def zeige_benutzerverwaltung():
    st.subheader("🔐 Interne Benutzerverwaltung")
    with st.expander("➕ Neuen internen Benutzer anlegen"):
        with st.form("neuer_user"):
            u_name = st.text_input("Benutzername")
            u_rolle = st.selectbox("Rolle", ROLLEN)
            u_pw = st.text_input("Passwort", type="password")
            if st.form_submit_button("Benutzer speichern"):
                conn = get_connection()
                try:
                    conn.execute("INSERT INTO internal_users (username, passwort_hash, rolle) VALUES (?,?,?)",
                                 (u_name, hash_password(u_pw), u_rolle))
                    conn.commit()
                    st.success(f"Benutzer {u_name} angelegt!")
                except: st.error("Benutzername existiert bereits.")
                finally: conn.close()
    
    conn = get_connection()
    users = pd.read_sql_query("SELECT username, rolle, ist_aktiv FROM internal_users", conn)
    st.dataframe(users, use_container_width=True)
    conn.close()

# -------------------------------------------------
# Main Navigation
# -------------------------------------------------
def main():
    st.set_page_config(page_title="Einsatzlager Pro 2026", layout="wide")
    init_db()
    apply_mobile_styles()
    
    if "internal_logged_in" not in st.session_state:
        st.session_state.internal_logged_in = False

    if not st.session_state.internal_logged_in:
        st.title("📦 Lagerwirtschaft Login")
        with st.form("login"):
            u = st.text_input("Benutzer")
            p = st.text_input("Passwort", type="password")
            if st.form_submit_button("Login"):
                conn = get_connection()
                user = conn.execute("SELECT * FROM internal_users WHERE username=? AND passwort_hash=?", (u, hash_password(p))).fetchone()
                if user:
                    st.session_state.internal_logged_in = True
                    st.session_state.internal_user = dict(user)
                    st.rerun()
                else: st.error("Login fehlgeschlagen")
        return

    # Navigation
    menu = st.sidebar.radio("Navigation", list(MENU_LABELS.values()))
    
    if menu == MENU_LABELS["dashboard"]: zeige_dashboard()
    elif menu == MENU_LABELS["kundenverwaltung"]: zeige_kundenverwaltung()
    elif menu == MENU_LABELS["benutzerverwaltung"]: zeige_benutzerverwaltung()
    # Hier folgen die restlichen Platzhalter...
    else: st.info(f"Bereich {menu} wird geladen...")

    if st.sidebar.button("Abmelden"):
        st.session_state.clear()
        st.rerun()

if __name__ == "__main__":
    main()
