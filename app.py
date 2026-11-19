import hashlib
import json
import logging
import os
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


LOGGER = logging.getLogger("invoice_app")
DB_PATH = os.getenv("DB_PATH", "./data/invoices.db")
MNB_URL = "http://www.mnb.hu/arfolyamok.asmx"
MNB_NAMESPACE = "http://www.mnb.hu/webservices/"
SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SUPPORTED_CURRENCIES = {"EUR", "USD", "GBP", "PLN"}
COMPANY_TAX_DIGITS = "26481937"
COMPANY_NAMES = ("nordwindsolutions", "nordwindsolutionskft")
MAX_PDF_BYTES = 25 * 1024 * 1024

MONTHS = {
    "januar": 1, "january": 1, "januar": 1, "janvier": 1, "enero": 1, "gennaio": 1, "stycznia": 1,
    "februar": 2, "february": 2, "fevrier": 2, "febrero": 2, "febbraio": 2, "lutego": 2,
    "marcius": 3, "march": 3, "marz": 3, "mars": 3, "marzo": 3, "marca": 3,
    "aprilis": 4, "april": 4, "avril": 4, "abril": 4, "kwietnia": 4,
    "majus": 5, "may": 5, "mai": 5, "mayo": 5, "maggio": 5, "maja": 5,
    "junius": 6, "june": 6, "juni": 6, "juin": 6, "junio": 6, "giugno": 6, "czerwca": 6,
    "julius": 7, "july": 7, "juli": 7, "juillet": 7, "julio": 7, "luglio": 7, "lipca": 7,
    "augusztus": 8, "august": 8, "aout": 8, "agosto": 8, "sierpnia": 8,
    "szeptember": 9, "september": 9, "septembre": 9, "septiembre": 9, "settembre": 9, "wrzesnia": 9,
    "oktober": 10, "october": 10, "octobre": 10, "octubre": 10, "ottobre": 10, "pazdziernika": 10,
    "november": 11, "novembre": 11, "noviembre": 11, "listopada": 11,
    "december": 12, "decembre": 12, "diciembre": 12, "dicembre": 12, "grudnia": 12,
}

app = FastAPI(title="Számlafeldolgozó", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")


class InvoiceError(Exception):
    pass


@contextmanager
def database(db_path: str = DB_PATH):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(db_path: str = DB_PATH) -> None:
    with database(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY,
                issuer_key TEXT NOT NULL,
                invoice_number TEXT NOT NULL,
                seller_name TEXT NOT NULL,
                seller_tax_id TEXT,
                buyer_name TEXT NOT NULL,
                buyer_tax_id TEXT,
                issue_date TEXT NOT NULL,
                service_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                gross_amount TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('bejövő', 'kimenő')),
                partner_name TEXT NOT NULL,
                partner_tax_id TEXT,
                exchange_rate TEXT NOT NULL,
                exchange_rate_date TEXT NOT NULL,
                huf_amount INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (issuer_key, invoice_number)
            );
            CREATE TABLE IF NOT EXISTS processing_jobs (
                fingerprint TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS exchange_rates (
                currency TEXT NOT NULL,
                rate_date TEXT NOT NULL,
                rate TEXT NOT NULL,
                unit INTEGER NOT NULL,
                PRIMARY KEY (currency, rate_date)
            );
            CREATE TABLE IF NOT EXISTS exchange_rate_intervals (
                currency TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                PRIMARY KEY (currency, start_date, end_date)
            );
            """
        )


def normalize_key(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").casefold())
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]", "", normalized)


def normalize_tax_id(value: str | None) -> str:
    text = (value or "").strip()
    hungarian = re.fullmatch(r"(?:HU)?(\d{8})(?:-\d-\d{2})?", text, re.IGNORECASE)
    if hungarian:
        return hungarian.group(1)
    return normalize_key(text)


def parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        raise InvoiceError("A szükséges dátum hiányzik.")
    normalized = text.replace("/", "-").replace(".", "-").strip("- ")
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            pass

    year_month_pattern = re.compile(r"(\d{4})[.,]?\s+([A-Za-zÀ-ÿ]+)\s+(\d{1,2})\.?", re.IGNORECASE)
    month_pattern = re.compile(r"(\d{1,2})\.?\s+(?:de\s+)?([A-Za-zÀ-ÿ]+)(?:\s+de)?\s+(\d{4})", re.IGNORECASE)
    english_pattern = re.compile(r"([A-Za-zÀ-ÿ]+)\s+(\d{1,2}),?\s+(\d{4})", re.IGNORECASE)
    match = year_month_pattern.search(text) or month_pattern.search(text) or english_pattern.search(text)
    if match:
        groups = match.groups()
        if match.re is year_month_pattern:
            year, month_name, day = int(groups[0]), groups[1], int(groups[2])
        elif groups[0].isdigit():
            day, month_name, year = int(groups[0]), groups[1], int(groups[2])
        else:
            month_name, day, year = groups[0], int(groups[1]), int(groups[2])
        month_key = unicodedata.normalize("NFKD", month_name.casefold())
        month_key = "".join(character for character in month_key if not unicodedata.combining(character))
        month_key = re.sub(r"[^a-z]", "", month_key)
        month = MONTHS.get(month_key)
        if month:
            return date(year, month, day)
    raise InvoiceError(f"Nem értelmezhető dátum: {text}")


def parse_amount(value: Any) -> Decimal:
    text = str(value or "").strip()
    text = re.sub(r"[^\d,.\-'\u2019\u00a0\s]", "", text)
    text = text.replace("'", "").replace("\u2019", "").replace("\u00a0", "").replace(" ", "")
    if not text or not re.search(r"\d", text):
        raise InvoiceError(f"Nem értelmezhető összeg: {value}")
    comma, dot = text.rfind(","), text.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_separator = "," if comma > dot else "."
        thousands_separator = "." if decimal_separator == "," else ","
        text = text.replace(thousands_separator, "")
        if decimal_separator == ",":
            text = text.replace(",", ".")
    elif comma >= 0:
        parts = text.split(",")
        text = "".join(parts[:-1]) + ("." + parts[-1] if len(parts[-1]) in (1, 2) else parts[-1])
    elif dot >= 0:
        parts = text.split(".")
        if len(parts) > 2:
            text = "".join(parts[:-1]) + ("." + parts[-1] if len(parts[-1]) in (1, 2) else parts[-1])
        elif len(parts[-1]) not in (1, 2):
            text = "".join(parts)
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise InvoiceError(f"Nem értelmezhető összeg: {value}") from error
    if amount <= 0:
        raise InvoiceError("A bruttó végösszegnek pozitívnak kell lennie.")
    return amount


def _find_label_value(lines: list[str], labels: tuple[str, ...]) -> str | None:
    for index, line in enumerate(lines):
        lowered = line.casefold()
        for label in labels:
            position = lowered.find(label)
            if position >= 0:
                tail = line[position + len(label):].strip(" \t:#-–")
                if tail:
                    return tail
                if index + 1 < len(lines):
                    return lines[index + 1].strip()
    return None


def _find_invoice_number(lines: list[str]) -> str | None:
    labels = (
        "invoice number", "invoice no.", "invoice no", "invoice #", "számlaszám", "számla száma",
        "rechnungsnummer", "rechnung nr", "n° de facture", "nº de factura", "facture n°", "facture no",
        "número de factura", "numero de factura", "numero fattura n.", "numero fattura", "fattura n.",
        "fattura n°", "numer faktury",
    )
    for index, line in enumerate(lines):
        lowered = line.casefold()
        for label in labels:
            position = lowered.find(label)
            if position < 0:
                continue
            value = line[position + len(label):].strip(" \t:#-–.")
            if not value and index + 1 < len(lines):
                value = lines[index + 1].strip()
            value = value.strip(" \t:#-–.")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]{1,80}", value):
                return value
    return None


def _amount_from_text(value: str | None) -> Decimal | None:
    if not value:
        return None
    matches = re.findall(r"[-+]?\d[\d\s.,'’\u00a0]*\d|[-+]?\d", value)
    for match in reversed(matches):
        try:
            return parse_amount(match)
        except InvoiceError:
            continue
    return None


def extract_with_rules(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    invoice_number = _find_invoice_number(lines)
    issue_text = _find_label_value(lines, ("issue date", "invoice date", "kiállítás dátuma", "kiállítás kelte",
                                           "date d'émission", "date de facture", "fecha de emisión", "fecha de factura", "rechnungsdatum",
                                           "data fattura", "data wystawienia"))
    service_text = _find_label_value(lines, ("service date", "date of supply", "supply date", "performance date", "teljesítés dátuma",
                                             "teljesítési dátum", "delivery date", "leistungsdatum",
                                             "date de prestation", "fecha de servicio", "fecha de operación",
                                             "data prestazione", "data esecuzione", "data sprzedaży", "data wykonania"))
    due_text = _find_label_value(lines, ("due date", "payment due", "fizetési határidő", "fizetési határid",
                                         "fälligkeit", "zahlbar bis", "date d'échéance", "fecha de vencimiento",
                                         "scadenza", "data scadenza", "termin płatności", "payment deadline"))
    currency_text = _find_label_value(
        lines, ("currency", "pénznem", "devise", "währung", "wahrung", "moneda", "valuta", "waluta", "moneta")
    )
    currency_match = re.search(r"\b(HUF|EUR|USD|GBP|PLN)\b|€|£|\$", currency_text or "", re.IGNORECASE)
    if not currency_match:
        currency_match = re.search(r"\b(HUF|EUR|USD|GBP|PLN)\b|€|£|\$", text, re.IGNORECASE)
    currency = currency_match.group(1).upper() if currency_match and currency_match.group(1) else (
        "EUR" if currency_match and currency_match.group(0) == "€" else
        "GBP" if currency_match and currency_match.group(0) == "£" else
        "USD" if currency_match and currency_match.group(0) == "$" else None
    )

    gross_text = _find_label_value(
        lines, ("amount due", "total due", "grand total", "total incl", "gross amount", "total gross",
                "bruttó végösszeg", "fizetendő összesen", "fizetendő összeg", "fizetendő", "végösszeg", "bruttó összeg",
                "endbetrag", "gesamtbetrag", "rechnungsbetrag", "total ttc", "net à payer ttc", "net à payer",
                "montant total", "total bruto", "importe total", "total a pagar", "totale documento",
                "totale fattura", "da pagare", "do zapłaty")
    )
    gross_amount = _amount_from_text(gross_text)

    seller_name = _find_label_value(lines, ("seller:", "from:", "kiállító:", "eladó:", "szállító:",
                                            "verkauft von", "vendeur:", "vendedor:", "venditore:", "sprzedawca:"))
    buyer_name = _find_label_value(
        lines, ("buyer:", "buyer", "bill to:", "bill to", "billed to:", "vevő:", "vevő", "megrendelő:",
                "kunde:", "client:", "client", "cliente:", "cliente", "comprador:", "nabywca:",
                "facturé à", "facturado a", "facturato a", "rechnungsempfänger", "destinataire:",
                "cessionario / committente")
    )
    buyer_markers = (
        "bill to", "billed to", "buyer", "vevő", "megrendelő", "kunde", "client", "cliente",
        "comprador", "nabywca", "facturé à", "facturado a", "facturato a", "rechnungsempfänger",
        "destinataire", "cessionario / committente",
    )
    buyer_marker = next(
        (index for index, line in enumerate(lines)
         if any(marker in line.casefold() for marker in buyer_markers)),
        len(lines),
    )
    if seller_name is None:
        excluded = ("invoice", "számla", "rechn", "facture", "factura", "fattura", "faktura",
                    "invoice number", "számlaszám", "adószám", "tax id", "vat", "bill to", "buyer",
                    "vevő", "date", "dátum", "due", "határid", "currency", "pénznem")
        for line in lines[:buyer_marker]:
            lowered = line.casefold()
            if any(word in lowered for word in excluded) or not re.search(r"[A-Za-zÀ-ÿ]{3}", line):
                continue
            seller_name = line
            break
    if buyer_name and any(word in buyer_name.casefold() for word in ("tax", "vat", "adószám")):
        buyer_name = None
    if buyer_name is None and buyer_marker < len(lines):
        for line in lines[buyer_marker + 1:]:
            lowered = line.casefold()
            if any(word in lowered for word in ("tax", "vat", "adószám", "invoice", "számla", "date", "dátum")):
                continue
            if re.search(r"[A-Za-zÀ-ÿ]{3}", line):
                buyer_name = line
                break
    tax_pattern = re.compile(r"\b(?:[A-Z]{2}\s?)?\d{8,12}(?:-\d-\d{2})?\b", re.IGNORECASE)

    def tax_from_segment(segment: list[str]) -> str | None:
        for line in segment:
            lowered = line.casefold()
            if not any(label in lowered for label in (
                "tax", "vat", "adószám", "fiscal", "tva", "nif", "nip", "ust", "idnr", "identyfikacji",
            )):
                continue
            match = tax_pattern.search(line)
            if match:
                return match.group(0).replace(" ", "")
        return None

    seller_tax = tax_from_segment(lines[:buyer_marker])
    buyer_tax = tax_from_segment(lines[buyer_marker:]) if buyer_marker < len(lines) else None

    missing = [
        name for name, value in (
            ("számlaszám", invoice_number), ("kiállító", seller_name), ("vevő", buyer_name),
            ("kiállítás dátuma", issue_text), ("teljesítés dátuma", service_text),
            ("fizetési határidő", due_text), ("bruttó összeg", gross_amount), ("pénznem", currency),
        ) if not value
    ]
    if missing:
        raise InvoiceError("Nem nyerhetők ki megbízhatóan ezek a mezők: " + ", ".join(missing) + ".")
    return {
        "invoice_number": invoice_number.strip(),
        "seller_name": seller_name.strip(),
        "seller_tax_id": seller_tax,
        "buyer_name": buyer_name.strip(),
        "buyer_tax_id": buyer_tax,
        "issue_date": parse_date(issue_text).isoformat(),
        "service_date": parse_date(service_text).isoformat(),
        "due_date": parse_date(due_text).isoformat(),
        "currency": currency,
        "gross_amount": str(gross_amount),
    }


def extract_with_llm(text: str) -> dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise InvoiceError("Ehhez a számlaformátumhoz OpenRouter-kulcs szükséges, de nincs beállítva.")
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
    prompt = (
        "Extract the requested data from this invoice. The document may be Hungarian, English, German, "
        "French, Spanish, Italian or Polish and may span multiple pages. Return only a JSON object with "
        "invoice_number, seller_name, seller_tax_id, buyer_name, buyer_tax_id, issue_date, service_date, "
        "due_date, currency, gross_amount. Dates must be YYYY-MM-DD. gross_amount must be the final amount "
        "payable including VAT, as a decimal string without currency symbols. Do not confuse issue date, "
        "performance/supply date, and due date. Use null for a tax id only if it is not shown. "
        "Do not infer missing dates or amounts. Invoice text follows:\n\n" + text[:40000]
    )
    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0},
        timeout=(10, 90),
    )
    response.raise_for_status()
    try:
        content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("JSON object not found")
        result = json.loads(match.group(0))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvoiceError("Az adatkinyerő szolgáltatás érvénytelen választ adott.") from error
    required = ("invoice_number", "seller_name", "buyer_name", "issue_date", "service_date",
                "due_date", "currency", "gross_amount")
    if any(not result.get(field) for field in required):
        raise InvoiceError("Az adatkinyerő válaszából kötelező számlaadat hiányzik.")
    return result


def extract_invoice(text: str) -> dict[str, Any]:
    try:
        invoice = extract_with_rules(text)
        validate_invoice(invoice)
        return invoice
    except InvoiceError:
        if not os.getenv("OPENROUTER_API_KEY", "").strip():
            raise
        return extract_with_llm(text)


def validate_invoice(raw: dict[str, Any]) -> dict[str, Any]:
    invoice = dict(raw)
    for field in ("invoice_number", "seller_name", "buyer_name"):
        invoice[field] = str(invoice.get(field) or "").strip()
        if not invoice[field]:
            raise InvoiceError(f"Hiányzó kötelező mező: {field}")
    invoice["issue_date"] = parse_date(invoice["issue_date"]).isoformat()
    invoice["service_date"] = parse_date(invoice["service_date"]).isoformat()
    invoice["due_date"] = parse_date(invoice["due_date"]).isoformat()
    invoice["currency"] = str(invoice.get("currency") or "").upper().strip()
    if invoice["currency"] != "HUF" and invoice["currency"] not in SUPPORTED_CURRENCIES:
        raise InvoiceError(f"Nem támogatott pénznem: {invoice['currency']}")
    invoice["gross_amount"] = parse_amount(invoice["gross_amount"])

    seller_identity = normalize_tax_id(invoice.get("seller_tax_id"))
    buyer_identity = normalize_tax_id(invoice.get("buyer_tax_id"))
    seller_name_key = normalize_key(invoice["seller_name"])
    buyer_name_key = normalize_key(invoice["buyer_name"])
    ours_as_seller = COMPANY_TAX_DIGITS in seller_identity or any(name in seller_name_key for name in COMPANY_NAMES)
    ours_as_buyer = COMPANY_TAX_DIGITS in buyer_identity or any(name in buyer_name_key for name in COMPANY_NAMES)
    if ours_as_seller == ours_as_buyer:
        raise InvoiceError("A számla iránya nem állapítható meg egyértelműen a cégadatok alapján.")
    invoice["direction"] = "kimenő" if ours_as_seller else "bejövő"
    if ours_as_seller:
        invoice["partner_name"] = invoice["buyer_name"]
        invoice["partner_tax_id"] = invoice.get("buyer_tax_id")
    else:
        invoice["partner_name"] = invoice["seller_name"]
        invoice["partner_tax_id"] = invoice.get("seller_tax_id")
    invoice["issuer_key"] = normalize_tax_id(invoice["seller_tax_id"]) or normalize_key(invoice["seller_name"])
    return invoice


def fetch_mnb_rates(start: date, end: date, currency: str) -> list[tuple[str, str, int]]:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_NAMESPACE}" xmlns:web="{MNB_NAMESPACE}">'
        "<soap:Body><web:GetExchangeRates>"
        f"<web:startDate>{start.isoformat()}</web:startDate>"
        f"<web:endDate>{end.isoformat()}</web:endDate>"
        f"<web:currencyNames>{currency}</web:currencyNames>"
        "</web:GetExchangeRates></soap:Body></soap:Envelope>"
    )
    response = requests.post(
        MNB_URL,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{MNB_NAMESPACE}MNBArfolyamServiceSoap/GetExchangeRates"',
        },
        timeout=(10, 45),
    )
    response.raise_for_status()
    try:
        soap_root = ET.fromstring(response.content)
        result_text = next(element.text for element in soap_root.iter() if element.tag.endswith("GetExchangeRatesResult"))
        rates_root = ET.fromstring(result_text or "<MNBExchangeRates />")
    except (ET.ParseError, StopIteration) as error:
        raise InvoiceError("Az MNB árfolyamválasza nem értelmezhető.") from error
    rates: list[tuple[str, str, int]] = []
    for day in rates_root.iter():
        if not day.tag.endswith("Day"):
            continue
        rate_date = day.attrib.get("date")
        for rate in list(day):
            if rate.attrib.get("curr", "").upper() != currency:
                continue
            try:
                value = Decimal((rate.text or "").replace(",", "."))
                unit = int(rate.attrib["unit"])
                if unit <= 0:
                    raise ValueError("invalid unit")
            except (InvalidOperation, KeyError, ValueError) as error:
                raise InvoiceError("Az MNB hibás árfolyamadatot adott.") from error
            if rate_date:
                rates.append((rate_date, str(value), unit))
    return rates


def get_exchange_rate(target: date, currency: str, db_path: str = DB_PATH) -> tuple[Decimal, date]:
    if currency == "HUF":
        return Decimal("1"), target
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise InvoiceError(f"Nem támogatott pénznem: {currency}")
    with database(db_path) as connection:
        covered = connection.execute(
            "SELECT 1 FROM exchange_rate_intervals WHERE currency=? AND start_date<=? AND end_date>=? LIMIT 1",
            (currency, target.isoformat(), target.isoformat()),
        ).fetchone()
        if not covered:
            previous = connection.execute(
                "SELECT MAX(end_date) AS end_date FROM exchange_rate_intervals WHERE currency=? AND end_date<?",
                (currency, target.isoformat()),
            ).fetchone()
            start = date.fromisoformat(previous["end_date"]) + timedelta(days=1) if previous and previous["end_date"] else target - timedelta(days=14)
            rates = fetch_mnb_rates(start, target, currency)
            connection.executemany(
                "INSERT OR IGNORE INTO exchange_rates(currency, rate_date, rate, unit) VALUES(?,?,?,?)",
                [(currency, rate_date, value, unit) for rate_date, value, unit in rates],
            )
            connection.execute(
                "INSERT OR IGNORE INTO exchange_rate_intervals(currency, start_date, end_date) VALUES(?,?,?)",
                (currency, start.isoformat(), target.isoformat()),
            )
        row = connection.execute(
            "SELECT rate, rate_date, unit FROM exchange_rates WHERE currency=? AND rate_date<=? "
            "ORDER BY rate_date DESC LIMIT 1",
            (currency, target.isoformat()),
        ).fetchone()
    if row:
        return Decimal(row["rate"]) / Decimal(row["unit"]), date.fromisoformat(row["rate_date"])
    raise InvoiceError(f"Az MNB-nél nem található {currency} árfolyam {target.isoformat()} napján vagy előtte.")


def convert_to_huf(amount: Decimal, rate: Decimal) -> int:
    return int((amount * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_dashboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    invoices = [row for row in rows if row.get("status", "processed") == "processed"]
    partners: dict[str, dict[str, Any]] = {}
    monthly: dict[str, dict[str, int]] = {}
    for row in invoices:
        partner_key = normalize_tax_id(row.get("partner_tax_id")) or normalize_key(row["partner_name"])
        partner = partners.setdefault(
            partner_key,
            {"name": row["partner_name"], "tax_id": row.get("partner_tax_id"), "bejövő": 0, "kimenő": 0},
        )
        if not partner.get("tax_id") and row.get("partner_tax_id"):
            partner["tax_id"] = row["partner_tax_id"]
        partner[row["direction"]] += int(row["huf_amount"])

        due_month = row["due_date"][:7]
        month = monthly.setdefault(due_month, {"bejövő": 0, "kimenő": 0})
        month[row["direction"]] += int(row["huf_amount"])

    if monthly:
        first_month = date.fromisoformat(min(monthly) + "-01")
        last_month = date.fromisoformat(max(monthly) + "-01")
        current = first_month
        while current <= last_month:
            monthly.setdefault(current.strftime("%Y-%m"), {"bejövő": 0, "kimenő": 0})
            current = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)

    partners_list = sorted(partners.values(), key=lambda item: item["name"].casefold())
    return {
        "invoices": invoices,
        "errors": [row for row in rows if row.get("status") == "error"],
        "months": [{"month": key, **monthly[key]} for key in sorted(monthly)],
        "partners": partners_list,
    }


def _extract_pdf_text(content: bytes) -> str:
    try:
        import fitz
        document = fitz.open(stream=content, filetype="pdf")
        text = "\n".join(page.get_text("text") for page in document)
        document.close()
    except Exception as error:
        raise InvoiceError("A PDF nem olvasható vagy sérült.") from error
    if not text.strip():
        raise InvoiceError("A PDF-ben nem található kinyerhető szöveg.")
    return text


def process_pdf(content: bytes, filename: str, fingerprint: str, db_path: str = DB_PATH) -> str:
    if not content.startswith(b"%PDF-"):
        raise InvoiceError("A feltöltött fájl nem érvényes PDF.")
    text = _extract_pdf_text(content)
    invoice = validate_invoice(extract_invoice(text))
    rate, rate_date = get_exchange_rate(date.fromisoformat(invoice["service_date"]), invoice["currency"], db_path)
    huf_amount = convert_to_huf(invoice["gross_amount"], rate)
    with database(db_path) as connection:
        duplicate = connection.execute(
            "SELECT id FROM invoices WHERE issuer_key=? AND invoice_number=?",
            (invoice["issuer_key"], invoice["invoice_number"]),
        ).fetchone()
        if duplicate:
            return "duplicate"
        connection.execute(
            """
            INSERT INTO invoices (
                issuer_key, invoice_number, seller_name, seller_tax_id, buyer_name, buyer_tax_id,
                issue_date, service_date, due_date, currency, gross_amount, direction, partner_name,
                partner_tax_id, exchange_rate, exchange_rate_date, huf_amount, file_name, fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                invoice["issuer_key"], invoice["invoice_number"], invoice["seller_name"],
                invoice.get("seller_tax_id"), invoice["buyer_name"], invoice.get("buyer_tax_id"),
                invoice["issue_date"], invoice["service_date"], invoice["due_date"], invoice["currency"],
                str(invoice["gross_amount"]), invoice["direction"], invoice["partner_name"],
                invoice.get("partner_tax_id"), str(rate), rate_date.isoformat(), huf_amount, filename, fingerprint,
            ),
        )
    return "processed"


def load_dashboard_rows(db_path: str = DB_PATH) -> list[dict[str, Any]]:
    with database(db_path) as connection:
        invoices = [dict(row) | {"status": "processed"} for row in connection.execute("SELECT * FROM invoices")]
        jobs = [dict(row) for row in connection.execute(
            "SELECT fingerprint, file_name, status, error, updated_at FROM processing_jobs WHERE status='error'"
        )]
    return invoices + jobs


def mark_job(fingerprint: str, filename: str, status: str, error: str | None = None, db_path: str = DB_PATH) -> None:
    with database(db_path) as connection:
        connection.execute(
            """
            INSERT INTO processing_jobs(fingerprint, file_name, status, error, updated_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(fingerprint) DO UPDATE SET file_name=excluded.file_name, status=excluded.status,
                error=excluded.error, updated_at=CURRENT_TIMESTAMP
            """,
            (fingerprint, filename, status, error),
        )


@app.get("/")
def home() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    return build_dashboard(load_dashboard_rows())


@app.post("/api/upload")
def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    results = []
    for uploaded in files:
        filename = Path(uploaded.filename or "ismeretlen.pdf").name
        content = uploaded.file.read(MAX_PDF_BYTES + 1)
        fingerprint = hashlib.sha256(content).hexdigest()
        if len(content) > MAX_PDF_BYTES:
            message = "A fájl meghaladja a 25 MB-os méretkorlátot."
            mark_job(fingerprint, filename, "error", message)
            results.append({"file": filename, "status": "error", "message": message})
            continue
        with database() as connection:
            existing = connection.execute(
                "SELECT status FROM processing_jobs WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
        if existing and existing["status"] in ("processed", "duplicate"):
            results.append({"file": filename, "status": existing["status"], "message": "Már feldolgozva."})
            continue
        with database() as connection:
            saved_invoice = connection.execute(
                "SELECT id FROM invoices WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
        if saved_invoice:
            mark_job(fingerprint, filename, "processed")
            results.append({"file": filename, "status": "processed", "message": "Már feldolgozva."})
            continue
        try:
            status = process_pdf(content, filename, fingerprint)
            mark_job(fingerprint, filename, status)
            message = "Sikeresen feldolgozva." if status == "processed" else "Azonos kiállítóval és számlaszámmal már szerepel."
            results.append({"file": filename, "status": status, "message": message})
        except Exception as error:
            LOGGER.exception("Invoice processing failed for %s", filename)
            message = str(error) if isinstance(error, InvoiceError) else "Váratlan feldolgozási hiba történt."
            mark_job(fingerprint, filename, "error", message)
            results.append({"file": filename, "status": "error", "message": message})
    return {"results": results, "dashboard": build_dashboard(load_dashboard_rows())}


init_db()
