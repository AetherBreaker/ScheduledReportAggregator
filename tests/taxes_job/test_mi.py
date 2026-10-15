# Standard library imports
from datetime import date, datetime
from decimal import Decimal

# Third party imports
import numpy as np
import pandas as pd

# First party imports
from scheduled_report_aggregator.jobs.taxes_job.states import mi

INVOICED = datetime(2026, 6, 1, 10, 30)  # noqa: DTZ001 - pyodbc hands back naive datetimes
MONTH_PARAMETERS = 2  # start and end


def result(**overrides: object) -> pd.DataFrame:
  row = {
    "invoice": 10001,
    "date": INVOICED,
    "invoice_date": INVOICED,
    "account": "SFT900",
    "address": " 100 Main St. ",
    "city": "Gaylord",
    "state": "mi",
    "zip": "49000",
    "brand": "OTP",
    "price": Decimal("123.455"),
    "cigars": 0,
    "ounce_lines": 2,
    "ounce_lines_unweighed": 0,
    "ounces": Decimal("12.5"),
  }
  return pd.DataFrame([row | overrides])


def test_store_rows_are_cleaned_rounded_half_up_and_left_unnamed() -> None:
  warnings: list[str] = []
  rows = mi.rows_from(result(), "SW T-108C", warnings)

  row = rows.iloc[0]
  assert row["brand"] == f"OTP {chr(0x2013)} Other Tobacco Products"  # the state's en dash
  assert row["name"] == ""
  assert row["invoice"] == "10001"
  assert row["date"] == date(2026, 6, 1)
  assert row["address"] == "100 Main St."
  assert row["state"] == "MI"
  assert row["price"] == Decimal("123.46")
  assert row["ounces"] == Decimal("12.50")
  assert any("SFT900" in w for w in warnings)


def test_ounces_blank_when_any_ounce_item_lacks_a_weight() -> None:
  warnings: list[str] = []
  rows = mi.rows_from(result(ounce_lines_unweighed=1), "SW T-108C", warnings)

  assert rows.iloc[0]["ounces"] is None
  assert any("Total Ounces left blank on 1 rows" in w for w in warnings)


def test_ounces_blank_on_rows_without_ounce_items() -> None:
  rows = mi.rows_from(result(brand="PC1", cigars=Decimal("40.4"), ounce_lines=0, ounces=0), "SW T-108C", [])

  assert rows.iloc[0]["ounces"] is None
  assert rows.iloc[0]["cigars"] == int(Decimal(40))  # whole cigars


def test_missing_mandatory_vendor_address_is_noted() -> None:
  warnings: list[str] = []
  mi.rows_from(
    result(name="ALTADIS USA", address=None, city=None, state=None, zip=None).drop(columns="account"), "UA T-101A", warnings
  )

  assert any("vendor FEINs are blank" in w for w in warnings)
  assert any("rows have no zip" in w for w in warnings)


def test_month_end_rows_total_each_brand_between_our_own_licenses() -> None:
  rows = pd.concat(
    [
      mi.rows_from(result(price=Decimal("10.00")), "x", []),
      mi.rows_from(result(invoice=2, price=Decimal("5.25")), "x", []),
      mi.rows_from(result(invoice=3, brand="PC1", price=Decimal("100.00"), cigars=20, ounce_lines=0), "x", []),
    ]
  )

  moved = mi.month_end_rows(rows, date(2026, 6, 30))

  assert list(moved["invoice"]) == ["06/30/26OTP", "06/30/26PC1"]
  assert list(moved["price"]) == [Decimal("15.25"), Decimal("100.00")]
  assert list(moved["cigars"]) == [0, 20]
  assert set(moved["fein"]) == {mi.COMPANY.fein}
  assert set(moved["zip"]) == {"49735"}


def test_cells_are_plain_python_values() -> None:
  rows = mi.rows_from(result(), "x", [])
  rows["cigars"] = rows["cigars"].astype(np.int64)

  (cells,) = mi.as_cells(rows)

  assert type(cells[12]) is int
  assert cells[13] == Decimal("12.50")
  assert len(cells) == len(mi.COLUMNS)


def test_queries_take_only_the_month_as_parameters() -> None:
  sales = mi.sales_query(mi.SHIPPED_MI, ~mi.RETURN_LINE).get_sql()
  buys = mi.purchases_query(tax_paid=True).get_sql()

  assert sales.count("?") == MONTH_PARAMETERS
  assert buys.count("?") == MONTH_PARAMETERS
  assert "GROUP BY" in sales
  assert "CASE WHEN" in sales.split("GROUP BY")[1]  # SQL Server can't group by a select alias
  assert '"DefaultTaxType"=1' in buys
  assert '"DefaultTaxType",0)<>1' in mi.purchases_query(tax_paid=False).get_sql()
