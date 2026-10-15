# Standard library imports
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

# First party imports
from scheduled_report_aggregator.jobs.taxes_job.scoring import score_workbook
from scheduled_report_aggregator.jobs.taxes_job.states import mi
from scheduled_report_aggregator.jobs.taxes_job.workbook import load_template, write_rows

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence
  from pathlib import Path

  # First party imports
  from scheduled_report_aggregator.jobs.taxes_job.workbook import CellValue

ROW: list[CellValue] = [mi.BRAND_LABELS["OTP"], "", "", date(2026, 6, 1), date(2026, 6, 1), "10001", "USA", "1 Main", "Gaylord", "MI", "49735", Decimal("123.46"), 0, None]  # fmt: skip


def save(path: Path, *rows: Sequence[CellValue]) -> Path:
  wb = load_template(mi.SECONDARY_WHOLESALER)
  write_rows(wb["T-108C"], mi.header_for("T-108C"), [tuple(r) for r in rows], mi.NUMBER_FORMATS)
  wb.save(path)
  return path


def test_identical_workbooks_score_fully(tmp_path: Path) -> None:
  lines = score_workbook(save(tmp_path / "gen.xlsx", ROW), save(tmp_path / "filed.xlsx", ROW))

  assert "T-108C: generated 1 rows, filed 1 | matched 1, only generated 0, only filed 0" in lines
  assert "  L: 1/1" in lines


def test_excel_artifacts_are_normalised(tmp_path: Path) -> None:
  # Text dates, float pennies and numeric ZIPs are how the CPA's copies arrive.
  filed = [*ROW]
  filed[3], filed[11], filed[10] = "6/1/2026", 123.46000000000001, 49735
  filed[4] = datetime(2026, 6, 1)  # noqa: DTZ001 - Excel cells hold naive datetimes

  lines = score_workbook(save(tmp_path / "gen.xlsx", ROW), save(tmp_path / "filed.xlsx", filed))

  assert all(line.endswith(" 1/1") for line in lines if line.startswith("  "))


def test_mismatches_name_cells_never_values(tmp_path: Path) -> None:
  filed = [*ROW]
  filed[11] = Decimal("999.99")
  extra = [*ROW]
  extra[5] = "10002"

  lines = score_workbook(save(tmp_path / "gen.xlsx", ROW), save(tmp_path / "filed.xlsx", filed, extra))

  assert "  L: 0/1  (L7)" in lines
  assert "  filed rows with no generated match: 8" in lines
  assert not any("999" in line or "123" in line for line in lines)
