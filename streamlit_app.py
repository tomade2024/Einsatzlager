import hashlib
import io
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

DB_FILE = "lager.db"

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
BESTELLSTATUS = ["offen", "kommissioniert", "geliefert", "storniert"]


# -------------------------------------------------
# Hilfsfunktionen
# -------------------------------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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


def row_to_dict(row):
    return dict(row) if row is not None else None


def interner_user_eingeloggt():
    return st.session_state.get("internal_logged_in", False)


def kunde_eingeloggt():
    return "kunde" in st.session_state


def current_role():
    if not interner_user_eingeloggt():
        return None
    return st.session_state.get("internal_user", {}).get("rolle")


def require_role(*rollen):
    if not interner_user_eingeloggt():
        st.error("Bitte zuerst als interner Benutzer einloggen.")
        st.stop()
    if current_role() not in rollen:
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
        key = (
            item["artikel_id"],
            item.get("bestell_typ", "Stück"),
        )
        if key not in zusammen:
            zusammen[key] = item.copy()
        else:
            zusammen[key]["menge_stueck"] += item["menge_stueck"]
            zusammen[key]["eingabe_menge"] = (
                float(zusammen[key].get("eingabe_menge", 0))
                + float(item.get("eingabe_menge", 0))
            )
    return list(zusammen.values())


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
    ].copy()


def build_kommissionierliste_text(bestellung, positionen):
    lines = []
    lines.append("KOMMISSIONIERLISTE")
    lines.append("")
    lines.append(f"Bestellnummer: {bestellung['bestellnummer']}")
    lines.append(f"Datum: {bestellung['datum']}")
    lines.append(f"Uhrzeit: {bestellung['uhrzeit']}")
    lines.append(f"Kunde: {bestellung['kunde_name']}")
    lines.append(f"Status: {bestellung['status']}")
    lines.append("")
    lines.append("Artikel:")
    lines.append("")

    for _, pos in positionen.iterrows():
        lines.append(
            f"- {pos['artikelnummer']} | {pos['name']} | Lager: {pos['lager']} | Menge: {pos['menge_stueck']} Stück"
        )

    return "\n".join(lines)


def build_lieferschein_text(bestellung, positionen):
    lines = []
    lines.append("LIEFERSCHEIN")
    lines.append("")
    lines.append(f"Bestellnummer: {bestellung['bestellnummer']}")
    lines.append(f"Lieferadresse: {bestellung['lieferadresse']}")
    lines.append(f"Datum: {bestellung['datum']}")
    lines.append(f"Uhrzeit: {bestellung['uhrzeit']}")
    lines.append(f"Status: {bestellung['status']}")
    lines.append("")
    lines.append("Bestellte Materialien:")
    lines.append("")

    for _, pos in positionen.iterrows():
        lines.append(
            f"- {pos['artikelnummer']} | {pos['name']} | Menge: {pos['menge_stueck']} Stück"
        )

    return "\n".join(lines)


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
    columns = [row["name"] for row in cur.fetchall()]
    if column_name not in columns:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        conn.commit()
    conn.close()


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS artikel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikelnummer TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            lager TEXT NOT NULL,
            verpackung_typ TEXT NOT NULL,
            inhalt_pro_pack INTEGER NOT NULL DEFAULT 10,
            packs_pro_palette INTEGER NOT NULL DEFAULT 10,
            bestand_stueck INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wareneingang (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikel_id INTEGER NOT NULL,
            menge_stueck INTEGER NOT NULL,
            buchungs_typ TEXT,
            eingabe_menge REAL,
            datum TEXT NOT NULL,
            FOREIGN KEY (artikel_id) REFERENCES artikel(id)
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
        CREATE TABLE IF NOT EXISTS internal_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            passwort_hash TEXT NOT NULL,
            rolle TEXT NOT NULL,
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

    ensure_column_exists("artikel", "packs_pro_palette", "INTEGER NOT NULL DEFAULT 10")
    ensure_column_exists("wareneingang", "buchungs_typ", "TEXT")
    ensure_column_exists("wareneingang", "eingabe_menge", "REAL")
    ensure_column_exists("bestellungen", "status", "TEXT NOT NULL DEFAULT 'offen'")
    ensure_column_exists("bestellungen", "status_geaendert_am", "TEXT")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS anzahl FROM artikel")
    result = cur.fetchone()
    if result["anzahl"] == 0:
        demo_artikel = [
            ("MED-1001", "Verbandskasten", "Medizinlager", "Pack", 10, 12, 50),
            ("VER-1002", "Einweghandschuhe", "Verbrauchslager", "Pack", 10, 20, 120),
            ("MAT-1003", "Schrauben Set", "Materiallager", "Pack", 10, 30, 80),
            ("TEC-1004", "Netzteil", "Techniklager", "Stück", 1, 50, 15),
            ("MOE-1005", "Bürostuhl", "Möbellager", "Stück", 1, 8, 8),
            ("LEB-1006", "Mineralwasser", "Lebensmittellager", "Pack", 10, 48, 60),
            ("TEX-1007", "Arbeitshose", "Textillager", "Stück", 1, 25, 20),
        ]
        cur.executemany("""
            INSERT INTO artikel
            (artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, packs_pro_palette, bestand_stueck)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, demo_artikel)
        conn.commit()

    cur.execute("SELECT COUNT(*) AS anzahl FROM internal_users")
    user_count = cur.fetchone()["anzahl"]
    if user_count == 0:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        defaults = [
            ("admin", hash_password("admin123"), "Admin", now_str, 1),
            ("lager", hash_password("lager123"), "Lagerist", now_str, 1),
            ("vertrieb", hash_password("vertrieb123"), "Vertrieb", now_str, 1),
        ]
        cur.executemany("""
            INSERT INTO internal_users (username, passwort_hash, rolle, erstellt_am, ist_aktiv)
            VALUES (?, ?, ?, ?, ?)
        """, defaults)
        conn.commit()

    conn.close()


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
        SELECT id, username, rolle, erstellt_am, ist_aktiv
        FROM internal_users
        ORDER BY rolle, username
    """, conn)
    conn.close()
    return df


def internen_benutzer_anlegen(username: str, passwort: str, rolle: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO internal_users (username, passwort_hash, rolle, erstellt_am, ist_aktiv)
        VALUES (?, ?, ?, ?, 1)
    """, (
        username.strip(),
        hash_password(passwort),
        rolle,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))
    conn.commit()
    conn.close()


def internes_passwort_aendern(user_id: int, neues_passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE internal_users
        SET passwort_hash = ?
        WHERE id = ?
    """, (hash_password(neues_passwort), user_id))
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

    zeit = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        zeit
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

    cur.execute("""
        UPDATE kunden
        SET passwort_hash = ?
        WHERE id = ?
    """, (hash_password(neues_passwort), kunden_id))

    conn.commit()
    conn.close()


def kunde_passwort_admin_reset(kunden_id: int, neues_passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE kunden
        SET passwort_hash = ?
        WHERE id = ?
    """, (hash_password(neues_passwort), kunden_id))
    conn.commit()
    conn.close()


# -------------------------------------------------
# Artikel / Lager / Palette / Alternativen
# -------------------------------------------------
def artikel_df():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM artikel ORDER BY lager, name", conn)
    conn.close()

    if not df.empty:
        df["bestand_pack"] = df["bestand_stueck"] / df["inhalt_pro_pack"]
        df["bestand_pack"] = df["bestand_pack"].round(2)
        df["stueck_pro_palette"] = df["inhalt_pro_pack"] * df["packs_pro_palette"]
        df["bestand_palette"] = df["bestand_stueck"] / df["stueck_pro_palette"]
        df["bestand_palette"] = df["bestand_palette"].round(2)

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
    bestand_stueck
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO artikel
        (artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, packs_pro_palette, bestand_stueck)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        artikelnummer.strip(),
        name.strip(),
        lager,
        verpackung_typ,
        int(inhalt_pro_pack),
        int(packs_pro_palette),
        int(bestand_stueck),
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
    bestand_stueck
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE artikel
        SET artikelnummer = ?,
            name = ?,
            lager = ?,
            verpackung_typ = ?,
            inhalt_pro_pack = ?,
            packs_pro_palette = ?,
            bestand_stueck = ?
        WHERE id = ?
    """, (
        artikelnummer.strip(),
        name.strip(),
        lager,
        verpackung_typ,
        int(inhalt_pro_pack),
        int(packs_pro_palette),
        int(bestand_stueck),
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

    cur.execute("""
        SELECT COUNT(*) AS anzahl
        FROM artikel_alternativen
        WHERE artikel_id = ? OR alternativ_artikel_id = ?
    """, (artikel_id, artikel_id))
    alternativen = cur.fetchone()["anzahl"]

    if bestellungen > 0 or wareneingaenge > 0 or alternativen > 0:
        conn.close()
        raise ValueError(
            "Artikel kann nicht gelöscht werden, weil bereits Wareneingänge, Bestellungen oder Alternativzuordnungen dazu existieren."
        )

    cur.execute("DELETE FROM artikel WHERE id = ?", (artikel_id,))
    conn.commit()
    conn.close()


def menge_zu_stueck(artikel_row, buchungs_typ: str, eingabe_menge: float) -> int:
    inhalt_pro_pack = int(artikel_row["inhalt_pro_pack"])
    packs_pro_palette = int(artikel_row["packs_pro_palette"])

    if buchungs_typ == "Stück":
        return int(eingabe_menge)
    if buchungs_typ == "Pack":
        return int(eingabe_menge * inhalt_pro_pack)
    if buchungs_typ == "Palette":
        return int(eingabe_menge * inhalt_pro_pack * packs_pro_palette)
    raise ValueError("Ungültiger Buchungstyp.")


def bestellmenge_zu_stueck(artikel_row, bestell_typ: str, eingabe_menge: float) -> int:
    inhalt_pro_pack = int(artikel_row["inhalt_pro_pack"])
    packs_pro_palette = int(artikel_row["packs_pro_palette"])

    if bestell_typ == "Stück":
        return int(eingabe_menge)
    if bestell_typ == "Pack":
        return int(eingabe_menge * inhalt_pro_pack)
    if bestell_typ == "Palette":
        return int(eingabe_menge * inhalt_pro_pack * packs_pro_palette)

    raise ValueError("Ungültiger Bestelltyp.")


def wareneingang_buchen(artikel_id: int, buchungs_typ: str, eingabe_menge: float):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,))
    artikel = cur.fetchone()

    if artikel is None:
        conn.close()
        raise ValueError("Artikel wurde nicht gefunden.")

    menge_stueck = menge_zu_stueck(artikel, buchungs_typ, eingabe_menge)

    cur.execute("""
        UPDATE artikel
        SET bestand_stueck = bestand_stueck + ?
        WHERE id = ?
    """, (menge_stueck, artikel_id))

    cur.execute("""
        INSERT INTO wareneingang (artikel_id, menge_stueck, buchungs_typ, eingabe_menge, datum)
        VALUES (?, ?, ?, ?, ?)
    """, (
        artikel_id,
        menge_stueck,
        buchungs_typ,
        float(eingabe_menge),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))

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

    if df.empty:
        return []

    return df["alternativ_artikel_id"].tolist()


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
        df["bestand_pack"] = df["bestand_stueck"] / df["inhalt_pro_pack"]
        df["bestand_pack"] = df["bestand_pack"].round(2)
        df["stueck_pro_palette"] = df["inhalt_pro_pack"] * df["packs_pro_palette"]
        df["bestand_palette"] = df["bestand_stueck"] / df["stueck_pro_palette"]
        df["bestand_palette"] = df["bestand_palette"].round(2)

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


# -------------------------------------------------
# Bestellungen
# -------------------------------------------------
def bestellung_speichern(kunden_id: int, kunde_name_text: str, lieferadresse: str, warenkorb: list):
    jetzt = datetime.now()
    bestellnummer = f"B-{jetzt.strftime('%Y%m%d%H%M%S')}"

    conn = get_connection()
    cur = conn.cursor()

    erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunden_id)

    for pos in warenkorb:
        cur.execute("SELECT bestand_stueck, lager FROM artikel WHERE id = ?", (pos["artikel_id"],))
        artikel = cur.fetchone()

        if artikel is None:
            conn.close()
            raise ValueError(f"Artikel {pos['name']} wurde nicht gefunden.")

        if artikel["lager"] not in erlaubte_lager:
            conn.close()
            raise ValueError(f"Das Lager {artikel['lager']} ist für diesen Kunden gesperrt.")

        if artikel["bestand_stueck"] < pos["menge_stueck"]:
            conn.close()
            raise ValueError(
                f"Zu wenig Bestand für {pos['name']}. Verfügbar: {artikel['bestand_stueck']} Stück."
            )

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
        jetzt.strftime("%Y-%m-%d %H:%M:%S"),
    ))
    bestellung_id = cur.lastrowid

    for pos in warenkorb:
        cur.execute("""
            INSERT INTO bestellpositionen (bestellung_id, artikel_id, menge_stueck)
            VALUES (?, ?, ?)
        """, (bestellung_id, pos["artikel_id"], pos["menge_stueck"]))

        cur.execute("""
            UPDATE artikel
            SET bestand_stueck = bestand_stueck - ?
            WHERE id = ?
        """, (pos["menge_stueck"], pos["artikel_id"]))

    conn.commit()
    conn.close()
    return bestellnummer


def hole_bestellungen():
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT *
        FROM bestellungen
        ORDER BY id DESC
    """, conn)
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


def bestellstatus_setzen(bestellung_id: int, neuer_status: str):
    if neuer_status not in BESTELLSTATUS:
        raise ValueError("Ungültiger Bestellstatus.")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bestellungen
        SET status = ?, status_geaendert_am = ?
        WHERE id = ?
    """, (
        neuer_status,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        int(bestellung_id),
    ))
    conn.commit()
    conn.close()


def hole_bestellpositionen(bestell_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT
            bp.menge_stueck,
            a.artikelnummer,
            a.name,
            a.lager,
            a.verpackung_typ,
            a.inhalt_pro_pack,
            a.packs_pro_palette
        FROM bestellpositionen bp
        JOIN artikel a ON a.id = bp.artikel_id
        WHERE bp.bestellung_id = ?
        ORDER BY a.lager, a.name
    """, conn, params=(bestell_id,))
    conn.close()
    return df


# -------------------------------------------------
# PDF / Druck
# -------------------------------------------------
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


def pdf_download_button(pdf_bytes: bytes, filename: str, label: str):
    st.download_button(
        label=label,
        data=pdf_bytes,
        file_name=filename,
        mime="application/pdf"
    )


def generate_pdf_lieferschein(bestellung, positionen):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm
    )

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

    info_table = Table(info, colWidths=[45 * mm, 120 * mm])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Bestellte Materialien</b>", styles["Heading3"]))
    story.append(Spacer(1, 6))

    data = [["Artikelnummer", "Bezeichnung", "Lager", "Menge (Stück)"]]
    for _, pos in positionen.iterrows():
        data.append([
            str(pos["artikelnummer"]),
            str(pos["name"]),
            str(pos["lager"]),
            str(pos["menge_stueck"]),
        ])

    pos_table = Table(data, colWidths=[35 * mm, 70 * mm, 45 * mm, 30 * mm])
    pos_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9eaf7")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    story.append(pos_table)
    story.append(Spacer(1, 20))

    story.append(Paragraph("Unterschrift Warenausgang: ________________________________", styles["Normal"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Unterschrift Kunde / Empfänger: ________________________________", styles["Normal"]))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def generate_pdf_kommissionierliste(bestellung, positionen):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm
    )

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

    info_table = Table(info, colWidths=[45 * mm, 120 * mm])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Zu kommissionierende Artikel</b>", styles["Heading3"]))
    story.append(Spacer(1, 6))

    data = [["Artikelnummer", "Bezeichnung", "Lager", "Menge", "Erledigt"]]
    for _, pos in positionen.iterrows():
        data.append([
            str(pos["artikelnummer"]),
            str(pos["name"]),
            str(pos["lager"]),
            str(pos["menge_stueck"]),
            "_____",
        ])

    pos_table = Table(data, colWidths=[30 * mm, 60 * mm, 40 * mm, 20 * mm, 25 * mm])
    pos_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f3e8")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    story.append(pos_table)

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


# -------------------------------------------------
# Login Views
# -------------------------------------------------
def zeige_start_login():
    st.title("📦 Lagerwirtschaft")
    st.write("Bitte zuerst einloggen.")

    tab1, tab2 = st.tabs(["Interner Login", "Kunden Login / Registrierung"])

    with tab1:
        st.subheader("Interner Benutzer Login")
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
        tab_login, tab_register = st.tabs(["Kunden Login", "Registrierung"])

        with tab_login:
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

        with tab_register:
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
                            firmenname, anrede, vorname, nachname, email, telefon,
                            strasse, plz, ort, passwort
                        )
                        st.success(f"Registrierung erfolgreich. Kundennummer: {kunden_nr}")
                    except sqlite3.IntegrityError:
                        st.error("Diese E-Mail ist bereits registriert.")


# -------------------------------------------------
# Interne Bereiche
# -------------------------------------------------
def zeige_lagerbestand():
    require_role("Admin", "Lagerist", "Vertrieb")
    st.subheader("Lagerbestand")

    df = artikel_df()
    suchtext = st.text_input("Artikel suchen", placeholder="Artikelnummer, Name oder Lager")
    df = suche_artikel_df(df, suchtext)

    lager_filter = st.selectbox("Unterlager filtern", ["Alle"] + LAGER)
    if lager_filter != "Alle":
        df = df[df["lager"] == lager_filter]

    if df.empty:
        st.info("Keine Artikel gefunden.")
        return

    anzeigen = df[[
        "artikelnummer",
        "name",
        "lager",
        "verpackung_typ",
        "inhalt_pro_pack",
        "packs_pro_palette",
        "bestand_stueck",
        "bestand_pack",
        "bestand_palette",
    ]].copy()

    anzeigen.columns = [
        "Artikelnummer",
        "Bezeichnung",
        "Lager",
        "Verpackung",
        "Stück pro Pack",
        "Pack pro Palette",
        "Bestand Stück",
        "Bestand Pack",
        "Bestand Palette",
    ]

    st.dataframe(anzeigen, use_container_width=True)


def zeige_gesamtmonitor():
    require_role("Admin", "Lagerist", "Vertrieb")
    st.subheader("Gesamtmonitor Bestellübersicht")

    bestellungen = hole_bestellungen()

    if bestellungen.empty:
        st.info("Es gibt noch keine Bestellungen.")
        return

    offen_count = len(bestellungen[bestellungen["status"] == "offen"])
    komm_count = len(bestellungen[bestellungen["status"] == "kommissioniert"])
    geliefert_count = len(bestellungen[bestellungen["status"] == "geliefert"])
    storniert_count = len(bestellungen[bestellungen["status"] == "storniert"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Offen", offen_count)
    c2.metric("Kommissioniert", komm_count)
    c3.metric("Geliefert", geliefert_count)
    c4.metric("Storniert", storniert_count)

    col1, col2 = st.columns(2)

    with col1:
        status_filter = st.selectbox(
            "Status filtern",
            ["Alle"] + BESTELLSTATUS,
            key="monitor_status_filter"
        )

    with col2:
        suchtext = st.text_input(
            "Suche",
            placeholder="Bestellnummer, Kunde, Lieferadresse, Datum",
            key="monitor_suche"
        ).strip().lower()

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
        ].copy()

    if df.empty:
        st.info("Keine Bestellungen für den aktuellen Filter gefunden.")
        return

    anzeige = df[[
        "bestellnummer",
        "datum",
        "uhrzeit",
        "kunde_name",
        "status",
        "lieferadresse"
    ]].copy()

    anzeige.columns = [
        "Lieferscheinnummer",
        "Datum",
        "Uhrzeit",
        "Kunde",
        "Status",
        "Lieferadresse"
    ]

    def status_farbe(status):
        if status == "offen":
            return "background-color: #fff3cd; color: #856404;"
        if status == "kommissioniert":
            return "background-color: #d1ecf1; color: #0c5460;"
        if status == "geliefert":
            return "background-color: #d4edda; color: #155724;"
        if status == "storniert":
            return "background-color: #f8d7da; color: #721c24;"
        return ""

    styled_df = anzeige.style.map(status_farbe, subset=["Status"])

    st.dataframe(styled_df, use_container_width=True, height=550)

    st.markdown("### Status-Legende")
    l1, l2, l3, l4 = st.columns(4)
    with l1:
        st.markdown("🟨 **offen**")
    with l2:
        st.markdown("🟦 **kommissioniert**")
    with l3:
        st.markdown("🟩 **geliefert**")
    with l4:
        st.markdown("🟥 **storniert**")


def zeige_tv_monitor():
    require_role("Admin", "Lagerist", "Vertrieb")

    bestellungen = hole_bestellungen()

    offen_count = len(bestellungen[bestellungen["status"] == "offen"]) if not bestellungen.empty else 0
    komm_count = len(bestellungen[bestellungen["status"] == "kommissioniert"]) if not bestellungen.empty else 0
    geliefert_count = len(bestellungen[bestellungen["status"] == "geliefert"]) if not bestellungen.empty else 0
    storniert_count = len(bestellungen[bestellungen["status"] == "storniert"]) if not bestellungen.empty else 0

    df = bestellungen.copy()

    if not df.empty:
        df = df[[
            "bestellnummer",
            "datum",
            "uhrzeit",
            "kunde_name",
            "status"
        ]].copy()

        df.columns = [
            "Lieferscheinnummer",
            "Datum",
            "Uhrzeit",
            "Kunde",
            "Status"
        ]

    components.html(
        """
        <script>
            setTimeout(function() {
                window.location.reload();
            }, 30000);
        </script>
        """,
        height=0,
    )

    st.markdown("""
    <style>
        header[data-testid="stHeader"] {display: none;}
        section[data-testid="stSidebar"] {display: none;}
        div[data-testid="stToolbar"] {display: none;}
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}

        .block-container {
            padding-top: 1rem;
            padding-bottom: 1rem;
            padding-left: 1.5rem;
            padding-right: 1.5rem;
            max-width: 100%;
        }

        .tv-title {
            font-size: 42px;
            font-weight: 800;
            margin-bottom: 10px;
        }

        .tv-subtitle {
            font-size: 20px;
            color: #666;
            margin-bottom: 20px;
        }

        .tv-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 18px;
            margin-bottom: 24px;
        }

        .tv-card {
            border-radius: 18px;
            padding: 24px;
            color: #111;
            box-shadow: 0 4px 14px rgba(0,0,0,0.08);
            text-align: center;
        }

        .tv-card h3 {
            margin: 0;
            font-size: 24px;
            font-weight: 700;
        }

        .tv-card .value {
            margin-top: 10px;
            font-size: 48px;
            font-weight: 800;
        }

        .tv-offen { background: #fff3cd; }
        .tv-kommissioniert { background: #d1ecf1; }
        .tv-geliefert { background: #d4edda; }
        .tv-storniert { background: #f8d7da; }

        .tv-table-title {
            font-size: 28px;
            font-weight: 700;
            margin-top: 10px;
            margin-bottom: 10px;
        }

        .tv-refresh {
            font-size: 18px;
            color: #666;
            margin-bottom: 18px;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="tv-title">📺 TV-Monitor Bestellstatus</div>', unsafe_allow_html=True)
    st.markdown('<div class="tv-subtitle">Automatische Aktualisierung alle 30 Sekunden</div>', unsafe_allow_html=True)

    st.markdown(f"""
    <div class="tv-grid">
        <div class="tv-card tv-offen">
            <h3>Offen</h3>
            <div class="value">{offen_count}</div>
        </div>
        <div class="tv-card tv-kommissioniert">
            <h3>Kommissioniert</h3>
            <div class="value">{komm_count}</div>
        </div>
        <div class="tv-card tv-geliefert">
            <h3>Geliefert</h3>
            <div class="value">{geliefert_count}</div>
        </div>
        <div class="tv-card tv-storniert">
            <h3>Storniert</h3>
            <div class="value">{storniert_count}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="tv-table-title">Aktuelle Bestellungen</div>', unsafe_allow_html=True)
    st.markdown('<div class="tv-refresh">Ansicht ist für einen TV- oder Wandmonitor optimiert.</div>', unsafe_allow_html=True)

    if df.empty:
        st.info("Es sind aktuell keine Bestellungen vorhanden.")
        return

    def status_farbe(status):
        if status == "offen":
            return "background-color: #fff3cd; color: #856404; font-weight: 700; font-size: 22px;"
        if status == "kommissioniert":
            return "background-color: #d1ecf1; color: #0c5460; font-weight: 700; font-size: 22px;"
        if status == "geliefert":
            return "background-color: #d4edda; color: #155724; font-weight: 700; font-size: 22px;"
        if status == "storniert":
            return "background-color: #f8d7da; color: #721c24; font-weight: 700; font-size: 22px;"
        return ""

    styled_df = (
        df.style
        .map(status_farbe, subset=["Status"])
        .set_properties(**{
            "font-size": "22px",
            "text-align": "left",
        })
    )

    st.dataframe(
        styled_df,
        use_container_width=True,
        height=700
    )


def zeige_wareneingang():
    require_role("Admin", "Lagerist")
    st.subheader("Wareneingang buchen")

    df = artikel_df()
    if df.empty:
        st.warning("Keine Artikel vorhanden.")
        return

    artikel_map = {
        (
            f"{row['artikelnummer']} | {row['name']} | "
            f"{row['lager']} | {row['inhalt_pro_pack']} Stk/Pack | "
            f"{row['packs_pro_palette']} Pack/Palette"
        ): row
        for _, row in df.iterrows()
    }

    auswahl = st.selectbox("Artikel", list(artikel_map.keys()))
    artikel = artikel_map[auswahl]

    st.info(
        f"Artikel: {artikel['name']} | Stück pro Pack: {artikel['inhalt_pro_pack']} | "
        f"Pack pro Palette: {artikel['packs_pro_palette']}"
    )

    buchungs_typ = st.selectbox("Wareneingang buchen als", ["Stück", "Pack", "Palette"])

    if buchungs_typ == "Palette":
        eingabe_menge = st.number_input("Anzahl Paletten", min_value=1.0, step=1.0, value=1.0)
    else:
        eingabe_menge = st.number_input(f"Anzahl {buchungs_typ}", min_value=1.0, step=1.0, value=1.0)

    if st.button("Wareneingang buchen"):
        try:
            wareneingang_buchen(int(artikel["id"]), buchungs_typ, float(eingabe_menge))
            st.success("Wareneingang erfolgreich gebucht.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))


def zeige_artikel_anlegen():
    require_role("Admin", "Lagerist")
    st.subheader("Neuen Artikel anlegen")

    with st.form("artikel_form"):
        artikelnummer = st.text_input("Artikelnummer")
        name = st.text_input("Artikelbezeichnung")
        lager = st.selectbox("Unterlager", LAGER)
        verpackung_typ = st.selectbox("Verpackungsart", ["Stück", "Pack"])
        inhalt_pro_pack = st.number_input("Stück pro Pack", min_value=1, value=10, step=1)
        packs_pro_palette = st.number_input("Pack pro Palette", min_value=1, value=10, step=1)
        bestand_stueck = st.number_input("Startbestand in Stück", min_value=0, value=0, step=1)
        senden = st.form_submit_button("Artikel speichern")

    if senden:
        if not artikelnummer.strip() or not name.strip():
            st.error("Bitte Artikelnummer und Bezeichnung ausfüllen.")
        else:
            try:
                artikel_speichern(
                    artikelnummer,
                    name,
                    lager,
                    verpackung_typ,
                    inhalt_pro_pack,
                    packs_pro_palette,
                    bestand_stueck,
                )
                st.success("Artikel wurde gespeichert.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Die Artikelnummer existiert bereits.")


def zeige_artikel_bearbeiten_loeschen():
    require_role("Admin", "Lagerist")
    st.subheader("Artikel bearbeiten / löschen")

    df = artikel_df()
    if df.empty:
        st.info("Keine Artikel vorhanden.")
        return

    suchtext = st.text_input("Artikel suchen", placeholder="Artikelnummer, Name oder Lager", key="artikel_edit_suche")
    df = suche_artikel_df(df, suchtext)

    if df.empty:
        st.info("Keine Artikel gefunden.")
        return

    artikel_map = {
        f"{row['artikelnummer']} | {row['name']} | {row['lager']}": row
        for _, row in df.iterrows()
    }

    auswahl = st.selectbox("Artikel auswählen", list(artikel_map.keys()))
    artikel = artikel_map[auswahl]

    tab1, tab2, tab3 = st.tabs(["Artikel bearbeiten", "Alternativen", "Artikel löschen"])

    with tab1:
        with st.form("artikel_bearbeiten_form"):
            artikelnummer = st.text_input("Artikelnummer", value=artikel["artikelnummer"])
            name = st.text_input("Artikelbezeichnung", value=artikel["name"])
            lager = st.selectbox(
                "Unterlager",
                LAGER,
                index=LAGER.index(artikel["lager"])
            )
            verpackung_typ = st.selectbox(
                "Verpackungsart",
                ["Stück", "Pack"],
                index=["Stück", "Pack"].index(artikel["verpackung_typ"])
            )
            inhalt_pro_pack = st.number_input(
                "Stück pro Pack",
                min_value=1,
                value=int(artikel["inhalt_pro_pack"]),
                step=1
            )
            packs_pro_palette = st.number_input(
                "Pack pro Palette",
                min_value=1,
                value=int(artikel["packs_pro_palette"]),
                step=1
            )
            bestand_stueck = st.number_input(
                "Bestand in Stück",
                min_value=0,
                value=int(artikel["bestand_stueck"]),
                step=1
            )

            speichern = st.form_submit_button("Änderungen speichern")

        if speichern:
            if not artikelnummer.strip() or not name.strip():
                st.error("Bitte Artikelnummer und Bezeichnung ausfüllen.")
            else:
                try:
                    artikel_aktualisieren(
                        artikel_id=int(artikel["id"]),
                        artikelnummer=artikelnummer,
                        name=name,
                        lager=lager,
                        verpackung_typ=verpackung_typ,
                        inhalt_pro_pack=inhalt_pro_pack,
                        packs_pro_palette=packs_pro_palette,
                        bestand_stueck=bestand_stueck,
                    )
                    st.success("Artikel wurde aktualisiert.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Die Artikelnummer existiert bereits.")

    with tab2:
        st.markdown("### Alternative Artikel hinterlegen")

        alle_artikel = artikel_df()
        alle_artikel = alle_artikel[alle_artikel["id"] != artikel["id"]].copy()

        vorhandene_ids = hole_artikel_alternativen_ids(int(artikel["id"]))

        alt_options = {}
        for _, row in alle_artikel.iterrows():
            label = (
                f"{row['artikelnummer']} | {row['name']} | {row['lager']} | "
                f"Bestand: {row['bestand_stueck']} Stück"
            )
            alt_options[label] = int(row["id"])

        default_labels = [label for label, aid in alt_options.items() if aid in vorhandene_ids]

        neue_auswahl = st.multiselect(
            "Alternative Artikel",
            options=list(alt_options.keys()),
            default=default_labels
        )

        if st.button("Alternativen speichern"):
            alternative_ids = [alt_options[label] for label in neue_auswahl]
            setze_artikel_alternativen(int(artikel["id"]), alternative_ids)
            st.success("Alternative Artikel wurden gespeichert.")
            st.rerun()

        st.markdown("### Aktuell hinterlegte Alternativen")
        alt_df = hole_artikel_alternativen_df(int(artikel["id"]))
        if alt_df.empty:
            st.info("Keine Alternativen hinterlegt.")
        else:
            anzeigen = alt_df[[
                "artikelnummer",
                "name",
                "lager",
                "bestand_stueck",
                "bestand_pack",
                "bestand_palette"
            ]].copy()
            anzeigen.columns = [
                "Artikelnummer",
                "Bezeichnung",
                "Lager",
                "Bestand Stück",
                "Bestand Pack",
                "Bestand Palette"
            ]
            st.dataframe(anzeigen, use_container_width=True)

    with tab3:
        st.warning("Achtung: Löschen ist nur möglich, wenn noch keine Wareneingänge oder Bestellungen existieren.")
        st.write(f"**Artikelnummer:** {artikel['artikelnummer']}")
        st.write(f"**Bezeichnung:** {artikel['name']}")
        st.write(f"**Lager:** {artikel['lager']}")

        if st.button("Artikel endgültig löschen"):
            try:
                artikel_loeschen(int(artikel["id"]))
                st.success("Artikel wurde gelöscht.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))


def zeige_kundenverwaltung():
    require_role("Admin", "Vertrieb")
    st.subheader("Kundenverwaltung")

    kunden_df = hole_alle_kunden()
    if kunden_df.empty:
        st.info("Noch keine Kunden registriert.")
        return

    st.dataframe(kunden_df, use_container_width=True)

    kunden_map = {}
    for _, row in kunden_df.iterrows():
        label = f"{row['kunden_nr']} | {row['vorname']} {row['nachname']} | {row['email']}"
        kunden_map[label] = int(row["id"])

    auswahl = st.selectbox("Kunde auswählen", list(kunden_map.keys()))
    kunden_id = kunden_map[auswahl]

    kunde = hole_kunde_by_id(kunden_id)
    erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunden_id)

    tab1, tab2, tab3 = st.tabs(["Lagerfreigaben", "Kundendaten", "Passwort zurücksetzen"])

    with tab1:
        neue_freigaben = st.multiselect(
            "Erlaubte Unterlager für diesen Kunden",
            options=LAGER,
            default=erlaubte_lager
        )

        if st.button("Lagerfreigaben speichern"):
            setze_lagerfreigaben_fuer_kunde(kunden_id, neue_freigaben)
            st.success("Lagerfreigaben gespeichert.")
            st.rerun()

    with tab2:
        st.write(f"**Kundennummer:** {kunde['kunden_nr']}")
        st.write(f"**Name:** {kunde['vorname']} {kunde['nachname']}")
        st.write(f"**E-Mail:** {kunde['email']}")
        st.write(f"**Telefon:** {kunde['telefon']}")
        st.write(f"**Adresse:** {kunde['strasse']}, {kunde['plz']} {kunde['ort']}")

    with tab3:
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


def zeige_bestellungen():
    require_role("Admin", "Lagerist", "Vertrieb")
    st.subheader("Bestellungen, Kommissionierliste und Lieferschein")

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

    st.markdown("### Bestelldaten")
    col1, col2 = st.columns(2)

    with col1:
        st.write(f"**Bestellnummer:** {bestellung['bestellnummer']}")
        st.write(f"**Kunde:** {bestellung['kunde_name']}")
        st.write(f"**Datum:** {bestellung['datum']}")
        st.write(f"**Uhrzeit:** {bestellung['uhrzeit']}")
        st.write(f"**Status:** {bestellung['status']}")

    with col2:
        st.write("**Lieferadresse:**")
        st.write(bestellung["lieferadresse"])

    st.markdown("### Bestellstatus ändern")
    neuer_status = st.selectbox(
        "Neuen Status wählen",
        BESTELLSTATUS,
        index=BESTELLSTATUS.index(bestellung["status"]) if bestellung["status"] in BESTELLSTATUS else 0
    )

    if st.button("Bestellstatus speichern"):
        try:
            bestellstatus_setzen(int(bestellung["id"]), neuer_status)
            st.success("Bestellstatus wurde aktualisiert.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    st.markdown("### Positionen")
    st.dataframe(positionen, use_container_width=True)

    kom_text = build_kommissionierliste_text(bestellung, positionen)
    lief_text = build_lieferschein_text(bestellung, positionen)
    kom_pdf = generate_pdf_kommissionierliste(bestellung, positionen)
    lief_pdf = generate_pdf_lieferschein(bestellung, positionen)

    tab1, tab2 = st.tabs(["Kommissionierliste", "Lieferschein"])

    with tab1:
        st.text_area("Kommissionierliste Vorschau", value=kom_text, height=350)
        col_a, col_b = st.columns(2)
        with col_a:
            pdf_download_button(
                kom_pdf,
                f"Kommissionierliste_{bestellung['bestellnummer']}.pdf",
                "📄 PDF-Kommissionierliste herunterladen"
            )
        with col_b:
            render_print_button(
                title=f"Kommissionierliste {bestellung['bestellnummer']}",
                text_content=kom_text,
                button_label="🖨️ Kommissionierliste drucken"
            )

    with tab2:
        st.text_area("Lieferschein Vorschau", value=lief_text, height=350)
        col_a, col_b = st.columns(2)
        with col_a:
            pdf_download_button(
                lief_pdf,
                f"Lieferschein_{bestellung['bestellnummer']}.pdf",
                "📄 PDF-Lieferschein herunterladen"
            )
        with col_b:
            render_print_button(
                title=f"Lieferschein {bestellung['bestellnummer']}",
                text_content=lief_text,
                button_label="🖨️ Lieferschein drucken"
            )


def zeige_benutzerverwaltung():
    require_role("Admin")
    st.subheader("Interne Benutzerverwaltung")

    df = hole_interne_benutzer()
    st.dataframe(df, use_container_width=True)

    st.markdown("### Neuen internen Benutzer anlegen")
    with st.form("internal_user_form"):
        username = st.text_input("Benutzername")
        rolle = st.selectbox("Rolle", ROLLEN)
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
                internen_benutzer_anlegen(username, passwort, rolle)
                st.success("Interner Benutzer wurde angelegt.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Benutzername existiert bereits.")

    st.markdown("### Eigenes Passwort ändern")
    current_user = st.session_state.get("internal_user")
    with st.form("change_internal_pw"):
        neues_passwort = st.text_input("Neues Passwort", type="password")
        neues_passwort2 = st.text_input("Neues Passwort wiederholen", type="password")
        update = st.form_submit_button("Passwort ändern")

    if update:
        if not neues_passwort:
            st.error("Bitte neues Passwort eingeben.")
        elif neues_passwort != neues_passwort2:
            st.error("Die Passwörter stimmen nicht überein.")
        else:
            internes_passwort_aendern(current_user["id"], neues_passwort)
            st.success("Passwort wurde geändert.")


# -------------------------------------------------
# Kundenbereiche
# -------------------------------------------------
def zeige_mein_konto():
    if not kunde_eingeloggt():
        st.error("Bitte als Kunde einloggen.")
        st.stop()

    kunde = st.session_state.kunde
    st.subheader("Mein Konto")

    tab1, tab2 = st.tabs(["Kundendaten", "Passwort ändern"])

    with tab1:
        st.write(f"**Kundennummer:** {kunde['kunden_nr']}")
        st.write(f"**Name:** {kunde_name(kunde)}")
        st.write(f"**E-Mail:** {kunde['email']}")
        st.write(f"**Telefon:** {kunde['telefon']}")
        st.write("**Adresse:**")
        st.text(kunde_lieferadresse(kunde))

        erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunde["id"])
        st.write("**Freigegebene Unterlager:**")
        if erlaubte_lager:
            for lager in erlaubte_lager:
                st.write(f"- {lager}")
        else:
            st.write("Keine freigegebenen Unterlager.")

    with tab2:
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

    suchtext = st.text_input("Artikel suchen", placeholder="Artikelnummer, Name oder Lager", key="shop_suche")
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
            f"Bestand: {row['bestand_stueck']} Stück | "
            f"{row['bestand_pack']} Pack | {row['bestand_palette']} Palette"
        ): row
        for _, row in sichtbare_artikel.iterrows()
    }

    ausgewaehlt = st.selectbox("Artikel auswählen", list(artikel_map.keys()))
    row = artikel_map[ausgewaehlt]

    st.write(f"**Stück pro Pack:** {int(row['inhalt_pro_pack'])}")
    st.write(f"**Pack pro Palette:** {int(row['packs_pro_palette'])}")
    st.write(f"**Stück pro Palette:** {int(row['inhalt_pro_pack']) * int(row['packs_pro_palette'])}")
    st.write(f"**Aktueller Bestand:** {int(row['bestand_stueck'])} Stück")

    bestell_typ = st.selectbox("Bestellen als", ["Stück", "Pack", "Palette"])

    if bestell_typ == "Palette":
        menge_eingabe = st.number_input("Anzahl Paletten", min_value=1.0, value=1.0, step=1.0)
    else:
        menge_eingabe = st.number_input(f"Anzahl {bestell_typ}", min_value=1.0, value=1.0, step=1.0)

    try:
        menge_stueck = bestellmenge_zu_stueck(row, bestell_typ, float(menge_eingabe))
    except ValueError as e:
        st.error(str(e))
        return

    st.write(f"**Umgerechnete Bestellmenge:** {menge_stueck} Stück")

    genug_bestand = menge_stueck <= int(row["bestand_stueck"])

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
        st.error(f"Nicht genug Bestand. Verfügbar sind nur {int(row['bestand_stueck'])} Stück.")
        st.markdown("### Alternative Artikel")

        alt_df = hole_artikel_alternativen_df(int(row["id"]))
        if not alt_df.empty:
            alt_df = alt_df[
                (alt_df["lager"].isin(erlaubte_lager)) &
                (alt_df["bestand_stueck"] > 0)
            ].copy()

        if alt_df.empty:
            st.info("Keine verfügbaren Alternativen hinterlegt.")
        else:
            alt_map = {
                (
                    f"{alt['artikelnummer']} | {alt['name']} | Lager: {alt['lager']} | "
                    f"Bestand: {alt['bestand_stueck']} Stück | "
                    f"{alt['bestand_pack']} Pack | {alt['bestand_palette']} Palette"
                ): alt
                for _, alt in alt_df.iterrows()
            }

            alt_auswahl = st.selectbox("Alternative auswählen", list(alt_map.keys()))
            alt_row = alt_map[alt_auswahl]

            st.write(f"**Alternative gewählt:** {alt_row['name']}")
            st.write(f"**Stück pro Pack:** {int(alt_row['inhalt_pro_pack'])}")
            st.write(f"**Pack pro Palette:** {int(alt_row['packs_pro_palette'])}")
            st.write(f"**Bestand:** {int(alt_row['bestand_stueck'])} Stück")

            try:
                alt_menge_stueck = bestellmenge_zu_stueck(alt_row, bestell_typ, float(menge_eingabe))
            except ValueError as e:
                st.error(str(e))
                alt_menge_stueck = None

            if alt_menge_stueck is not None:
                st.write(f"**Umgerechnete Menge für Alternative:** {alt_menge_stueck} Stück")

                if alt_menge_stueck <= int(alt_row["bestand_stueck"]):
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
                    st.warning("Die Alternative hat ebenfalls nicht genug Bestand für diese Menge.")

    st.markdown("### Warenkorb")
    if st.session_state.warenkorb:
        warenkorb_df = pd.DataFrame(st.session_state.warenkorb)

        if "bestell_typ" not in warenkorb_df.columns:
            warenkorb_df["bestell_typ"] = "Stück"
        if "eingabe_menge" not in warenkorb_df.columns:
            warenkorb_df["eingabe_menge"] = warenkorb_df["menge_stueck"]

        anzeigen = warenkorb_df[[
            "artikelnummer",
            "name",
            "lager",
            "bestell_typ",
            "eingabe_menge",
            "menge_stueck"
        ]].copy()

        anzeigen.columns = [
            "Artikelnummer",
            "Bezeichnung",
            "Lager",
            "Bestellt als",
            "Eingabemenge",
            "Menge Stück"
        ]

        st.dataframe(anzeigen, use_container_width=True)

        remove_options = {
            f"{item['artikelnummer']} | {item['name']} | Menge: {item['menge_stueck']} Stück": idx
            for idx, item in enumerate(st.session_state.warenkorb)
        }
        remove_selection = st.selectbox("Position zum Entfernen", list(remove_options.keys()))

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Gewählte Position entfernen"):
                idx = remove_options[remove_selection]
                st.session_state.warenkorb.pop(idx)
                st.success("Position entfernt.")
                st.rerun()
        with col2:
            if st.button("Warenkorb leeren"):
                st.session_state.warenkorb = []
                st.success("Warenkorb geleert.")
                st.rerun()

        st.markdown("### Lieferadresse")
        standardadresse = kunde_lieferadresse(kunde)
        lieferadresse = st.text_area("Lieferadresse", value=standardadresse, height=120)

        if st.button("Bestellung abschließen", type="primary"):
            try:
                bestellnummer = bestellung_speichern(
                    kunden_id=kunde["id"],
                    kunde_name_text=kunde_name(kunde),
                    lieferadresse=lieferadresse.strip(),
                    warenkorb=st.session_state.warenkorb
                )
                st.session_state.warenkorb = []
                st.success(f"Bestellung {bestellnummer} wurde gespeichert.")
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

    st.markdown("### Bestelldaten")
    col1, col2 = st.columns(2)

    with col1:
        st.write(f"**Bestellnummer:** {bestellung['bestellnummer']}")
        st.write(f"**Datum:** {bestellung['datum']}")
        st.write(f"**Uhrzeit:** {bestellung['uhrzeit']}")
        st.write(f"**Status:** {bestellung['status']}")

    with col2:
        st.write("**Lieferadresse:**")
        st.write(bestellung["lieferadresse"])

    st.markdown("### Positionen")
    st.dataframe(positionen, use_container_width=True)

    kom_text = build_kommissionierliste_text(bestellung, positionen)
    lief_text = build_lieferschein_text(bestellung, positionen)
    kom_pdf = generate_pdf_kommissionierliste(bestellung, positionen)
    lief_pdf = generate_pdf_lieferschein(bestellung, positionen)

    tab1, tab2 = st.tabs(["Kommissionierliste", "Lieferschein"])

    with tab1:
        st.text_area("Kommissionierliste Vorschau", value=kom_text, height=300)
        pdf_download_button(
            kom_pdf,
            f"Kommissionierliste_{bestellung['bestellnummer']}.pdf",
            "📄 PDF-Kommissionierliste herunterladen"
        )

    with tab2:
        st.text_area("Lieferschein Vorschau", value=lief_text, height=300)
        pdf_download_button(
            lief_pdf,
            f"Lieferschein_{bestellung['bestellnummer']}.pdf",
            "📄 PDF-Lieferschein herunterladen"
        )


# -------------------------------------------------
# Navigation
# -------------------------------------------------
def zeige_sidebar_internal():
    user = st.session_state.get("internal_user", {})
    rolle = user.get("rolle")

    st.sidebar.success(f"Interner Benutzer: {user.get('username')} ({rolle})")

    if st.sidebar.button("Logout"):
        st.session_state.internal_logged_in = False
        st.session_state.pop("internal_user", None)
        st.session_state.warenkorb = []
        st.rerun()

    menue = ["Lagerbestand", "Gesamtmonitor", "TV-Monitor", "Bestellungen"]

    if rolle in ["Admin", "Lagerist"]:
        menue += ["Wareneingang", "Artikel anlegen", "Artikel bearbeiten / löschen"]

    if rolle in ["Admin", "Vertrieb"]:
        menue += ["Kundenverwaltung"]

    if rolle == "Admin":
        menue += ["Benutzerverwaltung"]

    return st.sidebar.radio("Bereich auswählen", menue)


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
    st.set_page_config(page_title="Lagerwirtschaft", layout="wide")
    init_db()

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
        if menue == "Lagerbestand":
            zeige_lagerbestand()
        elif menue == "Gesamtmonitor":
            zeige_gesamtmonitor()
        elif menue == "TV-Monitor":
            zeige_tv_monitor()
        elif menue == "Bestellungen":
            zeige_bestellungen()
        elif menue == "Wareneingang":
            zeige_wareneingang()
        elif menue == "Artikel anlegen":
            zeige_artikel_anlegen()
        elif menue == "Artikel bearbeiten / löschen":
            zeige_artikel_bearbeiten_loeschen()
        elif menue == "Kundenverwaltung":
            zeige_kundenverwaltung()
        elif menue == "Benutzerverwaltung":
            zeige_benutzerverwaltung()
        return


if __name__ == "__main__":
    main()
