# Tobacco Tax Automation — Status & Findings

Living status doc for the state tobacco tax filing automation effort (the `taxes_job`). Update this
file as the investigation and implementation progress — treat it as the source of truth for context
that isn't derivable from the code itself (CPA answers, data gaps, unconfirmed theories).

## Goal

Automate monthly state tobacco tax filings (currently done by hand via a CPA), starting with Michigan
(biggest state) but designing the code to extend to other states later. Lives in
`src/scheduled_report_aggregator/jobs/taxes_job/` — currently just scaffolding (`__init__.py` with an
empty `main_job`, and empty `states/{ia,mi,oh,wi}.py` stubs), not yet committed to git as of 2026-08-12.

**Why:** replacing a manual process where the user exports a UI report + a raw SQL report each month
and hands both to a CPA, who reconciles them by hand into the MI DOT e-filing (MiMATS) spreadsheets
(Unclassified Acquirer and Secondary Wholesaler forms — T-101A/B/C, T-103, T-108A/B/C/D, T-115A/B tabs).

## Key domain facts learned from reverse-engineering real June 2026 filings + the SQL warehouse DB

- DB is a wholesale/distribution ERP. Tax model already lives in `TaxGroups` (states), `TaxTypes`
  (Cigar/OTP/Chew/Vapor categories), `TaxPlans` (state+category rate/cap rules — e.g. Michigan Cigar =
  32% capped at $0.50/cigar, confirmed against real `OrderDetails.ItemTax` values).
- Tax-paid vs. tax-unpaid status is encoded via **separate `ProductLines`** per status (e.g. "Bag
  Tobacco" vs "Bag Tobacco Tax Paid"), reflected in `ProductLines.DefaultTaxType` (`1`/"None" = already
  taxed upstream, don't recompute). Also cross-checked by a `Restrictions` table with explicit
  "Tobacco Tax Paid"/"Tobacco Tax Unpaid"/"Michigan Only" tags.
- Sales side: `OrderDetails.TaxPlanID` flags MI-taxable line items directly; `ReturnLine=1` marks
  returns (feeds T-101C) — no separate return-request table is actually used (`PendingReturnWorksheets`
  is empty).
- Purchase side: `PurchasesDetails` has **no working `TaxPlanID`** (always 0) — must classify via
  `PurchasesDetails.VendorPartRowID → VendorParts.ItemRowID → ItemInformation → ProductLines` instead.
  Purchase-side dollar totals didn't reconcile cleanly against one real invoice (off by 2.7%–24%
  depending on method) — cause not yet resolved.
- **Major unconfirmed finding:** the two MI filings (Unclassified Acquirer, Secondary Wholesaler) look
  linked — Unclassified Acquirer appears to bulk-transfer its entire month's inventory to Secondary
  Wholesaler as two summary lines (OTP total + PC1/cigar total), whose exact dollar figures then
  reappear as Secondary Wholesaler's only two T-101B rows. Needs CPA confirmation before assuming this
  in code — see open questions below.
- The old manual-process code lives in a **sibling repo** `d:\SFT Software Projects\FTXTaxProgram`
  (`src/add_names_to_report.py`, `src/filter_wh_invoices.py`, `tax_depts.json` classification list,
  various `.sql` pull scripts). Confirmed via grep: it never touches the purchase side at all, and
  there is no FEIN/vendor-tax-ID reference anywhere in it or the DB exports.

## Open questions queued for the user's CPA (as of 2026-07-31)

1. "Total Ounces" column is blank on every single row of both real June 2026 filings — does she
   actually report this to MI, and if so where does that number come from?
2. The old program never handled the purchase side (T-101A/T-103 etc.) — how does she currently build
   that part of the filing?
3. ~1% of sold items (49/4875) have a bare number instead of a category name in `Category1` — how does
   she handle unrecognized/miscategorized items?
4. Vendor FEIN/tax-ID numbers aren't in the DB or old code anywhere — where does she get them?
5. Does she confirm the Unclassified Acquirer → Secondary Wholesaler bulk-transfer relationship above,
   and is the transferred amount simply "everything received/sold that month"?
6. Vendor invoices are sometimes bundled into one purchase record in the warehouse system, but MI needs
   them split back into separate invoice-number lines — how does she know how to split a bundled one?

**Status:** answers not yet received. Treat the CPA's answers as authoritative when they arrive, and
update the data-gap understanding above before writing real extraction/classification logic in
`taxes_job/`. Don't assume the bulk-transfer theory (#5) or any other unconfirmed finding above is
correct until she verifies it.

## Data files used for this investigation — no longer present in the repo

`example files/` (real June 2026 MI xlsx filings), `sql_exports/` (15-table raw DB export: OrderHeader,
OrderDetails, PurchasesHeader, PurchasesDetails, Customers, Vendors, ItemInformation, ProductLines,
TaxGroups, TaxTypes, TaxPlans, ItemPackages, Restrictions, RestrictionsItems, VendorParts), and
`FTX Tax Reports Input/` (warehouse-UI-generated per-state reports, e.g. `MI Jun 26.csv`) were all
present during the investigation but are gone from the working tree now — they may need to be
re-requested/re-exported when implementation resumes.
