"""Score a generated return workbook against the one actually filed, for iterating on the schedule rules.

Output is counts and cell references only, never cell values: filed returns other than June 2026
are private, and the score has to be safe to print for any month. Rows pair up by invoice number
and brand code; columns compare after normalising what Excel and the CPA vary (dates typed as text,
floats like 0.30000000000000004, ZIP codes as numbers).
"""

# Standard library imports
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING

# Third party imports
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterable
  from pathlib import Path

  # Third party imports
  from openpyxl.worksheet.worksheet import Worksheet


__all__ = ["score_workbook"]

CENT = Decimal("0.01")
REFS_SHOWN = 8
WIDTH = 14  # columns A..N


def _date(v: object) -> object:
  if isinstance(v, datetime):
    return v.date()
  if isinstance(v, str):
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
      try:
        return datetime.strptime(v.strip(), fmt).date()  # noqa: DTZ007 - calendar dates, no zone
      except ValueError:
        pass
  return v


def _money(v: object) -> object:
  try:
    return None if v in (None, "") else Decimal(str(v)).quantize(CENT, ROUND_HALF_UP)
  except InvalidOperation:
    return str(v)


def _text(v: object) -> str:
  return "" if v is None else str(v).strip().upper()


NORMALISE: dict[str, Callable[[object], object]] = {
  "A": lambda v: _text(v)[:3],
  "D": _date,
  "E": _date,
  "L": _money,
  "M": lambda v: int(v) if isinstance(v, int | float) else 0,
  "N": _money,
}


def _rows(ws: Worksheet) -> dict[tuple[str, str, int], tuple[int, list[object]]]:
  """The sheet's schedule rows keyed by (invoice, brand code, occurrence), with their sheet row numbers."""
  header = next((r for r in range(1, ws.max_row + 1) if _text(ws.cell(r, 1).value) == "BRAND CODE"), None)
  if header is None:
    return {}
  seen: Counter[tuple[str, str]] = Counter()
  keyed = {}
  for number, row in enumerate(ws.iter_rows(min_row=header + 2, max_col=WIDTH, values_only=True), start=header + 2):
    if all(v in (None, "") for v in row):
      continue
    values = [NORMALISE.get(get_column_letter(c), _text)(v) for c, v in enumerate(row, start=1)]
    key = (str(values[5]), str(values[0]))
    keyed[(*key, seen[key])] = (number, values)
    seen[key] += 1
  return keyed


def score_workbook(generated: Path, filed: Path, skip_columns: Iterable[str] = ("B", "C")) -> list[str]:
  """Per-sheet match counts of *generated* against *filed*; mismatches are listed as cells of the filed sheet."""
  ours, theirs = load_workbook(generated), load_workbook(filed, data_only=True)  # the CPA types formulas such as =+D7
  skip = set(skip_columns)
  lines = [f"{filed.name} (columns {', '.join(sorted(skip)) or 'none'} not scored)"]
  for sheet in theirs.sheetnames:
    if sheet not in ours.sheetnames or sheet == "Instructions":
      continue
    gen, fil = _rows(ours[sheet]), _rows(theirs[sheet])
    if not gen and not fil:
      continue
    both = gen.keys() & fil.keys()
    lines.append(
      f"{sheet}: generated {len(gen)} rows, filed {len(fil)} | matched {len(both)}, "
      f"only generated {len(gen.keys() - both)}, only filed {len(fil.keys() - both)}"
    )
    if only_filed := sorted(fil[k][0] for k in fil.keys() - both):
      lines.append(
        f"  filed rows with no generated match: {', '.join(map(str, only_filed[:REFS_SHOWN]))}{' ...' if len(only_filed) > REFS_SHOWN else ''}"
      )
    for c in range(WIDTH):
      letter = get_column_letter(c + 1)
      if letter in skip or not both:
        continue
      bad = sorted(fil[k][0] for k in both if gen[k][1][c] != fil[k][1][c])
      refs = ", ".join(f"{letter}{n}" for n in bad[:REFS_SHOWN]) + (" ..." if len(bad) > REFS_SHOWN else "")
      lines.append(f"  {letter}: {len(both) - len(bad)}/{len(both)}" + (f"  ({refs})" if bad else ""))
  return lines
