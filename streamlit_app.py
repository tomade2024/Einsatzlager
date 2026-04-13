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

# PDF-Bibliotheken
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm

# --- KONFIGURATION ---
DB_FILE = "lager_v52_final.db"
LOGO_FILE = "image_0.png" # Stelle sicher, dass die Datei im gleichen Ordner liegt

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
# Datenbank & Sicherheit
# -------------------------------------------------
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

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
    # Historie Log
    cur.execute("""CREATE TABLE IF NOT EXISTS lager_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER, menge INTEGER, 
        typ TEXT, zeitpunkt TEXT, benutzer TEXT
    )""")
    
    # Admin-User Initialisierung
    if cur.execute("SELECT COUNT(*) FROM internal_users").fetchone()[0] == 0:
        admin_rechte = json.dumps(list(MENU_LABELS.keys()))
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle, rechte_json) VALUES (?,?,?,?)",
                    ("admin", hash_pw("admin123"), "Admin", admin_rechte))
    conn.commit()
    conn.close()

# -------------------------------------------------
# Hilfsfunktionen
# -------------------------------------------------
def speak(text):
    if text:
        components.html(f"<script>window.speechSynthesis.cancel(); var msg = new SpeechSynthesisUtterance('{text}'); msg.lang = 'de-DE'; window.speechSynthesis.speak(msg);</script>", height=0)

def apply_ui_style():
    st.markdown("""
        <style>
            .stButton > button { width: 100%; height: 60px; font-size: 18px !important; border-radius: 12px; font-weight: bold; }
            .pos-card { padding: 20px; border-radius: 12px; border-left: 10px solid #1f77b4; background: #f0f2f6; margin-bottom: 15px; }
        </style>
    """, unsafe_allow_html=True)

# -------------------------------------------------
# Lieferschein & Korrektur Logik
# -------------------------------------------------
def korrigiere_position_mit_log(pos_id, neue_menge, user):
    conn = get_connection()
    cur = conn.cursor()
    pos = cur.execute("SELECT menge_stueck, artikel_id FROM bestellpositionen WHERE id=?", (pos_id,)).fetchone()
    if pos:
        diff = pos['menge_stueck'] - neue_menge
        cur.execute("UPDATE artikel SET bestand_stk = bestand_stk + ? WHERE id=?", (diff, pos['artikel_id']))
        if neue_menge <= 0: cur.execute("DELETE FROM bestellpositionen WHERE id=?", (pos_id,))
        else: cur.execute("UPDATE bestellpositionen SET menge_stueck = ? WHERE id=?", (neue_menge, pos_id))
        cur.execute("INSERT INTO lager_log (artikel_id, menge, typ, zeitpunkt, benutzer) VALUES (?,?,?,?,?)",
                    (pos['artikel_id'], diff, 'Korrektur', datetime.now().strftime("%Y-%m-%d %H:%M"), user))
        conn.commit()
    conn.close()

def generiere_lieferschein_pdf(bestell_id):
    conn = get_connection()
    b = conn.execute("SELECT b.*, k.* FROM bestellungen b JOIN kunden k ON b.kunden_id = k.id WHERE b.id=?", (bestell_id,)).fetchone()
    positionen = conn.execute("SELECT a.name, a.art_nr, bp.menge_stueck, a.einheit FROM bestellpositionen bp JOIN artikel a ON a.id = bp.artikel_id WHERE bp.bestellung_id=?", (bestell_id,)).fetchall()
    conn.close()
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []
    
    if os.path.exists(LOGO_FILE):
        elements.append(Image(LOGO_FILE, width=6*cm, height=2.2*cm))
    
    elements.append(Paragraph(f"LIEFERSCHEIN {b['bestellnummer']}", styles['Title']))
    elements.append(Paragraph(f"Station: {b['station']} | Haus: {b['krankenhaus']}<br/>Datum: {b['datum']}", styles['Normal']))
    elements.append(Spacer(1, 1*cm))
    
    data = [["Art-Nr", "Bezeichnung", "Menge", "Einheit"]]
    for p in positionen:
        data.append([p['art_nr'], p['name'], str(p['menge_stueck']), p['einheit']])
        
    t = Table(data, colWidths=[3*cm, 8*cm, 2.5*cm, 3*cm])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0), colors.grey), ('TEXTCOLOR',(0,0),(-1,0), colors.whitesmoke), ('GRID',(0,0),(-1,-1),0.5,colors.black)]))
    elements.append(t)
    doc.build(elements)
    return buffer.getvalue()

# -------------------------------------------------
# UI SEKTIONEN
# -------------------------------------------------
def zeige_art_anlegen():
    st.subheader("➕ Neuen Artikel anlegen")
    with st.form("art_form"):
        art_nr = st.text_input("Artikelnummer")
        name = st.text_input("Bezeichnung")
        einheit = st.selectbox("Einheit", ["Stück", "Pack", "Palette"])
        c1, c2 = st.columns(2)
        in_pack = c1.number_input("Stück pro Pack", value=10)
        in_pal = c2.number_input("Packs pro Palette", value=50)
        if st.form_submit_button("Speichern"):
            conn = get_connection()
            try:
                conn.execute("INSERT INTO artikel (art_nr, name, einheit, inhalt_pack, inhalt_pal) VALUES (?,?,?,?,?)",
                             (art_nr, name, einheit, in_pack, in_pal))
                conn.commit()
                st.success("Artikel angelegt!")
            except: st.error("Fehler: Art-Nr existiert bereits.")
            conn.close()

def zeige_scanner():
    st.subheader("🚀 Scanner Terminal")
    scan = st.text_input("Barcode scannen...", key="term_scan")
    if scan:
        conn = get_connection()
        art = conn.execute("SELECT * FROM artikel WHERE art_nr=? OR ean_barcode=?", (scan, scan)).fetchone()
        if art:
            st.markdown(f"<div class='pos-card'><b>Gefunden: {art['name']}</b><br/>Bestand: {art['bestand_stk']}</div>", unsafe_allow_html=True)
            speak(f"{art['name']} erkannt.")
            c1, c2, c3 = st.columns(3)
            if c1.button("📥 +1 Stück"):
                conn.execute("UPDATE artikel SET bestand_stk = bestand_stk + 1 WHERE id=?", (art['id'],))
                conn.commit()
                st.rerun()
            if c2.button("📤 -1 Stück"):
                conn.execute("UPDATE artikel SET bestand_stk = bestand_stk - 1 WHERE id=?", (art['id'],))
                conn.commit()
                st.rerun()
        else: st.error("Unbekannt")
        conn.close()

def zeige_picking():
    st.subheader("📋 Picking & Korrektur")
    conn = get_connection()
    user = st.session_state.auth['username']
    tab1, tab2 = st.tabs(["Offene Aufträge", "Archiv & Korrektur"])
    
    with tab1:
        offen = conn.execute("SELECT * FROM bestellungen WHERE status != 'kommissioniert'").fetchall()
        for b in offen:
            if st.button(f"Auftrag {b['bestellnummer']} abschließen", key=b['id']):
                conn.execute("UPDATE bestellungen SET status='kommissioniert', kommissionierer=? WHERE id=?", (user, b['id']))
                conn.commit()
                st.rerun()
                
    with tab2:
        archiv = conn.execute("SELECT * FROM bestellungen WHERE status='kommissioniert' ORDER BY id DESC").fetchall()
        for b in archiv:
            with st.expander(f"📦 {b['bestellnummer']}"):
                pos = conn.execute("SELECT bp.id, a.name, bp.menge_stueck FROM bestellpositionen bp JOIN artikel a ON a.id=bp.artikel_id WHERE bp.bestellung_id=?", (b['id'],)).fetchall()
                for p in pos:
                    c1, c2 = st.columns([3,1])
                    neu_m = c1.number_input(f"{p['name']}", value=p['menge_stueck'], key=f"ed_{p['id']}")
                    if c2.button("💾", key=f"btn_{p['id']}"):
                        korrigiere_position_mit_log(p['id'], neu_m, user)
                        st.rerun()
                if st.button("🔄 Lieferschein drucken", key=f"pr_{b['id']}"):
                    st.download_button("Download PDF", generiere_lieferschein_pdf(b['id']), f"Lieferschein_{b['bestellnummer']}.pdf")
    conn.close()

# -------------------------------------------------
# Main Login & Navigation
# -------------------------------------------------
def main():
    st.set_page_config(page_title="KH-Logistik 5.2", layout="wide")
    init_db()
    apply_ui_style()

    if "auth" not in st.session_state: st.session_state.auth = None

    if st.session_state.auth is None:
        t1, t2 = st.tabs(["🔐 Login", "🏥 Registrierung"])
        with t1:
            u = st.text_input("User")
            p = st.text_input("Passwort", type="password")
            if st.button("Anmelden"):
                conn = get_connection()
                user = conn.execute("SELECT * FROM internal_users WHERE username=? AND passwort_hash=?", (u, hash_pw(p))).fetchone()
                if user:
                    st.session_state.auth = dict(user)
                    st.rerun()
                else: st.error("Fehler")
        with t2:
            with st.form("reg"):
                name = st.text_input("Vollständiger Name")
                kh = st.text_input("Krankenhaus")
                st_name = st.text_input("Station")
                mail = st.text_input("E-Mail")
                if st.form_submit_button("Registrieren"): st.success("Registrierung erhalten.")
        return

    # Dynamische Navigation
    meine_rechte = json.loads(st.session_state.auth['rechte_json'])
    st.sidebar.title(f"User: {st.session_state.auth['username']}")
    erlaubte_labels = [MENU_LABELS[r] for r in meine_rechte if r in MENU_LABELS]
    choice = st.sidebar.radio("Navigation", erlaubte_labels)

    if choice == MENU_LABELS["scanner"]: zeige_scanner()
    elif choice == MENU_LABELS["picking"]: zeige_picking()
    elif choice == MENU_LABELS["art_anlegen"]: zeige_art_anlegen()
    elif choice == MENU_LABELS["dashboard"]: st.info("Monitor-Ansicht")
    # Weitere Sektionen hier einbinden...

    if st.sidebar.button("Logout"):
        st.session_state.auth = None
        st.rerun()

if __name__ == "__main__":
    main()
