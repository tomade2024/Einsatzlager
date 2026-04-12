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
    "wareneingang": "📥 Wareneingang (Manuell)",
    "chargen_mhd": "🏷️ Chargen / MHD",
    "bestandswarnliste": "⚠️ Warnliste",
    "nachbestellliste": "🛒 Nachbestellliste",
    "tv_monitor": "📺 TV-Monitor",
    "artikel_anlegen": "➕ Artikel anlegen",
    "artikel_bearbeiten": "✏️ Bearbeiten / Löschen",
    "kundenverwaltung": "👥 Kundenverwaltung",
    "benutzerverwaltung": "🔐 Benutzerverwaltung",
    "backup": "💾 Backup & Restore",
}

# -------------------------------------------------
# Hilfsfunktionen & UI-Styling
# -------------------------------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def get_now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def apply_mobile_styles():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 70px; font-size: 20px !important; border-radius: 15px; font-weight: bold; }
            .stTextInput input { height: 60px; font-size: 22px !important; }
            .pos-card { padding: 20px; border-radius: 12px; border-left: 8px solid #1f77b4; background: #f0f2f6; margin-bottom: 15px; }
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
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikelnummer TEXT UNIQUE, name TEXT, lager TEXT, 
        verpackung_typ TEXT, inhalt_pro_pack INTEGER DEFAULT 10, packs_pro_palette INTEGER DEFAULT 10, 
        bestand_stueck INTEGER DEFAULT 0, reserviert_stueck INTEGER DEFAULT 0,
        mindestbestand_stueck INTEGER DEFAULT 0, meldebestand_stueck INTEGER DEFAULT 0, 
        zielbestand_stueck INTEGER DEFAULT 0, ean_barcode TEXT, hersteller TEXT, einheit TEXT DEFAULT 'Stück',
        lagerplatz TEXT, nachschub_lagerplatz TEXT, lieferant TEXT, lieferanten_artikelnummer TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel_chargen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER, chargennummer TEXT, 
        chargenbarcode TEXT UNIQUE, mhd_datum TEXT, ausgabe_bis TEXT, bestand_stueck INTEGER DEFAULT 0, 
        lagerplatz TEXT, nachschub_lagerplatz TEXT, wareneingang_datum TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kommissionierung_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellposition_id INTEGER, charge_id INTEGER, 
        menge_kommissioniert INTEGER, zeitpunkt TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellungen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellnummer TEXT, kunden_id INTEGER, kunde_name TEXT, 
        lieferadresse TEXT, datum TEXT, uhrzeit TEXT, status TEXT DEFAULT 'offen', status_geaendert_am TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellpositionen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellung_id INTEGER, artikel_id INTEGER, menge_stueck INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, passwort_hash TEXT, 
        rolle TEXT, menu_rights_json TEXT, erstellt_am TEXT, ist_aktiv INTEGER DEFAULT 1
    )""")
    
    cur.execute("SELECT COUNT(*) FROM internal_users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle, erstellt_am) VALUES (?,?,?,?)",
                    ("admin", hash
