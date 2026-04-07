import base64
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


# -----------------------------
# Datenbank
# -----------------------------
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
        CREATE TABLE IF NOT EXISTS bestellungen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bestellnummer TEXT NOT NULL,
            kunde TEXT NOT NULL,
            lieferadresse TEXT NOT NULL,
            datum TEXT NOT NULL,
            uhrzeit TEXT NOT NULL
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

    conn.close()


def artikel_df():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM artikel ORDER BY lager, name", conn)
    conn.close()

    if not df.empty:
        df["bestand_pack"] = df["bestand_stueck"] / df["inhalt_pro_pack"]
        df["bestand_pack"] = df["bestand_pack"].round(2)
    return df


def hole_artikel_nach_id(artikel_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,))
    row = cur.fetchone()
    conn.close()
    return row


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


def bestellung_speichern(kunde: str, lieferadresse: str, warenkorb: list):
    jetzt = datetime.now()
    bestellnummer = f"B-{jetzt.strftime('%Y%m%d%H%M%S')}"

    conn = get_connection()
    cur = conn.cursor()

    # Bestandsprüfung direkt vor dem Speichern
    for pos in warenkorb:
        cur.execute("SELECT bestand_stueck FROM artikel WHERE id = ?", (pos["artikel_id"],))
        bestand = cur.fetchone()["bestand_stueck"]
        if bestand < pos["menge_stueck"]:
            conn.close()
            raise ValueError(f"Zu wenig Bestand für {pos['name']}. Verfügbar: {bestand} Stück.")

    cur.execute("""
        INSERT INTO bestellungen (bestellnummer, kunde, lieferadresse, datum, uhrzeit)
        VALUES (?, ?, ?, ?, ?)
    """, (
        bestellnummer,
        kunde,
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
    df = pd.read_sql_query("SELECT * FROM bestellungen ORDER BY id DESC", conn)
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


# -----------------------------
# Hilfsfunktionen
# -----------------------------
def warenkorb_zusammenfassen(warenkorb):
    zusammen = {}
    for item in warenkorb:
        key = item["artikel_id"]
        if key not in zusammen:
            zusammen[key] = item.copy()
        else:
            zusammen[key]["menge_stueck"] += item["menge_stueck"]
    return list(zusammen.values())


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


def build_kommissionierliste_text(bestellung, positionen):
    lines = []
    lines.append("KOMMISSIONIERLISTE")
    lines.append("")
    lines.append(f"Bestellnummer: {bestellung['bestellnummer']}")
    lines.append(f"Datum: {bestellung['datum']}")
    lines.append(f"Uhrzeit: {bestellung['uhrzeit']}")
    lines.append(f"Kunde: {bestellung['kunde']}")
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
            .print-btn:hover {{
                opacity: 0.9;
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
        ["Kunde", bestellung["kunde"]],
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
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
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
        ["Kunde", bestellung["kunde"]],
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


# -----------------------------
# UI
# -----------------------------
def main():
    st.set_page_config(page_title="Lagerwirtschaft", layout="wide")
    init_db()

    st.title("📦 Lagerwirtschaft mit Shop, Kommissionierliste und Lieferschein")

    if "warenkorb" not in st.session_state:
        st.session_state.warenkorb = []

    menu = st.sidebar.radio(
        "Bereich auswählen",
        ["Lagerbestand", "Wareneingang", "Artikel anlegen", "Shop", "Bestellungen"]
    )

    # -------------------------
    # Lagerbestand
    # -------------------------
    if menu == "Lagerbestand":
        st.subheader("Lagerbestand")
        df = artikel_df()

        lager_filter = st.selectbox("Unterlager filtern", ["Alle"] + LAGER)
        if lager_filter != "Alle":
            df = df[df["lager"] == lager_filter]

        if df.empty:
            st.info("Keine Artikel vorhanden.")
        else:
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

    # -------------------------
    # Wareneingang
    # -------------------------
    elif menu == "Wareneingang":
        st.subheader("Wareneingang buchen")
        df = artikel_df()

        if df.empty:
            st.warning("Keine Artikel vorhanden.")
        else:
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

    # -------------------------
    # Artikel anlegen
    # -------------------------
    elif menu == "Artikel anlegen":
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

    # -------------------------
    # Shop
    # -------------------------
    elif menu == "Shop":
        st.subheader("Shopfunktion")
        df = artikel_df()
        verfuegbar = df[df["bestand_stueck"] > 0].copy()

        if verfuegbar.empty:
            st.warning("Aktuell sind keine Artikel verfügbar.")
        else:
            lager_filter = st.selectbox("Lager auswählen", ["Alle"] + LAGER, key="shop_lager")
            if lager_filter != "Alle":
                verfuegbar = verfuegbar[verfuegbar["lager"] == lager_filter]

            if verfuegbar.empty:
                st.info("In diesem Lager sind keine Artikel verfügbar.")
            else:
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

            st.markdown("#### Position entfernen")
            remove_options = {
                f"{item['artikelnummer']} | {item['name']} | Menge: {item['menge_stueck']}": idx
                for idx, item in enumerate(st.session_state.warenkorb)
            }
            remove_key = st.selectbox("Warenkorb-Position auswählen", list(remove_options.keys()))
            col_remove1, col_remove2 = st.columns(2)

            with col_remove1:
                if st.button("Gewählte Position entfernen"):
                    idx = remove_options[remove_key]
                    st.session_state.warenkorb.pop(idx)
                    st.success("Position entfernt.")
                    st.rerun()

            with col_remove2:
                if st.button("Warenkorb leeren"):
                    st.session_state.warenkorb = []
                    st.success("Warenkorb wurde geleert.")
                    st.rerun()

            st.markdown("#### Kundendaten")
            kunde = st.text_input("Kundenname")
            lieferadresse = st.text_area("Lieferadresse")

            if st.button("Bestellung abschließen", type="primary"):
                if not kunde.strip() or not lieferadresse.strip():
                    st.error("Bitte Kundenname und Lieferadresse ausfüllen.")
                else:
                    try:
                        bestellnummer = bestellung_speichern(
                            kunde,
                            lieferadresse,
                            st.session_state.warenkorb
                        )
                        st.session_state.warenkorb = []
                        st.success(f"Bestellung {bestellnummer} wurde gespeichert.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        else:
            st.info("Der Warenkorb ist leer.")

    # -------------------------
    # Bestellungen
    # -------------------------
    elif menu == "Bestellungen":
        st.subheader("Bestellungen, Kommissionierliste und Lieferschein")
        bestellungen = hole_bestellungen()

        if bestellungen.empty:
            st.info("Es gibt noch keine Bestellungen.")
        else:
            auswahl_map = {
                f"{row['bestellnummer']} | {row['kunde']} | {row['datum']} {row['uhrzeit']}": row
                for _, row in bestellungen.iterrows()
            }

            auswahl = st.selectbox("Bestellung auswählen", list(auswahl_map.keys()))
            bestellung = auswahl_map[auswahl]
            positionen = hole_bestellpositionen(int(bestellung["id"]))

            st.markdown("### Bestelldaten")
            col1, col2 = st.columns(2)

            with col1:
                st.write(f"**Bestellnummer:** {bestellung['bestellnummer']}")
                st.write(f"**Kunde:** {bestellung['kunde']}")
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
                st.text_area(
                    "Kommissionierliste Vorschau",
                    value=kom_text,
                    height=350
                )
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
                st.text_area(
                    "Lieferschein Vorschau",
                    value=lief_text,
                    height=350
                )
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


if __name__ == "__main__":
    main()
