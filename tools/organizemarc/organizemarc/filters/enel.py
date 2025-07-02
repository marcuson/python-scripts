import pathlib
from typing import Literal
import pdfplumber

from .utils import Location
from .date import date_to_lex, is_date


def _enel_parse_upper_right(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    x0 = width / 2
    top = 0
    x1 = width
    bottom = height / 5

    region = (x0, top, x1, bottom)
    text = page.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split


def _enel_parse_center_left(pdf: pdfplumber.PDF) -> list[str]:
    page = pdf.pages[0]

    width = page.width
    height = page.height

    h = height / 4
    x0 = 0
    top = h
    x1 = width / 2
    bottom = top + h

    region = (x0, top, x1, bottom)
    text = page.within_bbox(region).extract_text()
    text_lower = text.lower()
    text_split = text_lower.split()
    return text_split


def enel_gas(
    path: pathlib.Path, location: str | Location
) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        upper_right_texts = _enel_parse_upper_right(pdf)

        gas_index = next((i for i, s in enumerate(upper_right_texts) if s == "gas"), -1)
        if gas_index < 0:
            return False

        pdr = upper_right_texts[gas_index - 1]
        parsed_loc = "NA"
        match pdr:
            case "16130000416735":
                parsed_loc = Location.Afrbr148
            case "07860000008621":
                parsed_loc = Location.Monpol16

        if parsed_loc != location:
            return False

        center_left_texts = _enel_parse_center_left(pdf)

        due_date_ita = next((s for s in center_left_texts if is_date(s)), None)
        due_date_lex = (
            date_to_lex(due_date_ita) if due_date_ita is not None else "NO_DUE_DATE"
        )

        return {"pdr": pdr, "due_date_lex": due_date_lex}


def enel_luce(
    path: pathlib.Path, location: str | Location
) -> dict[str, str] | Literal[False]:
    with pdfplumber.open(path) as pdf:
        upper_right_texts = _enel_parse_upper_right(pdf)

        energia_index = next(
            (i for i, s in enumerate(upper_right_texts) if s == "energia"), -1
        )
        if energia_index < 0:
            return False

        pod = upper_right_texts[energia_index - 1]
        parsed_loc = "NA"
        match pod:
            case "it001e46933468":
                parsed_loc = Location.Afrbr148
            case "it001e46935791":
                parsed_loc = Location.Monpol16

        if parsed_loc != location:
            return False

        center_left_texts = _enel_parse_center_left(pdf)

        due_date_ita = next((s for s in center_left_texts if is_date(s)), None)
        due_date_lex = (
            date_to_lex(due_date_ita) if due_date_ita is not None else "NO_DUE_DATE"
        )

        return {"pdr": pod, "due_date_lex": due_date_lex}


def enel_x(path: pathlib.Path, due_date_ita: str) -> dict[str, str] | Literal[False]:
    due_date_lex = (
        date_to_lex("01" + due_date_ita, in_format="%d%m%y")
        if due_date_ita is not None
        else "NO_DUE_DATE"
    )

    return {"due_date_lex": due_date_lex}
