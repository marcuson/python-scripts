import pathlib
from typing import Literal

import pdfplumber

from .date import date_to_lex, is_date
from .pdf import extract_texts


def mps_leo(path: pathlib.Path, date_ita: str) -> dict[str, str] | Literal[False]:
    date_lex = (
        date_to_lex(date_ita, in_format="%d_%m_%Y")
        if date_ita is not None
        else "NO_DATE"
    )

    return {"date_lex": date_lex}


def _mps_cicci_parse_movimenti_cc(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = width / 2
    top = 0
    x1 = width / 2 + width / 10
    bottom = height / 10

    return extract_texts(page, x0, top, x1, bottom)


def _mps_cicci_parse_estratto_conto(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = width / 2
    top = 0
    x1 = width / 2 + width / 4
    bottom = height / 8

    return extract_texts(page, x0, top, x1, bottom)


def _mps_cicci_parse_doc_sintesi(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = width / 2
    top = 0
    x1 = width / 2 + width / 3
    bottom = height / 10

    return extract_texts(page, x0, top, x1, bottom)


def _mps_cicci_parse_movimenti_comunicazione(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = width / 2
    top = 0
    x1 = width / 2 + width / 3
    bottom = height / 10

    return extract_texts(page, x0, top, x1, bottom)


def mps_cicci_movimenti_cc(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        texts = _mps_cicci_parse_movimenti_cc(pdf)

        date_ita = next((s for s in texts if is_date(s)), None)
        date_lex = date_to_lex(date_ita) if date_ita is not None else "NO_DATE"

        return {"date_lex": date_lex}


def mps_cicci_estratto_conto(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        texts = _mps_cicci_parse_estratto_conto(pdf)

        date_ita = next((s for s in texts if is_date(s)), None)
        date_lex = date_to_lex(date_ita) if date_ita is not None else "NO_DATE"

        return {"date_lex": date_lex}


def mps_cicci_doc_sintesi(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        texts = _mps_cicci_parse_doc_sintesi(pdf)

        date_ita_month_index = next(
            (i for i, s in enumerate(texts) if is_date(s, format="%B")), -1
        )
        if date_ita_month_index < 0:
            return False

        date_ita = " ".join(texts[date_ita_month_index - 1 : date_ita_month_index + 2])
        date_lex = (
            date_to_lex(date_ita, in_format="full_human")
            if date_ita is not None
            else "NO_DATE"
        )

        return {"date_lex": date_lex}


def mps_cicci_comunicazione(path: pathlib.Path) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        texts = _mps_cicci_parse_movimenti_comunicazione(pdf)

        date_ita = next((s for s in texts if is_date(s)), None)
        date_lex = date_to_lex(date_ita) if date_ita is not None else "NO_DATE"

        return {"date_lex": date_lex}
