"""Regenerate the generated stress files: the 30-line invoice and the two-page PDF.

Usage: uv run --with fpdf2 python data/stress/generate.py
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent


def thirty_lines() -> None:
    lines = [
        "INVOICE",
        "Vendor: Atlas Industrial Supply",
        "Invoice Number: INV-2005",
        "Date: 2026-02-05",
        "Due Date: 2026-04-06",
        "",
        "Item        Qty   Unit Price   Amount",
    ]
    total = 0
    for i in range(30):
        item, price = [("WidgetA", 250), ("WidgetB", 500), ("GadgetX", 750)][i % 3]
        lines.append(f"{item:<11} 1     ${price:>8.2f}   ${price:>8.2f}   (lot {i + 1})")
        total += price
    lines += ["", f"Subtotal: ${total:,.2f}", "Tax (0%): $0.00", f"Total: ${total:,.2f}", "Payment Terms: Net 60"]
    (HERE / "stress_2005.txt").write_text("\n".join(lines) + "\n")
    print("wrote stress_2005.txt")


def two_page_pdf() -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "INVOICE", ln=True)
    pdf.set_font("Helvetica", "", 11)
    for line in [
        "Invoice Number: INV-2007",
        "Vendor: Summit Manufacturing Co.",
        "Date: 2026-02-07",
        "Due Date: 2026-03-09",
    ]:
        pdf.cell(0, 7, line, ln=True)
    pdf.ln(4)
    pdf.cell(0, 7, "Item          Qty    Unit Price    Amount", ln=True)
    pdf.cell(0, 7, "WidgetA        2       $250.00     $500.00", ln=True)
    pdf.cell(0, 7, "WidgetB        1       $500.00     $500.00", ln=True)
    pdf.cell(0, 7, "(continued on page 2)", ln=True)
    pdf.add_page()
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, "Page 2 of 2 - INV-2007", ln=True)
    pdf.ln(4)
    for line in ["Subtotal: $1,000.00", "Tax (0%): $0.00", "Total: $1,000.00", "Payment Terms: Net 30"]:
        pdf.cell(0, 7, line, ln=True)
    pdf.output(str(HERE / "stress_2007.pdf"))
    print("wrote stress_2007.pdf")


if __name__ == "__main__":
    thirty_lines()
    two_page_pdf()
