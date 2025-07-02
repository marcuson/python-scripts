import pathlib
from typing import Literal

import pdfplumber

from .date import date_to_lex, is_date
from .fatture_in_cloud import get_due_date
from .airbnb import get_report_date


def _parse_beni_consumo_upper_right(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = width / 5 * 4
    top = height / 15
    x1 = width
    bottom = top + height / 12

    region = (x0, top, x1, bottom)
    text = page.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split


def eramo_fattura_avviso(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    date_ita = get_due_date(path)
    date_lex = date_to_lex(date_ita) if date_ita is not None else "NO_DATE"
    return {"date_lex": date_lex}


def eramo_report(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    date_ita = get_report_date(path)
    date_lex = (
        date_to_lex(date_ita, in_format="full_human")
        if date_ita is not None
        else "NO_DATE"
    )
    return {"date_lex": date_lex}


def eramo_beni_consumo(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        texts = _parse_beni_consumo_upper_right(pdf)
        print(texts)
        date_ita = next((s for s in texts if is_date(s)), None)
        date_lex = date_to_lex(date_ita) if date_ita is not None else "NO_DATE"
        return {"date_lex": date_lex}
