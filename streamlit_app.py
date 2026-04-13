import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime
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
DB_FILE = "lager_v602.db"
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
    "Admin": list(MENU_LABELS.keys()),
    "Picker": ["scanner", "picking"],
    "Verräumer": ["scanner", "picking", "lagerbestand"],
    "Einkauf": ["dashboard", "art_anlegen", "lagerbestand", "reporting"],
    "Viewer": ["dashboard"]
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
    cur.execute("""CREATE TABLE IF NOT EXISTS artikel (
        id INTEGER PRIMARY KEY AUTOINCREMENT, art_nr TEXT UNIQUE, name TEXT, 
        einheit TEXT, inhalt_pack INTEGER DEFAULT 10, inhalt_karton INTEGER DEFAULT 100, 
        inhalt_palette INTEGER DEFAULT 1000, bestand_stk INTEGER DEFAULT 0,
        meldebestand_stk INTEGER DEFAULT 10, lagerplatz TEXT, ean_barcode TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS internal_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, 
        passwort_hash TEXT, rolle TEXT, rechte_json TEXT, last_login TEXT, is_online INTEGER DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS kunden (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, passwort_hash TEXT,
        krankenhaus TEXT, station TEXT, adresse TEXT, ansprechpartner TEXT, email TEXT
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

# -------------------------------------------------
# UI SEKTIONEN
# -------------------------------------------------

def zeige_kundenverwaltung():
    st.header("👥 Kundenverwaltung (Admin)")
    conn = get_connection()
    kunden = conn.execute("SELECT * FROM kunden").fetchall()
    
    for k in kunden:
        with st.expander(f"Station: {k['station']} ({k['krankenhaus']})"):
            with st.form(f"edit_kunde_{k['id']}"):
                n_kh = st.text_input("Krankenhaus Name", value=k['krankenhaus'])
                n_st = st.text_input("Station Name", value=k['station'])
                n_pw = st.text_input("Passwort Reset (leer lassen für keine Änderung)", type="password")
                if st.form_submit_button("Änderungen speichern"):
                    if n_pw:
                        conn.execute("UPDATE kunden SET krankenhaus=?, station=?, passwort_hash=? WHERE id=?", 
                                     (n_kh, n_st, hash_pw(n_pw), k['id']))
                    else:
                        conn.execute("UPDATE kunden SET krankenhaus=?, station=? WHERE id=?", (n_kh, n_st, k['id']))
                    conn.commit()
                    st.success("Daten aktualisiert!")
    conn.close()

def zeige_art_anlegen():
    st.header("➕ Artikel & Mengen-Hierarchie")
    with st.form("art_logic_form"):
        c1, c2 = st.columns(2)
        art_nr = c1.text_input("Artikel-Nummer (REF)")
        name = c1.text_input("Bezeichnung")
        platz = c2.text_input("Lagerplatz (z.B. A-01-04)")
        einh = c2.selectbox("Basiseinheit", ["Stück", "Beutel", "Rolle"])
        
        st.divider()
        st.subheader("📦 Mengen-Definition (Umrechnung)")
        m1, m2, m3 = st.columns(3)
        i_pack = m1.number_input("Wieviele Stück sind 1 Pack?", min_value=1, value=10)
        i_kart = m2.number_input("Wieviele Stück sind 1 Karton?", min_value=1, value=100)
        i_pale = m3.number_input("Wieviele Stück sind 1 Palette?", min_value=1, value=1000)
        
        if st.form_submit_button("Artikel final speichern"):
            conn = get_connection()
            try:
                conn.execute("""INSERT INTO artikel (art_nr, name, lagerplatz, einheit, inhalt_pack, inhalt_karton, inhalt_palette) 
                             VALUES (?,?,?,?,?,?,?)""", (art_nr, name, platz, einh, i_pack, i_kart, i_pale))
                conn.commit()
                st.success(f"Artikel {name} erfolgreich angelegt!")
            except: st.error("Fehler: Artikelnummer bereits vergeben.")
            conn.close()

def zeige_lagerbestand():
    st.header("📦 Lagerbestand & Suche")
    suchbegriff = st.text_input("Suchen nach Name, Platz oder REF...")
    conn = get_connection()
    df = pd.read_sql_query("SELECT id, art_nr, name, bestand_stk, lagerplatz, einheit, meldebestand_stk FROM artikel", conn)
    
    if not df.empty:
        if suchbegriff:
            mask = df.apply(lambda r: suchbegriff.lower() in str(r.values).lower(), axis=1)
            df_display = df[mask]
        else:
            df_display = df
        st.dataframe(df_display.drop(columns=['id']), use_container_width=True, hide_index=True)
    conn.close()

# -------------------------------------------------
# Main Entry Point
# -------------------------------------------------
def main():
    st.set_page_config(page_title="Lager-System 6.0.2", layout="wide")
    init_db()

    if "auth" not in st.session_state: st.session_state.auth = None

    if st.session_state.auth is None:
        tab1, tab2 = st.tabs(["🔐 Login", "🏥 Registrierung"])
        with tab1:
            u = st.text_input("User")
            p = st.text_input("Passwort", type="password")
            if st.button("Anmelden"):
                conn = get_connection()
                user = conn.execute("SELECT * FROM internal_users WHERE username=? AND passwort_hash=?", (u, hash_pw(p))).fetchone()
                if user:
                    st.session_state.auth = dict(user)
                    st.rerun()
                else: st.error("Login fehlgeschlagen")
        with tab2:
            with st.form("reg"):
                ru = st.text_input("Username")
                rp = st.text_input("Passwort", type="password")
                rkh = st.text_input("Krankenhaus")
                rst = st.text_input("Station")
                if st.form_submit_button("Account erstellen"):
                    conn = get_connection()
                    conn.execute("INSERT INTO kunden (username, passwort_hash, krankenhaus, station) VALUES (?,?,?,?)",
                                 (ru, hash_pw(rp), rkh, rst))
                    conn.commit()
                    st.success("Konto angelegt!")
        return

    meine_rechte = json.loads(st.session_state.auth['rechte_json'])
    st.sidebar.title(f"User: {st.session_state.auth['username']}")
    erlaubte = [MENU_LABELS[r] for r in meine_rechte if r in MENU_LABELS]
    choice = st.sidebar.radio("Navigation", erlaubte)

    if choice == MENU_LABELS["art_anlegen"]: zeige_art_anlegen()
    elif choice == MENU_LABELS["lagerbestand"]: zeige_lagerbestand()
    elif choice == MENU_LABELS["kundenverwaltung"]: zeige_kundenverwaltung()
    elif choice == MENU_LABELS["dashboard"]: st.info("Monitor-Ansicht (V6.0.1)")
    elif choice == MENU_LABELS["scanner"]: st.info("Scanner aktiv")
    elif choice == MENU_LABELS["picking"]: st.info("Picking aktiv")

    if st.sidebar.button("Abmelden"):
        st.session_state.auth = None
        st.rerun()

if __name__ == "__main__":
    main()
