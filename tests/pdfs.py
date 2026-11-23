import pymupdf

_FONT = None


def make_pdf(*pages: str) -> bytes:
    """Szöveges PDF generálása; a Droid Sans Fallback font bármilyen írásrendszert tud."""
    global _FONT
    _FONT = _FONT or pymupdf.Font("cjk")
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        writer = pymupdf.TextWriter(page.rect)
        for index, line in enumerate(text.split("\n")):
            writer.append((72, 72 + index * 18), line, font=_FONT, fontsize=12)
        writer.write_text(page)
    data = document.tobytes()
    document.close()
    return data


def invoice_text(number: str = "NW-2026-0001", gross: str = "1 270,00") -> str:
    """Kinyerhető számlaszöveg (kimenő számla a Nordwind Solutions Kft.-től)."""
    return (
        "Nordwind Solutions Kft.\nInfopark sétány 7.\n1117 Budapest\nAdószám: 26481937-2-43\n"
        "Vevő\nBalaton Tech Kft.\nFő utca 1.\n8200 Veszprém\nAdószám: 25573019-2-19\n"
        f"Számlaszám: {number}\nKiállítás dátuma: 2026.01.12.\nTeljesítés dátuma: 2026.01.11.\n"
        "Fizetési határidő: 2026.02.11.\nNettó összesen 1 000,00 Ft\nÁFA összesen (27%) 270,00 Ft\n"
        f"Fizetendő összesen {gross} Ft"
    )


def make_blank_pdf() -> bytes:
    document = pymupdf.open()
    document.new_page()
    data = document.tobytes()
    document.close()
    return data


def make_encrypted_pdf(text: str = "titkos") -> bytes:
    document = pymupdf.open(stream=make_pdf(text), filetype="pdf")
    data = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    document.close()
    return data
