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


def warenkorb_zusammenfassen(warenkorb):
    zusammen = {}
    for item in warenkorb:
        key = item["artikel_id"]
        if key not in zusammen:
            zusammen[key] = item.copy()
        else:
            zusammen[key]["menge_stueck"] += item["menge_stueck"]
    return list(zusammen.values())


def admin_ist_eingeloggt():
    return st.session_state.get("admin_logged_in", False)


def kunde_ist_eingeloggt():
    return "kunde" in st.session_state


def require_admin():
    if not admin_ist_eingeloggt():
        st.error("Dieser Bereich ist nur für Admins zugänglich.")
        st.stop()


# -------------------------------------------------
# Datenbank
# -------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
            bestand_stueck INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wareneingang (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artikel_id INTEGER NOT NULL,
            menge_stueck INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            passwort_hash TEXT NOT NULL,
            erstellt_am TEXT NOT NULL,
            ist_aktiv INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.commit()

    cur.execute("SELECT COUNT(*) AS anzahl FROM artikel")
    result = cur.fetchone()
    if result["anzahl"] == 0:
        demo_artikel = [
            ("MED-1001", "Verbandskasten", "Medizinlager", "Pack", 10, 50),
            ("VER-1002", "Einweghandschuhe", "Verbrauchslager", "Pack", 10, 120),
            ("MAT-1003", "Schrauben Set", "Materiallager", "Pack", 10, 80),
            ("TEC-1004", "Netzteil", "Techniklager", "Stück", 1, 15),
            ("MOE-1005", "Bürostuhl", "Möbellager", "Stück", 1, 8),
            ("LEB-1006", "Mineralwasser", "Lebensmittellager", "Pack", 10, 60),
            ("TEX-1007", "Arbeitshose", "Textillager", "Stück", 1, 20),
        ]
        cur.executemany("""
            INSERT INTO artikel
            (artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, bestand_stueck)
            VALUES (?, ?, ?, ?, ?, ?)
        """, demo_artikel)
        conn.commit()

    cur.execute("SELECT COUNT(*) AS anzahl FROM admin_users")
    admin_count = cur.fetchone()["anzahl"]
    if admin_count == 0:
        cur.execute("""
            INSERT INTO admin_users (username, passwort_hash, erstellt_am, ist_aktiv)
            VALUES (?, ?, ?, 1)
        """, (
            "admin",
            hash_password("admin123"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()

    conn.close()


# -------------------------------------------------
# Admin
# -------------------------------------------------
def admin_login(username: str, passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM admin_users
        WHERE username = ? AND passwort_hash = ? AND ist_aktiv = 1
    """, (username.strip(), hash_password(passwort)))
    row = cur.fetchone()
    conn.close()
    return row


def admin_passwort_aendern(admin_id: int, neues_passwort: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE admin_users
        SET passwort_hash = ?
        WHERE id = ?
    """, (hash_password(neues_passwort), admin_id))
    conn.commit()
    conn.close()


def zeige_admin_login():
    st.subheader("Admin-Anmeldung")

    with st.form("admin_login_form"):
        username = st.text_input("Admin Benutzername")
        passwort = st.text_input("Admin Passwort", type="password")
        senden = st.form_submit_button("Als Admin einloggen")

    if senden:
        admin = admin_login(username, passwort)
        if admin:
            st.session_state.admin_logged_in = True
            st.session_state.admin_user = dict(admin)
            st.success("Admin-Login erfolgreich.")
            st.rerun()
        else:
            st.error("Ungültiger Admin-Benutzername oder Passwort.")


def zeige_admin_einstellungen():
    require_admin()
    st.subheader("Admin-Einstellungen")

    admin = st.session_state.get("admin_user")
    st.write(f"**Eingeloggt als:** {admin['username']}")

    with st.form("admin_passwort_form"):
        neues_passwort = st.text_input("Neues Passwort", type="password")
        neues_passwort2 = st.text_input("Neues Passwort wiederholen", type="password")
        speichern = st.form_submit_button("Admin-Passwort ändern")

    if speichern:
        if not neues_passwort:
            st.error("Bitte ein neues Passwort eingeben.")
        elif neues_passwort != neues_passwort2:
            st.error("Die Passwörter stimmen nicht überein.")
        else:
            admin_passwort_aendern(admin["id"], neues_passwort)
            st.success("Admin-Passwort wurde geändert.")


# -------------------------------------------------
# Kunden
# -------------------------------------------------
def kundenname_komplett(kunde_row):
    firmenname = kunde_row["firmenname"] or ""
    vorname = kunde_row["vorname"] or ""
    nachname = kunde_row["nachname"] or ""

    if firmenname.strip():
        return f"{firmenname} / {vorname} {nachname}".strip()
    return f"{vorname} {nachname}".strip()


def kunden_lieferadresse_text(kunde_row):
    teile = []
    if kunde_row["firmenname"]:
        teile.append(kunde_row["firmenname"])
    teile.append(f"{kunde_row['vorname']} {kunde_row['nachname']}".strip())
    teile.append(kunde_row["strasse"])
    teile.append(f"{kunde_row['plz']} {kunde_row['ort']}")
    return "\n".join(teile)


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


def kunde_login(email, passwort):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM kunden
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


# -------------------------------------------------
# Artikel / Lager
# -------------------------------------------------
def artikel_df():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM artikel ORDER BY lager, name", conn)
    conn.close()

    if not df.empty:
        df["bestand_pack"] = df["bestand_stueck"] / df["inhalt_pro_pack"]
        df["bestand_pack"] = df["bestand_pack"].round(2)

    return df


def artikel_speichern(artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, bestand_stueck):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO artikel
        (artikelnummer, name, lager, verpackung_typ, inhalt_pro_pack, bestand_stueck)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        artikelnummer.strip(),
        name.strip(),
        lager,
        verpackung_typ,
        int(inhalt_pro_pack),
        int(bestand_stueck),
    ))
    conn.commit()
    conn.close()


def wareneingang_buchen(artikel_id: int, menge_stueck: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE artikel SET bestand_stueck = bestand_stueck + ? WHERE id = ?",
        (menge_stueck, artikel_id),
    )
    cur.execute(
        "INSERT INTO wareneingang (artikel_id, menge_stueck, datum) VALUES (?, ?, ?)",
        (artikel_id, menge_stueck, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


# -------------------------------------------------
# Bestellungen
# -------------------------------------------------
def bestellung_speichern(kunden_id: int, kunde_name: str, lieferadresse: str, warenkorb: list):
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
            raise ValueError(f"Zu wenig Bestand für {pos['name']}. Verfügbar: {artikel['bestand_stueck']} Stück.")

    cur.execute("""
        INSERT INTO bestellungen (
            bestellnummer, kunden_id, kunde_name, lieferadresse, datum, uhrzeit
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        bestellnummer,
        kunden_id,
        kunde_name,
        lieferadresse,
        jetzt.strftime("%d.%m.%Y"),
        jetzt.strftime("%H:%M:%S"),
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


def hole_bestellpositionen(bestell_id: int):
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT
            bp.menge_stueck,
            a.artikelnummer,
            a.name,
            a.lager,
            a.verpackung_typ,
            a.inhalt_pro_pack
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
def build_kommissionierliste_text(bestellung, positionen):
    lines = []
    lines.append("KOMMISSIONIERLISTE")
    lines.append("")
    lines.append(f"Bestellnummer: {bestellung['bestellnummer']}")
    lines.append(f"Datum: {bestellung['datum']}")
    lines.append(f"Uhrzeit: {bestellung['uhrzeit']}")
    lines.append(f"Kunde: {bestellung['kunde_name']}")
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
    lines.append("")
    lines.append("Bestellte Materialien:")
    lines.append("")

    for _, pos in positionen.iterrows():
        lines.append(
            f"- {pos['artikelnummer']} | {pos['name']} | Menge: {pos['menge_stueck']} Stück"
        )

    return "\n".join(lines)


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
# UI
# -------------------------------------------------
def zeige_kunden_login_registrierung():
    st.subheader("Kundenkonto")

    tab1, tab2 = st.tabs(["Login", "Registrierung"])

    with tab1:
        with st.form("login_form"):
            email = st.text_input("E-Mail")
            passwort = st.text_input("Passwort", type="password")
            login = st.form_submit_button("Einloggen")

        if login:
            kunde = kunde_login(email, passwort)
            if kunde:
                st.session_state.kunde = dict(kunde)
                st.session_state.warenkorb = []
                st.success("Login erfolgreich.")
                st.rerun()
            else:
                st.error("Ungültige E-Mail oder Passwort.")

    with tab2:
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


def zeige_lagerbestand():
    st.subheader("Lagerbestand")
    df = artikel_df()

    lager_filter = st.selectbox("Unterlager filtern", ["Alle"] + LAGER)
    if lager_filter != "Alle":
        df = df[df["lager"] == lager_filter]

    if df.empty:
        st.info("Keine Artikel vorhanden.")
        return

    anzeigen = df[[
        "artikelnummer",
        "name",
        "lager",
        "verpackung_typ",
        "inhalt_pro_pack",
        "bestand_stueck",
        "bestand_pack"
    ]].copy()

    anzeigen.columns = [
        "Artikelnummer",
        "Bezeichnung",
        "Lager",
        "Verpackung",
        "Inhalt pro Pack",
        "Bestand Stück",
        "Bestand Pack"
    ]

    st.dataframe(anzeigen, use_container_width=True)


def zeige_wareneingang():
    require_admin()
    st.subheader("Wareneingang buchen")

    df = artikel_df()
    if df.empty:
        st.warning("Keine Artikel vorhanden.")
        return

    artikel_map = {
        f"{row['artikelnummer']} | {row['name']} | {row['lager']}": int(row["id"])
        for _, row in df.iterrows()
    }

    auswahl = st.selectbox("Artikel", list(artikel_map.keys()))
    menge = st.number_input("Anzahl Stück", min_value=1, step=1)

    if st.button("Wareneingang buchen"):
        wareneingang_buchen(artikel_map[auswahl], int(menge))
        st.success("Wareneingang erfolgreich gebucht.")
        st.rerun()


def zeige_artikel_anlegen():
    require_admin()
    st.subheader("Neuen Artikel anlegen")

    with st.form("artikel_form"):
        artikelnummer = st.text_input("Artikelnummer")
        name = st.text_input("Artikelbezeichnung")
        lager = st.selectbox("Unterlager", LAGER)
        verpackung_typ = st.selectbox("Verpackungsgröße", ["Stück", "Pack"])
        inhalt_pro_pack = st.number_input("Stück pro Pack", min_value=1, value=10, step=1)
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
                    bestand_stueck,
                )
                st.success("Artikel wurde gespeichert.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Die Artikelnummer existiert bereits.")


def zeige_kundenverwaltung():
    require_admin()
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

    st.markdown("### Lagerfreigaben")
    neue_freigaben = st.multiselect(
        "Erlaubte Unterlager für diesen Kunden",
        options=LAGER,
        default=erlaubte_lager
    )

    if st.button("Lagerfreigaben speichern"):
        setze_lagerfreigaben_fuer_kunde(kunden_id, neue_freigaben)
        st.success("Lagerfreigaben gespeichert.")
        st.rerun()

    st.markdown("### Kundendaten")
    st.write(f"**Kundennummer:** {kunde['kunden_nr']}")
    st.write(f"**Name:** {kunde['vorname']} {kunde['nachname']}")
    st.write(f"**E-Mail:** {kunde['email']}")
    st.write(f"**Telefon:** {kunde['telefon']}")
    st.write(f"**Adresse:** {kunde['strasse']}, {kunde['plz']} {kunde['ort']}")


def zeige_shop():
    st.subheader("Shopfunktion")

    if not kunde_ist_eingeloggt():
        st.warning("Bitte zuerst registrieren oder einloggen, bevor bestellt werden kann.")
        zeige_kunden_login_registrierung()
        return

    kunde = st.session_state.kunde
    erlaubte_lager = hole_erlaubte_lager_fuer_kunde(kunde["id"])

    st.success(f"Eingeloggt als: {kunde['vorname']} {kunde['nachname']} ({kunde['email']})")

    if not erlaubte_lager:
        st.error("Für diesen Kunden sind aktuell keine Unterlager freigeschaltet.")
        return

    df = artikel_df()
    verfuegbar = df[(df["bestand_stueck"] > 0) & (df["lager"].isin(erlaubte_lager))].copy()

    st.info("Sichtbar sind nur die für den Kunden freigegebenen Unterlager.")

    if verfuegbar.empty:
        st.warning("Aktuell sind keine Artikel in den freigegebenen Lagern verfügbar.")
        return

    lager_filter = st.selectbox("Lager auswählen", ["Alle"] + erlaubte_lager, key="shop_lager")
    if lager_filter != "Alle":
        verfuegbar = verfuegbar[verfuegbar["lager"] == lager_filter]

    if verfuegbar.empty:
        st.info("In diesem Lager sind keine Artikel verfügbar.")
        return

    artikel_map = {
        f"{row['artikelnummer']} | {row['name']} | Bestand: {row['bestand_stueck']} Stück": row
        for _, row in verfuegbar.iterrows()
    }

    ausgewaehlt = st.selectbox("Artikel auswählen", list(artikel_map.keys()))
    row = artikel_map[ausgewaehlt]

    menge = st.number_input(
        "Bestellmenge in Stück",
        min_value=1,
        max_value=int(row["bestand_stueck"]),
        value=1,
        step=1
    )

    if st.button("In den Warenkorb"):
        st.session_state.warenkorb.append({
            "artikel_id": int(row["id"]),
            "artikelnummer": row["artikelnummer"],
            "name": row["name"],
            "lager": row["lager"],
            "menge_stueck": int(menge),
        })
        st.session_state.warenkorb = warenkorb_zusammenfassen(st.session_state.warenkorb)
        st.success("Artikel wurde in den Warenkorb gelegt.")
        st.rerun()

    st.markdown("### Warenkorb")

    if st.session_state.warenkorb:
        warenkorb_df = pd.DataFrame(st.session_state.warenkorb)
        anzeigen = warenkorb_df[["artikelnummer", "name", "lager", "menge_stueck"]].copy()
        anzeigen.columns = ["Artikelnummer", "Bezeichnung", "Lager", "Menge Stück"]
        st.dataframe(anzeigen, use_container_width=True)

        remove_options = {
            f"{item['artikelnummer']} | {item['name']} | Menge: {item['menge_stueck']}": idx
            for idx, item in enumerate(st.session_state.warenkorb)
        }

        auswahl_remove = st.selectbox("Position zum Entfernen", list(remove_options.keys()))

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Gewählte Position entfernen"):
                idx = remove_options[auswahl_remove]
                st.session_state.warenkorb.pop(idx)
                st.success("Position entfernt.")
                st.rerun()
        with col2:
            if st.button("Warenkorb leeren"):
                st.session_state.warenkorb = []
                st.success("Warenkorb geleert.")
                st.rerun()

        st.markdown("### Lieferadresse")
        standardadresse = kunden_lieferadresse_text(kunde)
        lieferadresse = st.text_area("Lieferadresse", value=standardadresse, height=120)

        if st.button("Bestellung abschließen", type="primary"):
            try:
                bestellnummer = bestellung_speichern(
                    kunden_id=kunde["id"],
                    kunde_name=kundenname_komplett(kunde),
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


def zeige_bestellungen():
    require_admin()
    st.subheader("Bestellungen, Kommissionierliste und Lieferschein")

    bestellungen = hole_bestellungen()
    if bestellungen.empty:
        st.info("Es gibt noch keine Bestellungen.")
        return

    auswahl_map = {
        f"{row['bestellnummer']} | {row['kunde_name']} | {row['datum']} {row['uhrzeit']}": row
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
        st.text_area("Kommissionierliste Vorschau", value=kom_text, height=350)
        col1, col2 = st.columns(2)
        with col1:
            pdf_download_button(
                kom_pdf,
                f"Kommissionierliste_{bestellung['bestellnummer']}.pdf",
                "📄 PDF-Kommissionierliste herunterladen"
            )
        with col2:
            render_print_button(
                title=f"Kommissionierliste {bestellung['bestellnummer']}",
                text_content=kom_text,
                button_label="🖨️ Kommissionierliste drucken"
            )

    with tab2:
        st.text_area("Lieferschein Vorschau", value=lief_text, height=350)
        col1, col2 = st.columns(2)
        with col1:
            pdf_download_button(
                lief_pdf,
                f"Lieferschein_{bestellung['bestellnummer']}.pdf",
                "📄 PDF-Lieferschein herunterladen"
            )
        with col2:
            render_print_button(
                title=f"Lieferschein {bestellung['bestellnummer']}",
                text_content=lief_text,
                button_label="🖨️ Lieferschein drucken"
            )


def main():
    st.set_page_config(page_title="Lagerwirtschaft", layout="wide")
    init_db()

    if "warenkorb" not in st.session_state:
        st.session_state.warenkorb = []

    st.title("📦 Lagerwirtschaft mit Admin-Anmeldung, Kundenregistrierung und Shop")

    st.sidebar.markdown("## Anmeldung")

    if admin_ist_eingeloggt():
        admin = st.session_state.get("admin_user", {})
        st.sidebar.success(f"Admin: {admin.get('username', '')}")
        if st.sidebar.button("Admin Logout"):
            st.session_state["admin_logged_in"] = False
            st.session_state.pop("admin_user", None)
            st.rerun()
    else:
        st.sidebar.info("Kein Admin eingeloggt")

    if kunde_ist_eingeloggt():
        kunde = st.session_state.kunde
        st.sidebar.success(f"Kunde:\n{kunde['vorname']} {kunde['nachname']}")
        if st.sidebar.button("Kunden Logout"):
            st.session_state.pop("kunde", None)
            st.session_state.warenkorb = []
            st.rerun()
    else:
        st.sidebar.info("Kein Kunde eingeloggt")

    menu = st.sidebar.radio(
        "Bereich auswählen",
        [
            "Lagerbestand",
            "Kunden Login / Registrierung",
            "Shop",
            "Admin Login",
            "Wareneingang",
            "Artikel anlegen",
            "Kundenverwaltung",
            "Bestellungen",
            "Admin Einstellungen",
        ]
    )

    if menu == "Lagerbestand":
        zeige_lagerbestand()
    elif menu == "Kunden Login / Registrierung":
        zeige_kunden_login_registrierung()
    elif menu == "Shop":
        zeige_shop()
    elif menu == "Admin Login":
        zeige_admin_login()
    elif menu == "Wareneingang":
        zeige_wareneingang()
    elif menu == "Artikel anlegen":
        zeige_artikel_anlegen()
    elif menu == "Kundenverwaltung":
        zeige_kundenverwaltung()
    elif menu == "Bestellungen":
        zeige_bestellungen()
    elif menu == "Admin Einstellungen":
        zeige_admin_einstellungen()


if __name__ == "__main__":
    main()
