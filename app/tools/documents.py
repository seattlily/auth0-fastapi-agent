"""Document generation for the Compass0 demo.

Pure-stdlib PDF writer — emits a one-page, Helvetica-only PDF 1.4 file
without any external dependencies. Good enough for mock contracts and
invoices; not a general-purpose PDF library.

PDFs land in `app/documents/` (created on demand). The Documents page
serves them through a permission-checked FastAPI route, never as
static assets.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


_DOCUMENTS_DIR_NAME = "documents"

# Helvetica metrics aren't exposed without bundling AFM files, so we
# approximate. ~90 chars at size 11 fits the page width with the 50pt
# left margin; bigger fonts wrap sooner.
_WRAP_BY_SIZE = {9: 110, 10: 100, 11: 90, 12: 82, 14: 70, 18: 55}


def documents_dir() -> Path:
    here = Path(__file__).resolve().parent.parent
    out = here / _DOCUMENTS_DIR_NAME
    out.mkdir(exist_ok=True)
    return out


# ---------- PDF writer ----------


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, size: int) -> list[str]:
    width = _WRAP_BY_SIZE.get(size, 90)
    if len(text) <= width:
        return [text]
    out: list[str] = []
    line: list[str] = []
    used = 0
    for word in text.split(" "):
        w = len(word)
        if line and used + 1 + w > width:
            out.append(" ".join(line))
            line, used = [word], w
        else:
            line.append(word)
            used = used + (1 if used else 0) + w
    if line:
        out.append(" ".join(line))
    return out


def write_pdf(path: Path, blocks: Sequence[tuple[str, int, bool]]) -> Path:
    """Write a single-page Letter PDF.

    blocks: each tuple is (text, font_size, bold). Empty strings render
    as blank lines. Long lines are word-wrapped to fit the page width.
    """
    content_lines: list[str] = ["BT"]
    y = 760  # start near the top of the page (Letter is 792pt tall)
    for text, size, bold in blocks:
        if not text:
            y -= max(size, 11) + 4
            continue
        font = "F2" if bold else "F1"
        for piece in _wrap(text, size):
            content_lines.append(f"/{font} {size} Tf")
            content_lines.append(f"1 0 0 1 50 {y} Tm")
            content_lines.append(f"({_escape(piece)}) Tj")
            y -= size + 4
        y -= 2

    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1", errors="replace")
    content_obj = (
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream"
    )

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /Resources "
            b"<< /Font << /F1 4 0 R /F2 5 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents 6 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        content_obj,
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(out)
    out += b"xref\n"
    out += f"0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += b"trailer\n"
    out += f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("ascii")
    out += b"startxref\n"
    out += f"{xref_offset}\n".encode("ascii")
    out += b"%%EOF"

    path.write_bytes(bytes(out))
    return path


# ---------- Mock contract / invoice writers ----------


def _today() -> str:
    return datetime.utcnow().strftime("%B %d, %Y")


def generate_contract_pdf(
    org_name: str,
    display_name: str,
    output_path: Path | None = None,
) -> Path:
    if output_path is None:
        output_path = documents_dir() / f"contract-{org_name}.pdf"

    blocks: list[tuple[str, int, bool]] = [
        ("Compass0", 18, True),
        ("Travel Management Services Agreement", 12, False),
        ("", 11, False),
        (f"Effective date: {_today()}", 10, False),
        ("", 10, False),
        ("Parties", 12, True),
        (
            "This agreement is entered into between Compass0, Inc. (\"Compass0\") "
            f"and {display_name} (\"Client\"; Auth0 organization slug: {org_name}).",
            11,
            False,
        ),
        ("", 11, False),
        ("1. Scope of services", 12, True),
        (
            "Compass0 will provide the Client with a managed travel-booking platform, "
            "including agent-assisted reservations, customer self-service, and itinerary "
            "management. Service availability targets a 99.9 percent monthly uptime.",
            11,
            False,
        ),
        ("", 11, False),
        ("2. Term", 12, True),
        (
            "Initial term: twelve (12) months from the effective date, automatically "
            "renewing for successive twelve-month periods unless either party provides "
            "thirty (30) days written notice of non-renewal.",
            11,
            False,
        ),
        ("", 11, False),
        ("3. Fees", 12, True),
        (
            "Client agrees to a base annual service fee, plus per-booking transaction "
            "fees as set out in Schedule A. Schedule A is a placeholder in this demo "
            "build and carries no commercial commitments.",
            11,
            False,
        ),
        ("", 11, False),
        ("4. Authorized users", 12, True),
        (
            "Client travel agents and customers will authenticate via Auth0 within the "
            f"organization \"{org_name}\". Compass0 administrators will provision and "
            "deprovision agents on Client's instruction. Compass0 may suspend any "
            "user account that violates the platform's acceptable-use policy.",
            11,
            False,
        ),
        ("", 11, False),
        ("5. Data protection", 12, True),
        (
            "Compass0 processes booking, customer, and traveler data on Client's "
            "behalf as a processor under the parties' Data Processing Addendum. "
            "Authentication tokens, MFA enrollments, and audit logs are retained per "
            "the security policy attached as Schedule B.",
            11,
            False,
        ),
        ("", 11, False),
        ("6. Signatures", 12, True),
        ("", 11, False),
        ("Compass0, Inc.", 11, True),
        ("By: ___________________________   Title: ___________________________", 10, False),
        ("Date: ___________________________", 10, False),
        ("", 11, False),
        (display_name, 11, True),
        ("By: ___________________________   Title: ___________________________", 10, False),
        ("Date: ___________________________", 10, False),
    ]
    return write_pdf(output_path, blocks)


def generate_invoice_pdf(
    trip: dict,
    customer: dict,
    company: dict | None,
    output_path: Path | None = None,
) -> Path:
    if output_path is None:
        output_path = documents_dir() / f"invoice-{trip['id']}.pdf"

    base = float(trip["cost"])
    taxes = round(base * 0.08, 2)
    fees = round(base * 0.02, 2)
    total = round(base + taxes + fees, 2)
    currency = trip.get("currency", "USD")

    org_line = (
        f"Organization: {company['display_name']} ({company['org_name']})"
        if company
        else f"Organization: {customer.get('org_name', '—')}"
    )

    blocks: list[tuple[str, int, bool]] = [
        ("Compass0", 18, True),
        (f"Invoice {trip['id'].upper().replace('TR_', 'INV-')}", 12, False),
        ("", 11, False),
        (f"Issued: {_today()}", 10, False),
        ("Payment terms: net 30", 10, False),
        ("", 10, False),
        ("Bill to", 12, True),
        (f"{customer['name']} ({customer['email']})", 11, False),
        (org_line, 11, False),
        ("", 11, False),
        ("Itinerary", 12, True),
        (f"Type: {trip['type'].title()}", 11, False),
        (f"Route: {trip['origin']} to {trip['destination']}", 11, False),
        (f"Depart: {trip['depart_date']}    Return: {trip['return_date']}", 11, False),
        (f"Status: {trip['status']}", 11, False),
        ("", 11, False),
        ("Charges", 12, True),
        (f"Base fare:                                {currency} {base:,.2f}", 11, False),
        (f"Taxes (est. 8 percent):                   {currency} {taxes:,.2f}", 11, False),
        (f"Booking and service fee (est. 2 percent): {currency} {fees:,.2f}", 11, False),
        ("", 11, False),
        (f"Total due:                                {currency} {total:,.2f}", 12, True),
        ("", 11, False),
        (
            "Questions? Reach your Compass0 travel agent or reply to this invoice. "
            "All amounts are illustrative for the workshop demo.",
            10,
            False,
        ),
    ]
    return write_pdf(output_path, blocks)
