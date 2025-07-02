import pathlib
from typing import Literal

from .utils import Location
from .date import date_to_lex


def tim(
    path: pathlib.Path, location: Location, place: str, due_date_ita: str
) -> dict[str, str] | Literal[False]:

    parsed_loc = "NO_SUFFIX"
    match place:
        case "BRASCHI":
            parsed_loc = "afrbr148"
        case "POLIZIANO":
            parsed_loc = "monpol16"

    if parsed_loc != location:
        return False

    due_date_lex = (
        date_to_lex(due_date_ita) if due_date_ita is not None else "NO_DUE_DATE"
    )

    return {"due_date_lex": due_date_lex}
