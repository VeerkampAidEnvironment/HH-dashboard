"""Branded server-side PDF reports."""

from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


TEAL = colors.HexColor("#087880")
ORANGE = colors.HexColor("#EFB417")
LIGHT_GREEN = colors.HexColor("#C2ED45")
INK = colors.HexColor("#282828")
SURFACE = colors.HexColor("#F5F5F5")
MUTED = colors.HexColor("#6B7280")
LINE = colors.HexColor("#D7E2E3")
PALE_TEAL = colors.HexColor("#EAF6F6")
PALE_ORANGE = colors.HexColor("#FFF3D5")
PALE_RED = colors.HexColor("#FDE8E5")
PALE_GREEN = colors.HexColor("#EDF7DF")
PALE_BLUE = colors.HexColor("#E8F3F8")


def _styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
            fontSize=22, leading=27, textColor=TEAL, alignment=TA_LEFT, spaceAfter=4 * mm,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle", parent=styles["Normal"], fontSize=9, leading=13,
            textColor=MUTED, spaceAfter=5 * mm,
        ),
        "section": ParagraphStyle(
            "ReportSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
            fontSize=12, leading=15, textColor=TEAL, spaceBefore=5 * mm, spaceAfter=2.5 * mm,
        ),
        "body": ParagraphStyle(
            "ReportBody", parent=styles["BodyText"], fontSize=8.5, leading=11, textColor=INK,
        ),
        "small": ParagraphStyle(
            "ReportSmall", parent=styles["BodyText"], fontSize=7.2, leading=9, textColor=INK,
        ),
        "kpi_label": ParagraphStyle(
            "KpiLabel", parent=styles["Normal"], fontSize=7, leading=9,
            textColor=MUTED, alignment=TA_LEFT,
        ),
        "kpi_value": ParagraphStyle(
            "KpiValue", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=18, leading=20, textColor=TEAL, alignment=TA_LEFT,
        ),
        "group_title": ParagraphStyle(
            "GroupTitle", parent=styles["Title"], fontName="Helvetica-Bold",
            fontSize=20, leading=23, textColor=INK, alignment=TA_LEFT, spaceAfter=1.5 * mm,
        ),
        "kicker": ParagraphStyle(
            "ReportKicker", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=7, leading=9, textColor=TEAL, spaceAfter=1.5 * mm,
            textTransform="uppercase",
        ),
        "matrix": ParagraphStyle(
            "MatrixText", parent=styles["Normal"], fontSize=6.3, leading=7.5,
            textColor=INK,
        ),
        "matrix_header": ParagraphStyle(
            "MatrixHeader", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=5.8, leading=7, textColor=colors.white, alignment=TA_CENTER,
        ),
        "center_small": ParagraphStyle(
            "CenterSmall", parent=styles["Normal"], fontSize=7, leading=8.5,
            textColor=INK, alignment=TA_CENTER,
        ),
    }


def _header_footer(canvas, doc):
    canvas.saveState()
    width, height = landscape(A4)
    canvas.setFillColor(TEAL)
    canvas.rect(0, height - 10 * mm, width, 10 * mm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(14 * mm, height - 6.5 * mm, "ARFSA Monitoring Database")
    canvas.setFillColor(ORANGE)
    canvas.rect(0, height - 10.8 * mm, width, 0.8 * mm, stroke=0, fill=1)
    canvas.setStrokeColor(colors.HexColor("#D1D5DB"))
    canvas.line(14 * mm, 10 * mm, width - 14 * mm, 10 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(14 * mm, 6 * mm, "Internal monitoring report")
    canvas.drawRightString(width - 14 * mm, 6 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _kpi_table(summary, styles):
    cards = [
        ("Source records", summary.get("total_records", 0)),
        ("Unique farmers", summary.get("unique_farmers", 0)),
        ("Received training", summary.get("trained", 0)),
        ("Confirmed adoption", summary.get("confirmed", 0)),
        ("Follow-up needed", summary.get("followup", 0)),
        ("Training needed", summary.get("retraining", 0)),
    ]
    cells = []
    for label, value in cards:
        cells.append([
            Paragraph(str(value), styles["kpi_value"]),
            Paragraph(label, styles["kpi_label"]),
        ])
    table = Table([cells], colWidths=[42 * mm] * len(cells), rowHeights=[22 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E6E7")),
        ("INNERGRID", (0, 0), (-1, -1), 1.5, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
    ]))
    return table


def _topic_table(topics, styles):
    header = [
        Paragraph("Topic", styles["small"]),
        "Participants", "Received", "Confirmed", "Follow-up", "Training needed", "Waiting",
    ]
    rows = [header]
    for topic in topics:
        rows.append([
            Paragraph(str(topic["topic"]), styles["small"]),
            topic.get("participants", 0), topic.get("trained", 0), topic.get("confirmed", 0),
            topic.get("followup", 0), topic.get("retraining", 0), topic.get("waiting", 0),
        ])
    table = Table(rows, colWidths=[91 * mm, 25 * mm, 23 * mm, 23 * mm, 23 * mm, 29 * mm, 21 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
        ("LINEBELOW", (0, 0), (-1, 0), 1, ORANGE),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D1D5DB")),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
    ]))
    return table


def build_dashboard_pdf(title: str, summary: dict, topics: list[dict], filters: list[str] | None = None,
                        groups: list[str] | None = None, priorities: list[dict] | None = None) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=17 * mm, bottomMargin=15 * mm, title=title, author="ARFSA",
    )
    styles = _styles()
    generated = datetime.now().strftime("%d %B %Y, %H:%M")
    filter_text = " | ".join(filters or ["No filters applied"])
    story = [
        Paragraph(title, styles["title"]),
        Paragraph(f"Generated {generated}<br/>{filter_text}", styles["subtitle"]),
        _kpi_table(summary, styles),
        Paragraph("Training progress by topic", styles["section"]),
        _topic_table(topics, styles),
    ]
    if groups:
        story.extend([
            Paragraph("Farmer groups", styles["section"]),
            Paragraph(", ".join(groups), styles["body"]),
        ])
    if priorities:
        story.extend([PageBreak(), Paragraph("Farmer follow-up priorities", styles["section"])])
        rows = [["Farmer", "Farmer ID", "Follow-ups", "Training needs", "Group"]]
        for item in priorities:
            rows.append([
                Paragraph(str(item.get("name", "")), styles["small"]),
                item.get("uid", ""), item.get("followup", 0), item.get("retraining", 0),
                Paragraph(str(item.get("group_name", "")), styles["small"]),
            ])
        priority_table = Table(rows, colWidths=[55 * mm, 30 * mm, 27 * mm, 30 * mm, 95 * mm], repeatRows=1)
        priority_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), TEAL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D1D5DB")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
        ]))
        story.append(priority_table)
    document.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return buffer.getvalue()


def _metric_cards(cards, styles):
    cells = []
    for label, value, detail in cards:
        cells.append([
            Paragraph(escape(str(value)), styles["kpi_value"]),
            Paragraph(escape(str(label)), styles["kpi_label"]),
            Paragraph(escape(str(detail)), styles["small"]),
        ])
    width = 252 * mm / max(len(cells), 1)
    table = Table([cells], colWidths=[width] * len(cells), rowHeights=[25 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.7, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 1.2, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
    ]))
    return table


TOPIC_COLUMNS = [
    ("PIP", "Household Resource Mapping (PIP)"),
    ("SWC", "SWC"),
    ("Kitchen", "Kitchen garden Establishment and Vegetable growing"),
    ("Bio", "Bio-inputs training"),
    ("Poultry", "Poultry Mgt and Vaccination"),
    ("Finance", "Financial Literacy"),
    ("Trees", "Tree planting/Agroforestry"),
    ("Regen", "Sustainable/Regenerative Agriculture"),
]


def _cbf_topic_summary(report, styles):
    by_topic = {item["topic"]: item for item in report["topics"]}
    rows = [[
        Paragraph("Topic", styles["matrix_header"]),
        Paragraph("Active farmers", styles["matrix_header"]),
        Paragraph("Follow-up (FU)", styles["matrix_header"]),
        Paragraph("Training (CT)", styles["matrix_header"]),
        Paragraph("Completed", styles["matrix_header"]),
        Paragraph("Waiting", styles["matrix_header"]),
    ]]
    for _short, topic in TOPIC_COLUMNS:
        item = by_topic.get(topic, {})
        rows.append([
            Paragraph(escape(topic), styles["small"]),
            item.get("participants", 0), item.get("followup", 0), item.get("retraining", 0),
            item.get("confirmed", 0), item.get("waiting", 0),
        ])
    table = Table(rows, colWidths=[116 * mm, 28 * mm, 28 * mm, 28 * mm, 26 * mm, 26 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, -1), 7),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, ORANGE),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))
    return table


def _cbf_farmer_matrix(report, styles):
    header = [
        Paragraph("Farmer", styles["matrix_header"]),
        Paragraph("Farmer ID", styles["matrix_header"]),
        Paragraph("Actions", styles["matrix_header"]),
    ] + [Paragraph(short, styles["matrix_header"]) for short, _topic in TOPIC_COLUMNS]
    rows = [header]
    cell_codes = []
    for row_index, farmer in enumerate(report["farmers"], start=1):
        codes = [farmer["topic_status"].get(topic, "-") for _short, topic in TOPIC_COLUMNS]
        rows.append([
            Paragraph(escape(farmer["name"]), styles["matrix"]),
            Paragraph(escape(farmer["uid"] or "-"), styles["matrix"]),
            farmer["action_count"],
            *codes,
        ])
        cell_codes.extend((row_index, column + 3, code) for column, code in enumerate(codes))
    table = Table(
        rows,
        colWidths=[43 * mm, 24 * mm, 15 * mm] + [21.25 * mm] * len(TOPIC_COLUMNS),
        repeatRows=1,
        splitByRow=1,
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, -1), 6.2),
        ("ALIGN", (2, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (2, -1), [colors.white, SURFACE]),
        ("GRID", (0, 0), (-1, -1), 0.3, LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, ORANGE),
        ("TOPPADDING", (0, 1), (-1, -1), 1.5 * mm),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 1.5 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm),
    ]
    palette = {
        "FU": (PALE_ORANGE, colors.HexColor("#8A5B00")),
        "CT": (PALE_RED, colors.HexColor("#A63A2D")),
        "OK": (PALE_GREEN, colors.HexColor("#4B6F25")),
        "WAIT": (colors.HexColor("#EEF2F3"), colors.HexColor("#667477")),
        "RV": (PALE_BLUE, colors.HexColor("#315F73")),
        "-": (colors.white, MUTED),
    }
    for row_index, column, code in cell_codes:
        background, text = palette.get(code, palette["-"])
        commands.extend([
            ("BACKGROUND", (column, row_index), (column, row_index), background),
            ("TEXTCOLOR", (column, row_index), (column, row_index), text),
            ("FONTNAME", (column, row_index), (column, row_index), "Helvetica-Bold"),
        ])
    table.setStyle(TableStyle(commands))
    return table


def build_cbf_report_pdf(cbf_name: str, group_reports: list[dict]) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=17 * mm, bottomMargin=15 * mm,
        title=f"CBF action report - {cbf_name}", author="ARFSA",
    )
    styles = _styles()
    story = []
    generated = datetime.now().strftime("%d %B %Y, %H:%M")
    for index, report in enumerate(group_reports):
        if index:
            story.append(PageBreak())
        story.extend([
            Paragraph("CBF GROUP ACTION OVERVIEW", styles["kicker"]),
            Paragraph(escape(report["group_name"]), styles["group_title"]),
            Paragraph(
                f"CBF: <b>{escape(cbf_name)}</b> &nbsp;&nbsp;|&nbsp;&nbsp; Generated {generated} &nbsp;&nbsp;|&nbsp;&nbsp; "
                "Action cells are prioritised for field planning.",
                styles["subtitle"],
            ),
            _metric_cards([
                ("Active farmers", report["active_count"], "Dropouts excluded from actions"),
                ("Need follow-up", report["fu_people"], "At least one FU topic"),
                ("Need training", report["ct_people"], "Centralized or refresher"),
                ("Action entries", report["action_entries"], "FU and CT topic assignments"),
                ("Dropped", len(report["dropped"]), "Listed separately"),
            ], styles),
            Paragraph("Action summary by topic", styles["section"]),
            _cbf_topic_summary(report, styles),
            Paragraph("Farmer action matrix", styles["section"]),
            Paragraph(
                "<b>FU</b> = follow-up required &nbsp;&nbsp; <b>CT</b> = centralized or refresher training required &nbsp;&nbsp; "
                "<b>OK</b> = confirmed &nbsp;&nbsp; <b>WAIT</b> = follow-up window not yet due &nbsp;&nbsp; <b>RV</b> = review data",
                styles["small"],
            ),
            Spacer(1, 2 * mm),
            _cbf_farmer_matrix(report, styles),
            Paragraph("Topic key", styles["section"]),
            Paragraph(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(
                f"<b>{escape(short)}</b>: {escape(topic)}" for short, topic in TOPIC_COLUMNS
            ), styles["small"]),
        ])
        if report["dropped"]:
            story.extend([
                Paragraph("Dropped participants", styles["section"]),
                Paragraph(", ".join(escape(name) for name in report["dropped"]), styles["small"]),
            ])
    document.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return buffer.getvalue()


def build_fh_dashboard_pdf(data: dict, filters: list[str] | None = None) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=17 * mm, bottomMargin=15 * mm,
        title="FH attendance dashboard", author="ARFSA",
    )
    styles = _styles()
    summary = data["summary"]
    story = [
        Paragraph("FH attendance dashboard", styles["title"]),
        Paragraph(
            f"Generated {datetime.now().strftime('%d %B %Y, %H:%M')}<br/>{escape(' | '.join(filters or ['No filters applied']))}",
            styles["subtitle"],
        ),
        _metric_cards([
            ("Participants", summary["total_records"], f"{summary['active']} active"),
            ("Reached", summary["reached"], "Attended at least once"),
            ("Attendance", f"{summary['attendance_rate']}%", "Among recorded entries"),
            ("Recording", f"{summary['recording_rate']}%", f"{summary['marked']} entries"),
            ("Groups / schools", summary["groups"], "FH delivery units"),
            ("Completed modules", summary["completed_modules"], f"{summary['in_progress_modules']} in progress"),
        ], styles),
        Paragraph("Programme tracks", styles["section"]),
    ]
    track_rows = [["Track", "Participants", "Groups", "Reached", "Attended entries", "Absent entries", "Attendance", "Recorded"]]
    for item in data["track_stats"]:
        track_rows.append([item["label"], item["participants"], item["groups"], item["reached"], item["attended"], item["absent"], f"{item['attendance_rate']}%", f"{item['recording_rate']}%"])
    track_table = Table(track_rows, colWidths=[55 * mm, 27 * mm, 24 * mm, 25 * mm, 32 * mm, 29 * mm, 27 * mm, 27 * mm])
    track_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"), ("GRID", (0, 0), (-1, -1), .35, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))
    story.extend([track_table, Paragraph("FH module delivery", styles["section"])])
    module_rows = [["Track / module", "Sessions", "Eligible", "Reached", "Completed", "Attended", "Absent", "Attendance", "Recorded"]]
    for item in data["modules"]:
        module_rows.append([
            Paragraph(f"<b>{escape(item['track_label'])}</b><br/>{escape(item['label'])}", styles["small"]),
            item["sessions"], item["participants"], item["started"], item["completed"],
            item["attended"], item["absent"], f"{item['attendance_rate']}%", f"{item['recording_rate']}%",
        ])
    module_table = Table(module_rows, colWidths=[80 * mm, 20 * mm, 24 * mm, 23 * mm, 25 * mm, 25 * mm, 22 * mm, 25 * mm, 24 * mm], repeatRows=1)
    module_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 6.8),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), .35, LINE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, ORANGE),
        ("TOPPADDING", (0, 0), (-1, -1), 1.8 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 * mm),
    ]))
    story.extend([module_table, Paragraph("Groups and schools", styles["section"])])
    group_rows = [["Group or school", "Track", "Participants", "Reached", "Attended", "Absent", "Attendance", "Recorded"]]
    for item in data["group_stats"]:
        group_rows.append([
            Paragraph(escape(item["group_name"]), styles["small"]), item["track"], item["participants"],
            item["reached"], item["attended"], item["absent"], f"{item['attendance_rate']}%", f"{item['recording_rate']}%",
        ])
    group_table = Table(group_rows, colWidths=[90 * mm, 31 * mm, 27 * mm, 25 * mm, 25 * mm, 23 * mm, 27 * mm, 27 * mm], repeatRows=1)
    group_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ALIGN", (2, 1), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), .35, LINE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
        ("TOPPADDING", (0, 0), (-1, -1), 1.7 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.7 * mm),
    ]))
    story.append(group_table)
    document.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return buffer.getvalue()
