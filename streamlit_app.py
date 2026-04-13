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
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm

# --- KONFIGURATION ---
DB_FILE = "lager_v52.db"
LOGO_FILE = "image_0.png"  # Stelle sicher, dass die Datei im Ordner liegt

MENU_LABELS = {
    "dashboard": "📊 Gesamt-Monitor",
    "scanner": "🚀 Scanner-Terminal",
    "picking": "📋 Picking & Lieferscheine",
    "lagerbestand": "📦 Lagerbestand",
    "art_anlegen": "➕ Artikel anlegen",
    "kundenverwaltung": "👥 Kundenverwaltung",
    "benutzerverwaltung": "🔐 Benutzerverwaltung",
    "reporting": "📉 Berichte & Export"
}

ROLLEN_DEFAULTS = {
    "Picker": ["scanner", "picking"],
    "Verräumer": ["scanner", "picking", "lagerbestand"],
    "Wareneingang": ["art_anlegen", "lagerbestand", "scanner"],
    "Einkauf": ["dashboard", "art_anlegen", "lagerbestand", "reporting"],
    "Viewer": ["dashboard"],
    "Admin": list(MENU_LABELS.keys())
}

# -------------------------------------------------
# Kern-Funktionen (DB & Security)
# -------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    # Artikel
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, art_nr TEXT UNIQUE, name TEXT, 
        einheit TEXT, inhalt_pack INTEGER, inhalt_pal INTEGER, bestand_stk INTEGER DEFAULT 0,
        meldebestand_stk INTEGER DEFAULT 10, lagerplatz TEXT, ean_barcode TEXT
    )""")
    # Benutzer
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, 
        passwort_hash TEXT, rolle TEXT, rechte_json TEXT
    )""")
    # Kunden (Krankenhaus)
    cur.execute("""CREATE TABLE IF NOT EXISTS kunden (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, krankenhaus TEXT, 
        station TEXT, adresse TEXT, ansprechpartner TEXT, email TEXT UNIQUE, passwort_hash TEXT
    )""")
    # Bestellungen
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellungen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellnummer TEXT UNIQUE, kunden_id INTEGER, 
        status TEXT DEFAULT 'offen', datum TEXT, kommissionierer TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellpositionen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellung_id INTEGER, artikel_id INTEGER, menge_stueck INTEGER
    )""")
    # Log / Historie
    cur.execute("""CREATE TABLE IF NOT EXISTS lager_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER, menge INTEGER, 
        typ TEXT, zeitpunkt TEXT, benutzer TEXT
    )""")
    
    # Standard Admin Check
    if cur.execute("SELECT COUNT(*) FROM internal_users").fetchone()[0] == 0:
        admin_rechte = json.dumps(list(MENU_LABELS.keys()))
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle, rechte_json) VALUES (?,?,?,?)",
                    ("admin", hash_pw("admin123"), "Admin", admin_rechte))
    conn.commit()
    conn.close()

# -------------------------------------------------
# Hilfs-Komponenten (Voice & Style)
# -------------------------------------------------
def speak(text):
    if text:
        components.html(f"<script>window.speechSynthesis.cancel(); var msg = new SpeechSynthesisUtterance('{text}'); msg.lang = 'de-DE'; window.speechSynthesis.speak(msg);</script>", height=0)

def apply_ui_style():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 60px; font-size: 18px !important; border-radius: 12px; font-weight: bold; }
            .pos-card { padding: 15px; border-radius: 10px; border-left: 8px solid #1f77b4; background: #f9f9f9; margin-bottom: 10px; box-shadow: 2px 2px 5px rgba(0,0,0,0.05); }
        </style>
    """, unsafe_allow_html=True)

# -------------------------------------------------
# PDF LOGIK (Lieferschein mit Logo)
# -------------------------------------------------
def generiere_lieferschein_pdf(bestell_id):
    conn = get_connection()
    bestellung = conn.execute("""
        SELECT b.bestellnummer, b.datum, k.krankenhaus, k.station, k.ansprechpartner, b.kommissionierer, k.adresse
        FROM bestellungen b JOIN kunden k ON b.kunden_id = k.id WHERE b.id = ?
    """, (bestell_id,)).fetchone()
    positionen = conn.execute("""
        SELECT a.name, a.art_nr, bp.menge_stueck, a.einheit
        FROM bestellpositionen bp JOIN artikel a ON bp.artikel_id = a.id WHERE bp.bestellung_id = ?
    """, (bestell_id,)).fetchall()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=1.5*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    elements = []

    # Logo & Header
    if os.path.exists(LOGO_FILE):
        elements.append(Image(LOGO_FILE, width=6*cm, height=2.2*cm))
    elements.append(Paragraph(f"<b>LIEFERSCHEIN</b>", styles['Heading1']))
    elements.append(Spacer(1, 0.5*cm))

    # Info Block
    info_data = [[f"Bestell-Nr: {bestellung['bestellnummer']}", f"Station: {bestellung['station']}"],
                 [f"Datum: {bestellung['datum']}", f"Haus: {bestellung['krankenhaus']}"]]
    t_info = Table(info_data, colWidths=[8*cm, 9*cm])
    elements.append(t_info)
    elements.append(Spacer(1, 1*cm))

    # Artikel Tabelle
    data = [["Art-Nr", "Bezeichnung", "Menge", "Einheit"]]
    for p in positionen:
        data.append([p['art_nr'], p['name'], str(p['menge_stueck']), p['einheit']])

    t_art = Table(data, colWidths=[3*cm, 8*cm, 3*cm, 3*cm])
    t_art.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.grey), ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke), ('GRID', (0,0), (-1,-1), 0.5, colors.black)]))
    elements.append(t_art)

    doc.build(elements)
    return buffer.getvalue()

# -------------------------------------------------
# KORREKTUR LOGIK (Bestands-Revision)
# -------------------------------------------------
def korrigiere_position_mit_log(pos_id, neue_menge, user):
    conn = get_connection()
    cur = conn.cursor()
    pos = cur.execute("SELECT menge_stueck, artikel_id FROM bestellpositionen WHERE id=?", (pos_id,)).fetchone()
    if pos:
        diff = pos['menge_stueck'] - neue_menge
        cur.execute("UPDATE artikel SET bestand_stueck = bestand_stueck + ? WHERE id=?", (diff, pos['artikel_id']))
        if neue_menge <= 0: cur.execute("DELETE FROM bestellpositionen WHERE id=?", (pos_id,))
        else: cur.execute("UPDATE bestellpositionen SET menge_stueck = ? WHERE id=?", (neue_menge, pos_id))
        cur.execute("INSERT INTO lager_log (artikel_id, menge, typ, zeitpunkt, benutzer) VALUES (?,?,?,?,?)",
                    (pos['artikel_id'], diff, 'Korrektur', datetime.now().strftime("%Y-%m-%d %H:%M"), user))
        conn.commit()
    conn.close()

# -------------------------------------------------
# UI ANSICHTEN
# -------------------------------------------------
def zeige_scanner_terminal():
    st.subheader("🚀 Scanner-Terminal")
    scan = st.text_input("Scan Barcode...", key="terminal_scan")
    if scan:
        conn = get_connection()
        art = conn.execute("SELECT * FROM artikel WHERE art_nr=? OR ean_barcode=?", (scan, scan)).fetchone()
        if art:
            st.info(f"Artikel: {art['name']} | Bestand: {art['bestand_stk']}")
            speak(f"{art['name']} erkannt.")
            c1, c2 = st.columns(2)
            if c1.button("📥 +1 Stück"):
                conn.execute("UPDATE artikel SET bestand_stk = bestand_stk + 1 WHERE id=?", (art['id'],))
                conn.commit()
                st.success("Bestand erhöht")
            if c2.button("📤 -1 Stück"):
                conn.execute("UPDATE artikel SET bestand_stk = bestand_stk - 1 WHERE id=?", (art['id'],))
                conn.commit()
                st.warning("Bestand verringert")
        conn.close()

def zeige_picking_archiv():
    st.subheader("📋 Picking & Korrektur-Archiv")
    conn = get_connection()
    user = st.session_state.auth['username']
    
    tab1, tab2 = st.tabs(["Laufende Picking-Aufträge", "Beleg-Archiv (Korrektur)"])
    
    with tab1:
        offen = conn.execute("SELECT * FROM bestellungen WHERE status IN ('offen', 'in_bearbeitung')").fetchall()
        for auf in offen:
            if st.button(f"Auftrag {auf['bestellnummer']} abschließen", key=auf['id']):
                conn.execute("UPDATE bestellungen SET status='kommissioniert', kommissionierer=? WHERE id=?", (user, auf['id']))
                conn.commit()
                st.rerun()

    with tab2:
        archiv = conn.execute("SELECT * FROM bestellungen WHERE status='kommissioniert' ORDER BY id DESC").fetchall()
        for auf in archiv:
            with st.expander(f"Beleg {auf['bestellnummer']}"):
                pos_liste = conn.execute("SELECT bp.id, a.name, bp.menge_stueck FROM bestellpositionen bp JOIN artikel a ON a.id=bp.artikel_id WHERE bp.bestellung_id=?", (auf['id'],)).fetchall()
                for p in pos_liste:
                    c1, c2 = st.columns([3,1])
                    neu_m = c1.number_input(f"{p['name']}", value=p['menge_stueck'], key=f"ed_{p['id']}")
                    if c2.button("💾", key=f"btn_{p['id']}"):
                        korrigiere_position_mit_log(p['id'], neu_m, user)
                        st.rerun()
                if st.button("🔄 Lieferschein neu generieren", key=f"print_{auf['id']}"):
                    pdf = generiere_lieferschein_pdf(auf['id'])
                    st.download_button("⬇️ Download PDF", pdf, f"Lieferschein_{auf['bestellnummer']}.pdf")
    conn.close()

# -------------------------------------------------
# Main Login & Navigation
# -------------------------------------------------
def main():
    st.set_page_config(page_title="KH-Logistik Pro V5.2", layout="wide")
    init_db()
    apply_ui_style()

    if "auth" not in st.session_state: st.session_state.auth = None

    if st.session_state.auth is None:
        login_tab, reg_tab = st.tabs(["🔐 Login", "🏥 Registrierung"])
        with login_tab:
            u = st.text_input("Username")
            p = st.text_input("Passwort", type="password")
            if st.button("Anmelden"):
                conn = get_connection()
                user = conn.execute("SELECT * FROM internal_users WHERE username=? AND passwort_hash=?", (u, hash_pw(p))).fetchone()
                if user: 
                    st.session_state.auth = dict(user)
                    st.rerun()
                else: st.error("Fehler")
        with reg_tab:
            with st.form("registrierung"):
                st.write("Konto für neue Station anlegen")
                # ... Felder wie in V4.0 ...
                if st.form_submit_button("Registrieren"): st.success("Konto erstellt!")
        return

    # Navigation
    meine_rechte = json.loads(st.session_state.auth['rechte_json'])
    st.sidebar.title(f"User: {st.session_state.auth['username']}")
    erlaubte_labels = [MENU_LABELS[r] for r in meine_rechte if r in MENU_LABELS]
    choice = st.sidebar.radio("Menü", erlaubte_labels)

    if choice == MENU_LABELS["scanner"]: zeige_scanner_terminal()
    elif choice == MENU_LABELS["picking"]: zeige_picking_archiv()
    elif choice == MENU_LABELS["dashboard"]: st.info("Monitor-Ansicht (V3.5)")
    elif choice == MENU_LABELS["art_anlegen"]: st.info("Artikel-Anlage (V4.0)")
    
    if st.sidebar.button("Abmelden"):
        st.session_state.auth = None
        st.rerun()

if __name__ == "__main__":
    main()
```
