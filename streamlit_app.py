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
DB_FILE = "lager_v52_final.db"
LOGO_FILE = "image_0.png"

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
# Hilfsfunktionen & Datenbank
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
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, art_nr TEXT UNIQUE, name TEXT, 
        einheit TEXT, inhalt_pack INTEGER, inhalt_pal INTEGER, bestand_stk INTEGER DEFAULT 0,
        meldebestand_stk INTEGER DEFAULT 10, lagerplatz TEXT, ean_barcode TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, 
        passwort_hash TEXT, rolle TEXT, rechte_json TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kunden (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, krankenhaus TEXT, 
        station TEXT, adresse TEXT, ansprechpartner TEXT, email TEXT UNIQUE, passwort_hash TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellungen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellnummer TEXT UNIQUE, kunden_id INTEGER, 
        status TEXT DEFAULT 'offen', datum TEXT, kommissionierer TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bestellpositionen (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bestellung_id INTEGER, artikel_id INTEGER, menge_stueck INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS lager_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, artikel_id INTEGER, menge INTEGER, 
        typ TEXT, zeitpunkt TEXT, benutzer TEXT
    )""")
    if cur.execute("SELECT COUNT(*) FROM internal_users").fetchone()[0] == 0:
        admin_rechte = json.dumps(list(MENU_LABELS.keys()))
        cur.execute("INSERT INTO internal_users (username, passwort_hash, rolle, rechte_json) VALUES (?,?,?,?)",
                    ("admin", hash_pw("admin123"), "Admin", admin_rechte))
    conn.commit()
    conn.close()

def speak(text):
    if text:
        components.html(f"<script>window.speechSynthesis.cancel(); var msg = new SpeechSynthesisUtterance('{text}'); msg.lang = 'de-DE'; window.speechSynthesis.speak(msg);</script>", height=0)

# -------------------------------------------------
# Business Logik
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
    p = conn.execute("SELECT a.name, a.art_nr, bp.menge_stueck, a.einheit FROM bestellpositionen bp JOIN artikel a ON bp.artikel_id = a.id WHERE bp.bestellung_id=?", (bestell_id,)).fetchall()
    conn.close()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []
    if os.path.exists(LOGO_FILE): elements.append(Image(LOGO_FILE, width=5*cm, height=2*cm))
    elements.append(Paragraph(f"LIEFERSCHEIN {b['bestellnummer']}", styles['Title']))
    elements.append(Paragraph(f"Station: {b['station']} | Haus: {b['krankenhaus']}", styles['Normal']))
    data = [["Art-Nr", "Bezeichnung", "Menge", "Einheit"]]
    for pos in p: data.append([pos['art_nr'], pos['name'], str(pos['menge_stueck']), pos['einheit']])
    t = Table(data, colWidths=[3*cm, 8*cm, 2*cm, 3*cm])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0), colors.grey),('GRID',(0,0),(-1,-1),0.5,colors.black)]))
    elements.append(t)
    doc.build(elements)
    return buffer.getvalue()

# -------------------------------------------------
# UI Sektionen
# -------------------------------------------------
def zeige_scanner():
    st.subheader("🚀 Scanner Terminal")
    scan = st.text_input("Barcode scannen...", key="term_scan")
    if scan:
        conn = get_connection()
        art = conn.execute("SELECT * FROM artikel WHERE art_nr=? OR ean_barcode=?", (scan, scan)).fetchone()
        if art:
            st.info(f"Gefunden: {art['name']}")
            speak(f"{art['name']}")
            if st.button("📥 +1 Stück"):
                conn.execute("UPDATE artikel SET bestand_stk = bestand_stk + 1 WHERE id=?", (art['id'],))
                conn.commit()
                st.success("Erfolgreich gebucht")
        conn.close()

def zeige_picking():
    st.subheader("📋 Picking & Lieferscheine")
    conn = get_connection()
    tab1, tab2 = st.tabs(["Offene Aufträge", "Archiv"])
    with tab1:
        offen = conn.execute("SELECT * FROM bestellungen WHERE status != 'kommissioniert'").fetchall()
        for b in offen:
            if st.button(f"Abschließen: {b['bestellnummer']}", key=b['id']):
                conn.execute("UPDATE bestellungen SET status='kommissioniert', kommissionierer=? WHERE id=?", (st.session_state.auth['username'], b['id']))
                conn.commit()
                st.rerun()
    with tab2:
        archiv = conn.execute("SELECT * FROM bestellungen WHERE status='kommissioniert'").fetchall()
        for b in archiv:
            with st.expander(f"Auftrag {b['bestellnummer']}"):
                if st.button("Drucken", key=f"p_{b['id']}"):
                    st.download_button("Download", generiere_lieferschein_pdf(b['id']), f"{b['bestellnummer']}.pdf")
    conn.close()

# -------------------------------------------------
# Main Navigation
# -------------------------------------------------
def main():
    st.set_page_config(page_title="KH-Logistik Final", layout="wide")
    init_db()
    if "auth" not in st.session_state: st.session_state.auth = None

    if st.session_state.auth is None:
        st.title("🏥 Krankenhaus Logistik Login")
        u = st.text_input("Benutzer")
        p = st.text_input("Passwort", type="password")
        if st.button("Anmelden"):
            conn = get_connection()
            user = conn.execute("SELECT * FROM internal_users WHERE username=? AND passwort_hash=?", (u, hash_pw(p))).fetchone()
            if user:
                st.session_state.auth = dict(user)
                st.rerun()
            else: st.error("Fehler")
        return

    meine_rechte = json.loads(st.session_state.auth['rechte_json'])
    st.sidebar.title(f"User: {st.session_state.auth['username']}")
    erlaubte_labels = [MENU_LABELS[r] for r in meine_rechte if r in MENU_LABELS]
    choice = st.sidebar.radio("Navigation", erlaubte_labels)

    if choice == MENU_LABELS["scanner"]: zeige_scanner()
    elif choice == MENU_LABELS["picking"]: zeige_picking()
    elif choice == MENU_LABELS["dashboard"]: st.info("Dashboard")
    elif choice == MENU_LABELS["art_anlegen"]: st.info("Artikelanlage")
    elif choice == MENU_LABELS["benutzerverwaltung"]: st.info("Benutzerverwaltung")

    if st.sidebar.button("Logout"):
        st.session_state.auth = None
        st.rerun()

if __name__ == "__main__":
    main()
