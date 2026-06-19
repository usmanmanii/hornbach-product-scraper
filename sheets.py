"""
sheets.py – Google Sheets integration using gspread and service account auth.

Handles:
  • Authentication via a Google service account
  • Creating / opening spreadsheets and worksheets
  • Writing header rows
  • Appending product data rows in batches
  • Updating existing rows
"""

import logging
from typing import List, Optional

import gspread
from google.oauth2.service_account import Credentials

from config import cfg
from models import Product

logger = logging.getLogger("hornbach_scraper.sheets")

# Google API scopes required for Sheets access
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class GoogleSheetsClient:
    """
    Manages reading and writing scraped product data to Google Sheets.

    Usage:
        client = GoogleSheetsClient()
        client.connect()
        client.write_products(products)
    """

    def __init__(
        self,
        service_account_file: Optional[str] = None,
        sheet_id: Optional[str] = None,
        worksheet_name: Optional[str] = None,
    ):
        self._sa_file = service_account_file or cfg.google_service_account_file
        self._sheet_id = sheet_id or cfg.google_sheet_id
        self._worksheet_name = worksheet_name or cfg.google_worksheet_name

        self._gc: Optional[gspread.Client] = None
        self._spreadsheet = None
        self._worksheet = None

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self):
        """
        Authenticate with Google and open the target spreadsheet.

        Steps:
        1. Load credentials from the service account JSON file.
        2. Authorise the gspread client.
        3. Open the spreadsheet by ID.
        4. Open or create the target worksheet.
        """
        logger.info("Authenticating with Google Sheets API…")

        # Step 1: Load service account credentials
        credentials = Credentials.from_service_account_file(
            self._sa_file, scopes=SCOPES
        )

        # Step 2: Authorise gspread
        self._gc = gspread.authorize(credentials)
        logger.info("Authenticated successfully.")

        # Step 3: Open spreadsheet
        try:
            self._spreadsheet = self._gc.open_by_key(self._sheet_id)
            logger.info("Opened spreadsheet: %s", self._spreadsheet.title)
        except gspread.SpreadsheetNotFound:
            logger.error(
                "Spreadsheet with ID '%s' not found. "
                "Ensure it exists and is shared with the service account email.",
                self._sheet_id,
            )
            raise

        # Step 4: Open or create the worksheet
        self._worksheet = self._get_or_create_worksheet(self._worksheet_name)

    def _get_or_create_worksheet(self, name: str):
        """Open an existing worksheet or create a new one."""
        try:
            ws = self._spreadsheet.worksheet(name)
            logger.info("Using existing worksheet: '%s'", name)
            return ws
        except gspread.WorksheetNotFound:
            logger.info("Worksheet '%s' not found – creating it.", name)
            ws = self._spreadsheet.add_worksheet(
                title=name, rows=1000, cols=len(Product.header_row())
            )
            return ws

    # ── Writing data ─────────────────────────────────────────────────────

    def write_products(
        self,
        products: List[Product],
        clear_existing: bool = False,
        batch_size: int = 50,
    ):
        """
        Write a list of products to the Google Sheet.

        Args:
            products:       List of Product objects to write.
            clear_existing: If True, clear the sheet before writing.
            batch_size:     Number of rows to write per API call.
        """
        if not self._worksheet:
            raise RuntimeError("Not connected – call connect() first.")

        if not products:
            logger.warning("No products to write.")
            return

        # Optionally clear existing data
        if clear_existing:
            self._worksheet.clear()
            logger.info("Cleared existing worksheet data.")

        # Write header row if the sheet is empty
        existing_data = self._worksheet.get_all_values()
        if not existing_data:
            self._worksheet.append_row(Product.header_row())
            logger.info("Wrote header row.")

        # Convert products to rows
        rows = [p.to_row() for p in products]

        # Write in batches to stay within API rate limits
        total_written = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            self._worksheet.append_rows(batch, value_input_option="USER_ENTERED")
            total_written += len(batch)
            logger.info(
                "Wrote batch %d–%d of %d rows.",
                i + 1, i + len(batch), len(rows),
            )

        logger.info("✅ Total rows written to Google Sheets: %d", total_written)

    def update_products(self, products: List[Product], start_row: int = 2):
        """
        Overwrite rows starting from `start_row` with the given products.
        Useful for full refreshes.
        """
        if not self._worksheet:
            raise RuntimeError("Not connected – call connect() first.")

        # Ensure header exists
        header = self._worksheet.row_values(1)
        if not header:
            self._worksheet.update("A1", [Product.header_row()])

        rows = [p.to_row() for p in products]
        end_row = start_row + len(rows) - 1
        end_col = chr(ord("A") + len(Product.header_row()) - 1)
        cell_range = f"A{start_row}:{end_col}{end_row}"

        self._worksheet.update(cell_range, rows, value_input_option="USER_ENTERED")
        logger.info("Updated %d rows (range %s).", len(rows), cell_range)

    # ── Utility methods ──────────────────────────────────────────────────

    def get_row_count(self) -> int:
        """Return the number of non-empty rows in the worksheet."""
        if not self._worksheet:
            return 0
        return len(self._worksheet.get_all_values())

    def create_new_spreadsheet(self, title: str) -> str:
        """
        Create a brand-new spreadsheet and return its ID.

        Useful if the user hasn't created a sheet yet.
        """
        if not self._gc:
            raise RuntimeError("Not authenticated – call connect() first.")

        spreadsheet = self._gc.create(title)
        logger.info(
            "Created new spreadsheet: '%s' (ID: %s)",
            title, spreadsheet.id,
        )

        # Update internal state to use the new spreadsheet
        self._spreadsheet = spreadsheet
        self._sheet_id = spreadsheet.id
        self._worksheet = self._get_or_create_worksheet(self._worksheet_name)

        return spreadsheet.id
