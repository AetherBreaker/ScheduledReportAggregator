"""Michigan's monthly OTP returns (Unclassified Acquirer and Secondary Wholesaler), built from the warehouse database.

Each schedule is one pypika query over the month's lines, grouped to one row per invoice and brand
code; `print()` a query to get T-SQL that runs as-is in SSMS (bind the two `?` to the month's start
and end). Rules come from Michigan's schedule definitions
(https://www.michigan.gov/taxes/business-taxes/tobacco/otp-schedules) and were fitted to the June 2026
filing; RULES_TO_CONFIRM lists the ones June could not settle.

The server is SQL Server 2017 at compatibility level 100 (SQL 2008 syntax), so the queries stay to
joins, CASE, COALESCE and GROUP BY.
"""

# Standard library imports
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

# Third party imports
import pandas as pd
from pypika import Case, MSSQLQuery, Parameter, Tables, functions as fn
from pypika.terms import Criterion

# First party imports
from scheduled_report_aggregator.jobs.taxes_job.workbook import TemplateSpec

if TYPE_CHECKING:
  # Third party imports
  import pyodbc
  from pypika.queries import QueryBuilder
  from pypika.terms import Term

  # First party imports
  from scheduled_report_aggregator.jobs.taxes_job.workbook import CellValue


__all__ = ["RETURNS", "SECONDARY_WHOLESALER", "UNCLASSIFIED_ACQUIRER", "MichiganReturns", "build_returns"]

# ItemInformation.Category1 values reported to Michigan. Anything else is deliberately not reported;
# verifying item categories is out of this job's scope (owner, 2026-10-15).
MI_CATEGORIES = (
  "21+ Cigarillos Budget",
  "21+ Little Cigars",
  "21+ Wraps/Papers",
  "Bag Tobacco",
  "Premium Cigar Budget",
  "Premium Point Cigars",
  "PremiumCigars",
  "T21OralTobacco",
)
# ProductLines.ProductLineID values, as stored (varchar, unpadded).
CIGAR_LINES = ("21", "22", "23", "27", "28")  # premium point, premium, premium budget, little cigars, cigarillos
OUNCE_LINES = ("18", "19", "20", "24", "31")  # ItemWeight is ounces here; vapor lines keep millilitres in the same column
TAX_PAID_TYPE = 1  # ProductLines.DefaultTaxType "None": Michigan tax was paid before the product reached us

# Michigan taxes OTP at 32% of the wholesale price and caps cigars at $0.50 each. A cigar is reported
# under PC1 exactly when the cap applies (32% of its per-cigar price reaches $0.50), whatever tax
# plan its order line carries: June's filing classed tax-paid cigarillos that way too.
RATE, CAP = Decimal("0.32"), Decimal("0.50")
CENT = Decimal("0.01")

BRAND_LABELS = {"OTP": "OTP – Other Tobacco Products", "PC1": "PC1 – Premium Cigar"}  # noqa: RUF001 - the state's labels use an en dash
MAX_NAME = 35  # template: "Maximum of 35 characters."


@dataclass(frozen=True)
class Party:
  """A buyer or seller as a schedule row names it."""

  name: str
  fein: str
  address: str
  city: str
  state: str
  zip: str


# The filer, as the month-end rows between its two Michigan licenses name it.
COMPANY = Party("Sweet Fire Tobacco Wholesale, Inc", "84-2018191", "810 S Otsego Ave, Suite 102", "Gaylord", "MI", "49735")

_TEMPLATES = "https://www.michigan.gov/taxes/-/media/Project/Websites/taxes/Tobacco"
UNCLASSIFIED_ACQUIRER = TemplateSpec(
  "mi", "Michigan_UnclassifiedAcquirer_Tobacco.xlsx", f"{_TEMPLATES}/Michigan_UnclassifiedAcquirer_Tobacco.xlsx", 6, ("Instructions",)
)
SECONDARY_WHOLESALER = TemplateSpec(
  "mi", "Michigan_SecondaryWholesaler_Tobacco.xlsx", f"{_TEMPLATES}/Michigan_SecondaryWholesaler_Tobacco.xlsx", 6, ("Instructions",)
)
RETURNS = {"UnclassifiedAcquirer": UNCLASSIFIED_ACQUIRER, "SecondaryWholesaler": SECONDARY_WHOLESALER}

# Row 5 of every schedule sheet; column D reads "Received" on receipt schedules and "Sold" on the rest.
HEADER = (
  "Brand Code", "Name", "FEIN / ME", "{date}", "Date", "Number", "Country Code",
  "Address", "City", "State", "Zip Code", "Price", "Premium Cigars", "Total Ounces",
)  # fmt: skip
RECEIPT_SHEETS = frozenset({"T-101A", "T-101B", "T-101C"})
COLUMNS = (
  "brand",
  "name",
  "fein",
  "date",
  "invoice_date",
  "invoice",
  "country",
  "address",
  "city",
  "state",
  "zip",
  "price",
  "cigars",
  "ounces",
)
NUMBER_FORMATS = (None, None, "@", "mm/dd/yyyy", "mm/dd/yyyy", "@", None, None, None, None, "@", "0.00", "0", "0.00")
MANDATORY = ("brand", "country", "address", "city", "state", "zip")  # marked "Mandatory" in each sheet's guidance row

RULES_TO_CONFIRM = (
  (
    "The month-end lines between our two licenses (Unclassified Acquirer T-108C and Secondary Wholesaler T-101B) "
    "carry the Secondary Wholesaler's T-108C totals per brand code. June's filing used figures that no warehouse "
    "price reproduces; ask the CPA where they come from."
  ),
  (
    "Purchase schedules (T-101A, the vendor rows of T-101B) are an untested draft: until a full month is entered "
    "with one warehouse purchase per vendor invoice (new-item invoices were still being split), no month can "
    "verify them. Dates use the warehouse's received date; the vendor's own invoice date is not stored."
  ),
  (
    "Return lines (OrderDetails.ReturnLine) go to Secondary Wholesaler T-101C as positive amounts and stay off "
    "the sales schedules; June had none to check against."
  ),
)


def header_for(sheet: str) -> tuple[str, ...]:
  """The exact row-5 header text of *sheet*."""
  return tuple(h.format(date="Received" if sheet in RECEIPT_SHEETS else "Sold") for h in HEADER)


# ---- the queries --------------------------------------------------------------------------------

OD, OH, C, II, PL, PD, PH, V, VP = Tables(
  "OrderDetails", "OrderHeader", "Customers", "iteminformation", "ProductLines",
  "PurchasesDetails", "PurchasesHeader", "Vendors", "VendorParts",
)  # fmt: skip

OUNCE_LINE = PL.ProductLineID.isin(OUNCE_LINES)
NO_WEIGHT = II.ItemWeight.isnull() | (II.ItemWeight <= 0)  # noqa: PD003 - pypika, not pandas

SALE_PRICE = OD.ItemCost * OD.InvoiceQty
SALE_PC1 = PL.ProductLineID.isin(CIGAR_LINES) & (II.CigCountNumUnits > 0) & (OD.ItemCost * RATE >= II.CigCountNumUnits * CAP)
SALE_BRAND = Case().when(SALE_PC1, "PC1").else_("OTP")
# Where the cap was charged, the tax shows the true count (the catalogue holds 20 for a box of 23).
SALE_CIGARS = (
  Case()
  .when(SALE_PC1 & (OD.TaxUsedMinOrMax == 1), OD.ItemTax * OD.InvoiceQty / CAP)
  .when(SALE_PC1, II.CigCountNumUnits * OD.InvoiceQty)
  .else_(0)
)
SALE_OUNCES = II.ItemWeight * OD.InvoiceQty

PART_UNITS = fn.Coalesce(VP.NumUnits, 1)  # items per vendor part
BUY_PRICE = PD.CostEa * PD.ReceivedQty
BUY_PC1 = PL.ProductLineID.isin(CIGAR_LINES) & (II.CigCountNumUnits > 0) & (PD.CostEa * RATE >= II.CigCountNumUnits * PART_UNITS * CAP)
BUY_BRAND = Case().when(BUY_PC1, "PC1").else_("OTP")
BUY_CIGARS = Case().when(BUY_PC1, II.CigCountNumUnits * PART_UNITS * PD.ReceivedQty).else_(0)
BUY_OUNCES = II.ItemWeight * PART_UNITS * PD.ReceivedQty


def _ounce_columns(ounces: Term) -> list[Term]:
  """Per-row ounce aggregates; `rows_from` turns them into Total Ounces or a blank."""
  return [
    fn.Sum(Case().when(OUNCE_LINE, 1).else_(0)).as_("ounce_lines"),
    fn.Sum(Case().when(OUNCE_LINE & NO_WEIGHT, 1).else_(0)).as_("ounce_lines_unweighed"),
    fn.Sum(Case().when(OUNCE_LINE & ~NO_WEIGHT, ounces).else_(0)).as_("ounces"),
  ]


def sales_query(*criteria: Criterion) -> QueryBuilder:
  """One row per invoice and brand code over the month's invoiced MI-category lines that meet *criteria*."""
  return (
    MSSQLQuery.from_(OD)
    .join(OH).on(OD.OrderID == OH.Row_ID)
    .join(C).on(OH.CustomerID == C.Row_ID)
    .join(II).on(OD.ItemRowID == II.Row_ID)
    .left_join(PL).on(II.ProductLine == PL.Row_ID)
    .where(OH.OrderStatus == "Invoiced")
    .where(OH.InvoicedDate >= Parameter("?"))
    .where(OH.InvoicedDate < Parameter("?"))
    .where(II.Category1.isin(MI_CATEGORIES))
    .where(SALE_PRICE != 0)
    .where(Criterion.all(criteria))
    .groupby(OH.Row_ID, OH.InvoicedDate, C.AccountNumber, OH.ShipToAddress1, OH.ShipToCity, OH.ShipToState, OH.ShipToZip, SALE_BRAND)
    .select(
      OH.Row_ID.as_("invoice"),
      OH.InvoicedDate.as_("date"),
      OH.InvoicedDate.as_("invoice_date"),
      C.AccountNumber.as_("account"),
      OH.ShipToAddress1.as_("address"),
      OH.ShipToCity.as_("city"),
      OH.ShipToState.as_("state"),
      OH.ShipToZip.as_("zip"),
      SALE_BRAND.as_("brand"),
      fn.Sum(SALE_PRICE).as_("price"),
      fn.Sum(SALE_CIGARS).as_("cigars"),
      *_ounce_columns(SALE_OUNCES),
    )
    .orderby(OH.InvoicedDate, OH.Row_ID, SALE_BRAND)
  )  # fmt: skip


def purchases_query(*, tax_paid: bool) -> QueryBuilder:
  """One row per vendor invoice and brand code over the MI-category purchase lines received in the month."""
  paid = PL.DefaultTaxType == TAX_PAID_TYPE
  return (
    MSSQLQuery.from_(PD)
    .join(PH).on(PD.HeaderRowID == PH.Row_ID)
    .join(V).on(PH.VendorID == V.Row_ID)
    .join(VP).on(PD.VendorPartRowID == VP.Row_ID)
    .join(II).on(VP.ItemRowID == II.Row_ID)
    .left_join(PL).on(II.ProductLine == PL.Row_ID)
    .where(PD.ReceivedDate >= Parameter("?"))
    .where(PD.ReceivedDate < Parameter("?"))
    .where(PD.ReceivedQty > 0)
    .where(II.Category1.isin(MI_CATEGORIES))
    .where(paid if tax_paid else (fn.Coalesce(PL.DefaultTaxType, 0) != TAX_PAID_TYPE))
    .groupby(PH.InvoiceNumber, PH.VendorID, V.VendorName, V.Address1, V.City, V.State, V.Zip, BUY_BRAND)
    .select(
      PH.InvoiceNumber.as_("invoice"),
      fn.Min(PD.ReceivedDate).as_("date"),
      fn.Min(PH.InvoiceDate).as_("invoice_date"),
      V.VendorName.as_("name"),
      V.Address1.as_("address"),
      V.City.as_("city"),
      V.State.as_("state"),
      V.Zip.as_("zip"),
      BUY_BRAND.as_("brand"),
      fn.Sum(BUY_PRICE).as_("price"),
      fn.Sum(BUY_CIGARS).as_("cigars"),
      *_ounce_columns(BUY_OUNCES),
    )
    .orderby(fn.Min(PD.ReceivedDate), PH.InvoiceNumber, BUY_BRAND)
  )  # fmt: skip


SHIPPED_MI = OH.ShipToState == "MI"
RETURN_LINE = fn.Coalesce(OD.ReturnLine, 0) == 1


# ---- from query results to schedule rows ----------------------------------------------------------


@dataclass
class MichiganReturns:
  """Both returns' schedule rows (frames with COLUMNS, keyed by return then sheet) and the reviewer's notes."""

  sheets: dict[str, dict[str, pd.DataFrame]]
  warnings: list[str] = field(default_factory=list)


def _fetch(cursor: pyodbc.Cursor, query: QueryBuilder, start: date, end: date) -> pd.DataFrame:
  """Run *query* for [start, end) and keep pyodbc's Decimals (`from_records` doesn't coerce to float)."""
  cursor.execute(query.get_sql(), start, end)
  return pd.DataFrame.from_records([tuple(r) for r in cursor.fetchall()], columns=[d[0] for d in cursor.description])


def _cents(value: object) -> Decimal:
  return Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)


def rows_from(result: pd.DataFrame, sheet: str, warnings: list[str], party: Party | None = None) -> pd.DataFrame:
  """Turn one schedule query's result into template rows (COLUMNS), noting what the reviewer must fill in.

  *party*, when given, names every row (the month-end rows); otherwise vendor rows carry the vendor's
  name and stores' rows stay unnamed until the FasTrax location export supplies legal names and FEINs.
  """
  if result.empty:
    return pd.DataFrame(columns=list(COLUMNS))
  rows = pd.DataFrame(
    {
      "brand": result["brand"].map(BRAND_LABELS),
      "name": party.name if party else result.get("name", ""),
      "fein": party.fein if party else "",
      "date": result["date"].map(lambda d: d.date() if isinstance(d, datetime) else d),
      "invoice_date": result["invoice_date"].map(lambda d: d.date() if isinstance(d, datetime) else d),
      "invoice": result["invoice"].astype(str).str.strip(),
      "country": "USA",
      "address": party.address if party else result["address"].fillna("").astype(str).str.strip(),
      "city": party.city if party else result["city"].fillna("").astype(str).str.strip(),
      "state": party.state if party else result["state"].fillna("").astype(str).str.strip().str.upper(),
      "zip": party.zip if party else result["zip"].fillna("").astype(str).str.strip(),
      "price": result["price"].map(_cents),
      "cigars": result["cigars"].map(lambda n: int(Decimal(str(n)).to_integral_value(ROUND_HALF_UP))),
      "ounces": [
        _cents(oz) if lines > 0 and unweighed == 0 else None
        for lines, unweighed, oz in zip(result["ounce_lines"], result["ounce_lines_unweighed"], result["ounces"], strict=True)
      ],
    }
  )

  if party is None and "account" in result:
    accounts = sorted(set(result["account"].astype(str)))
    warnings.append(
      f"{sheet}: store legal names and FEINs are blank on {len(rows)} rows until the FasTrax location export is wired in ({', '.join(accounts)})."
    )
  elif party is None:
    warnings.append(
      f"{sheet}: vendor FEINs are blank on {len(rows)} rows (no source yet); vendor names are the warehouse's, not legal names."
    )
  if (unweighed := int((result["ounce_lines_unweighed"] > 0).sum())) > 0:
    warnings.append(f"{sheet}: Total Ounces left blank on {unweighed} rows with ounce-measured items that have no ItemWeight.")
  if (long := int((rows["name"].str.len() > MAX_NAME).sum())) > 0:
    warnings.append(f"{sheet}: {long} names are longer than Michigan's {MAX_NAME}-character limit; truncated.")
    rows["name"] = rows["name"].str[:MAX_NAME]
  blanks = {column: int((rows[column].astype(str).str.strip() == "").sum()) for column in MANDATORY}
  warnings.extend(f"{sheet}: {n} rows have no {column}, which Michigan marks mandatory." for column, n in blanks.items() if n > 0)
  return rows


def month_end_rows(totals_from: pd.DataFrame, period_end: date) -> pd.DataFrame:
  """One row per brand code moving *totals_from*'s totals between our own two licenses on the month's last day."""
  if totals_from.empty:
    return pd.DataFrame(columns=list(COLUMNS))
  label_to_code = {v: k for k, v in BRAND_LABELS.items()}
  grouped = totals_from.groupby("brand", sort=True).agg(price=("price", lambda s: sum(s, Decimal(0))), cigars=("cigars", "sum"))
  return pd.DataFrame([
    {
      "brand": label, "name": COMPANY.name, "fein": COMPANY.fein, "date": period_end, "invoice_date": period_end,
      "invoice": f"{period_end:%m/%d/%y}{label_to_code[label]}", "country": "USA", "address": COMPANY.address,
      "city": COMPANY.city, "state": COMPANY.state, "zip": COMPANY.zip, "price": price, "cigars": int(cigars), "ounces": None,
    }
    for label, price, cigars in zip(grouped.index, grouped["price"], grouped["cigars"], strict=True)
  ])  # fmt: skip


def build_returns(cursor: pyodbc.Cursor, period_start: date) -> MichiganReturns:
  """Both Michigan returns' schedule rows for the month starting *period_start*.

  Schedules that only out-of-state Unclassified Acquirers, manufacturers, federal-agency sellers or
  tribal sellers file (UA T-101C, T-108A, T-108D, T-115A, T-115B) stay empty.
  """
  start = period_start.replace(day=1)
  end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
  last = start.replace(day=monthrange(start.year, start.month)[1])
  warnings: list[str] = []

  store_sales = rows_from(_fetch(cursor, sales_query(SHIPPED_MI, ~RETURN_LINE), start, end), "SW T-108C", warnings)
  exports = rows_from(_fetch(cursor, sales_query(~SHIPPED_MI, ~RETURN_LINE), start, end), "UA T-103", warnings)
  store_returns = rows_from(_fetch(cursor, sales_query(SHIPPED_MI, RETURN_LINE), start, end), "SW T-101C", warnings)
  store_returns["price"] = store_returns["price"].map(abs)
  store_returns["cigars"] = store_returns["cigars"].map(abs)
  untaxed_buys = rows_from(_fetch(cursor, purchases_query(tax_paid=False), start, end), "UA T-101A", warnings)
  taxed_buys = rows_from(_fetch(cursor, purchases_query(tax_paid=True), start, end), "SW T-101B", warnings)

  transfer = month_end_rows(store_sales, last)
  return MichiganReturns(
    sheets={
      "UnclassifiedAcquirer": {
        "T-101A": untaxed_buys,
        "T-103": exports,
        # An in-state Unclassified Acquirer owes the tax on acquisition: every untaxed purchase is
        # reported as sold, taxed, to our own Secondary Wholesaler license (June: equal to the cent).
        "T-108B": month_end_rows(untaxed_buys, last),
        "T-108C": transfer,
      },
      "SecondaryWholesaler": {
        "T-101B": pd.concat([transfer, taxed_buys], ignore_index=True),
        "T-101C": store_returns,
        "T-108C": store_sales,
      },
    },
    warnings=[*warnings, *RULES_TO_CONFIRM],
  )


def as_cells(rows: pd.DataFrame) -> list[tuple[CellValue, ...]]:
  """*rows* as plain Python cell values in template column order, blanks as None."""
  frame = rows[list(COLUMNS)].astype(object)  # boxes numpy scalars as Python ints/floats
  return [tuple(r) for r in frame.where(frame.notna(), None).itertuples(index=False)]
