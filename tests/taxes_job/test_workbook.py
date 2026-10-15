# Standard library imports
from datetime import date
from decimal import Decimal

# Third party imports
import pytest

# First party imports
from scheduled_report_aggregator.jobs.taxes_job.states import mi
from scheduled_report_aggregator.jobs.taxes_job.workbook import compare_templates, load_template, write_rows

UA_SHEETS = ["Instructions", "T-101A", "T-101C", "T-103", "T-108A", "T-108B", "T-108C", "T-108D", "T-115A", "T-115B"]
SW_SHEETS = ["Instructions", "T-101B", "T-101C", "T-108C"]


def test_bundled_templates_have_the_states_sheets() -> None:
  assert load_template(mi.UNCLASSIFIED_ACQUIRER).sheetnames == UA_SHEETS
  assert load_template(mi.SECONDARY_WHOLESALER).sheetnames == SW_SHEETS


@pytest.mark.parametrize("sheet", ["T-101A", "T-103", "T-108B"])
def test_every_schedule_header_matches_the_bundled_template(sheet: str) -> None:
  ws = load_template(mi.UNCLASSIFIED_ACQUIRER)[sheet]
  assert tuple(str(ws.cell(5, c).value).strip() for c in range(1, 15)) == mi.header_for(sheet)


def test_rows_start_at_row_7_contiguously() -> None:
  ws = load_template(mi.SECONDARY_WHOLESALER)["T-108C"]
  rows = [
    ("OTP", "", "", date(2026, 6, 1), date(2026, 6, 1), "1", "USA", "1 Main", "Gaylord", "MI", "49735", Decimal("1.50"), 0, None),
    ("PC1", "", "", date(2026, 6, 2), date(2026, 6, 2), "2", "USA", "1 Main", "Gaylord", "MI", "49735", Decimal("9.99"), 5, None),
  ]

  assert write_rows(ws, mi.header_for("T-108C"), rows, mi.NUMBER_FORMATS) == len(rows)
  assert ws["F7"].value == "1"
  assert ws["F8"].value == "2"
  assert ws["L8"].value == Decimal("9.99")
  assert ws["D7"].number_format == "mm/dd/yyyy"
  assert ws["K7"].number_format == "@"
  assert ws["A9"].value is None


def test_changed_header_fails_loudly() -> None:
  ws = load_template(mi.SECONDARY_WHOLESALER)["T-108C"]
  ws["C5"] = "FEIN"

  with pytest.raises(ValueError, match="layout has changed"):
    write_rows(ws, mi.header_for("T-108C"), [], mi.NUMBER_FORMATS)


def test_identical_templates_report_nothing() -> None:
  spec = mi.UNCLASSIFIED_ACQUIRER
  assert compare_templates(spec, load_template(spec), load_template(spec)) == []


def test_template_changes_are_reported_but_instructions_are_ignored() -> None:
  spec = mi.UNCLASSIFIED_ACQUIRER
  live = load_template(spec)
  live["T-103"]["L5"] = "Wholesale Price"
  live["Instructions"]["A1"] = "reworded"
  live.create_sheet("T-120")

  warnings = compare_templates(spec, load_template(spec), live)

  added, changed = warnings
  assert "adds sheet 'T-120'" in added
  assert "T-103" in changed
  assert "L5" in changed
