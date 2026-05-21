import os
from pathlib import Path
from PIL import Image, JpegImagePlugin
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

def setup_staging_dirs(base_path: str = "invoice_staging"):
    """Create the staging directory subfolders and a dummy directory README."""
    # Resolve relative to the script directory to keep it portably inside v3/
    base = Path(__file__).parent / base_path
    (base / "pending").mkdir(parents=True, exist_ok=True)
    (base / "approved").mkdir(parents=True, exist_ok=True)
    (base / "rejected").mkdir(parents=True, exist_ok=True)
    (base / "dummy").mkdir(parents=True, exist_ok=True)
    
    # Write README.md
    readme_path = base / "dummy" / "README.md"
    readme_path.write_text(
        "# Dummy Invoices Directory\n\n"
        "This folder contains synthetic test invoices generated for testing the pipeline:\n"
        "- `text_invoice_1.pdf`: Standard text-extractable invoice (easy case).\n"
        "- `scanned_invoice_1.pdf`: Rasterised image-only scanned invoice spanning 2 pages (hard case).\n",
        encoding="utf-8"
    )
    print(f"Staging folders initialized at: {base.resolve()}")

def draw_header_and_border(c, title, page_num, total_pages):
    """Draw a professional border and page number header."""
    c.setLineWidth(1)
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.rect(36, 36, 540, 720) # 0.5 inch margins
    c.setFont("Helvetica-Bold", 8)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(45, 740, title)
    c.drawRightString(567, 740, f"Page {page_num} of {total_pages}")

def create_text_invoice_1(filepath: Path):
    """Generate Dummy Invoice 1 — clean text PDF (easy case)"""
    print(f"Generating clean text invoice at: {filepath}")
    c = canvas.Canvas(str(filepath), pagesize=letter)
    
    # Setup page
    draw_header_and_border(c, "TAX INVOICE", 1, 1)
    
    # 1. Vendor Details (Top Right)
    c.setFont("Helvetica-Bold", 12)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawString(400, 710, "SUPREME OFFICE SUPPLIES")
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.drawString(400, 695, "ABN: 99 888 777 666")
    c.drawString(400, 680, "Email: sales@supremeoffice.com.au")
    c.drawString(400, 665, "Phone: 03 9876 5432")
    c.drawString(400, 650, "100 Supply Road, Melbourne VIC 3000")
    
    # 2. Invoice Meta Details (Top Left)
    c.setFont("Helvetica-Bold", 16)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawString(45, 700, "INVOICE")
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.drawString(45, 680, "Invoice Number: INV-2026-001")
    c.drawString(45, 665, "Invoice Date: 2026-05-21")
    c.drawString(45, 650, "Payment Due: 2026-06-21")
    
    # Divider line
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.line(45, 630, 567, 630)
    
    # 3. Bill To / Customer Details
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawString(45, 610, "BILL TO:")
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(45, 595, "Acme Pty Ltd")
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.drawString(45, 580, "ABN: 12 345 678 901")
    c.drawString(45, 565, "Email: accounts@acme.com.au")
    c.drawString(45, 550, "Phone: 02 9999 1234")
    c.drawString(45, 535, "42 Test Street, Sydney NSW 2000")
    
    # Divider line
    c.line(45, 515, 567, 515)
    
    # 4. Table Header
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawString(45, 495, "#")
    c.drawString(70, 495, "SKU")
    c.drawString(150, 495, "DESCRIPTION")
    c.drawRightString(400, 495, "QTY")
    c.drawRightString(480, 495, "UNIT PRICE")
    c.drawRightString(560, 495, "TOTAL")
    
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.line(45, 485, 567, 485)
    
    # 5. Table Rows
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    
    # Row 1
    c.drawString(45, 465, "1")
    c.drawString(70, 465, "PRN-001")
    c.drawString(150, 465, "Laser Printer Pro")
    c.drawRightString(400, 465, "1")
    c.drawRightString(480, 465, "$299.00")
    c.drawRightString(560, 465, "$299.00")
    
    # Row 2
    c.drawString(45, 440, "2")
    c.drawString(70, 440, "PAP-A4")
    c.drawString(150, 440, "Reflex A4 Paper Reams")
    c.drawRightString(400, 440, "10")
    c.drawRightString(480, 440, "$8.50")
    c.drawRightString(560, 440, "$85.00")
    
    # Row 3
    c.drawString(45, 415, "3")
    c.drawString(70, 415, "INK-BLK")
    c.drawString(150, 415, "Black Toner Cartridge")
    c.drawRightString(400, 415, "2")
    c.drawRightString(480, 415, "$120.00")
    c.drawRightString(560, 415, "$240.00")
    
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.line(45, 400, 567, 400)
    
    # 6. Totals Block (Right Aligned)
    c.setFont("Helvetica", 10)
    c.drawString(380, 375, "Subtotal:")
    c.drawRightString(560, 375, "$624.00")
    
    c.drawString(380, 355, "GST (10%):")
    c.drawRightString(560, 355, "$62.40")
    
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.1, 0.2, 0.4)
    c.drawString(380, 330, "Total Due:")
    c.drawRightString(560, 330, "$686.40")
    
    c.save()
    print(f"Successfully saved text_invoice_1.pdf")

def create_scanned_invoice_1(filepath: Path):
    """Generate Dummy Invoice 2 — minimal info PDF (hard case, missing customer fields)
    Spanning 2 pages, minimal labels, company name as a heading, no ABN or email.
    Saves a temporary text PDF, then rasterises it to images, and saves as image-only PDF.
    """
    print(f"Generating scanned 2-page invoice at: {filepath}")
    
    temp_pdf_path = filepath.parent / "temp_scanned_source.pdf"
    c = canvas.Canvas(str(temp_pdf_path), pagesize=letter)
    
    # ==================== PAGE 1 ====================
    draw_header_and_border(c, "INVOICE", 1, 2)
    
    # Vendor Heading (No label)
    c.setFont("Helvetica-Bold", 18)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(45, 700, "GLOBAL DYNAMICS")
    
    # Meta Info (Minimal)
    c.setFont("Helvetica", 9)
    c.drawString(45, 680, "Invoice: GD-55122")
    c.drawString(45, 665, "Date: 21 May 2026")
    
    # Customer Details (No labels like "Bill To", ABN or Email)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(45, 620, "Acme Pty Ltd")
    c.setFont("Helvetica", 9)
    c.drawString(45, 605, "42 Test Street")
    c.drawString(45, 590, "Sydney NSW 2000")
    
    # Divider line
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.line(45, 570, 567, 570)
    
    # Table Header (Minimal)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(45, 550, "#")
    c.drawString(70, 550, "SKU")
    c.drawString(140, 550, "ITEM")
    c.drawRightString(350, 550, "QTY")
    c.drawRightString(450, 550, "PRICE")
    c.drawRightString(560, 550, "TOTAL")
    c.line(45, 542, 567, 542)
    
    # Items (Page 1)
    c.setFont("Helvetica", 9)
    # Row 1
    c.drawString(45, 520, "1")
    c.drawString(70, 520, "DYN-01")
    c.drawString(140, 520, "Quantum Server Rack")
    c.drawRightString(350, 520, "1")
    c.drawRightString(450, 520, "1500.00")
    c.drawRightString(560, 520, "1500.00")
    
    # Row 2
    c.drawString(45, 490, "2")
    c.drawString(70, 490, "DYN-02")
    c.drawString(140, 490, "Supercomputer Cooling Fan")
    c.drawRightString(350, 490, "4")
    c.drawRightString(450, 490, "250.00")
    c.drawRightString(560, 490, "1000.00")
    
    # Row 3
    c.drawString(45, 460, "3")
    c.drawString(70, 460, "DYN-03")
    c.drawString(140, 460, "Liquid Nitrogen Canister")
    c.drawRightString(350, 460, "2")
    c.drawRightString(450, 460, "450.00")
    c.drawRightString(560, 460, "900.00")
    
    # Page Break
    c.showPage()
    
    # ==================== PAGE 2 ====================
    # Border
    draw_header_and_border(c, "INVOICE", 2, 2)
    
    # Items continue without repeated header!
    # Row 4
    c.setFont("Helvetica", 9)
    c.drawString(45, 700, "4")
    c.drawString(70, 700, "DYN-04")
    c.drawString(140, 700, "Holographic Projector Unit")
    c.drawRightString(350, 700, "1")
    c.drawRightString(450, 700, "3500.00")
    c.drawRightString(560, 700, "3500.00")
    
    # Row 5
    c.drawString(45, 670, "5")
    c.drawString(70, 670, "DYN-05")
    c.drawString(140, 670, "AI Processing Unit v2")
    c.drawRightString(350, 670, "2")
    c.drawRightString(450, 670, "1200.00")
    c.drawRightString(560, 670, "2400.00")
    
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.line(45, 650, 567, 650)
    
    # Totals (Page 2)
    c.setFont("Helvetica", 10)
    c.drawString(380, 620, "Subtotal:")
    c.drawRightString(560, 620, "9300.00")
    
    c.drawString(380, 600, "GST:")
    c.drawRightString(560, 600, "930.00")
    
    c.setFont("Helvetica-Bold", 11)
    c.drawString(380, 570, "Total:")
    c.drawRightString(560, 570, "10230.00")
    
    c.save()
    print(f"Generated source text PDF at: {temp_pdf_path}")
    
    # ==================== RASTERIZATION ====================
    # Convert PDF pages to images using fitz (PyMuPDF) and save as image-only PDF
    import fitz
    print("Rasterising source text PDF to simulate scanned invoice image...")
    doc = fitz.open(str(temp_pdf_path))
    images = []
    for i in range(len(doc)):
        fitz_page = doc.load_page(i)
        pix = fitz_page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append(img)
    doc.close()
    
    # Convert PIL Images to RGB and save as combined PDF
    rgb_images = [img.convert("RGB") for img in images]
    
    print(f"Saving rasterised image PDF to: {filepath}")
    rgb_images[0].save(
        str(filepath),
        save_all=True,
        append_images=rgb_images[1:]
    )
    
    # Clean up temp file
    if temp_pdf_path.exists():
        temp_pdf_path.unlink()
        
    print(f"Successfully saved scanned_invoice_1.pdf")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Synthetic Dummy Invoice Generator")
    parser.add_argument("--base", default="invoice_staging", help="Staging base folder name")
    args = parser.parse_args()
    
    setup_staging_dirs(args.base)
    
    dummy_dir = Path(__file__).parent / args.base / "dummy"
    
    pdf_text_path = dummy_dir / "text_invoice_1.pdf"
    pdf_scanned_path = dummy_dir / "scanned_invoice_1.pdf"
    
    create_text_invoice_1(pdf_text_path)
    print(f"Created: {pdf_text_path.resolve()}")
    
    create_scanned_invoice_1(pdf_scanned_path)
    print(f"Created: {pdf_scanned_path.resolve()}")
    
    print("\nGeneration completed. All test dummy invoices generated successfully!")
