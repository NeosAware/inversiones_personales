from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import BankMovement, BankStatementImport


ZERO = Decimal("0.00")
HEADER_ROW = ["F. Operativa", "Concepto", "F. Valor", "Importe", "Saldo"]
PLAN_KEYWORDS = ("PLAN AHORRO", "PLAN DE PENSION", "PENSION", "APORTACION PERIODICA POL.")
DIVIDEND_KEYWORDS = ("DIVID", "DIVIDENDO", "COBRO DIVIDENDO", "ABONO DIVIDENDO")
RENT_INCOME_KEYWORDS = ("ALQUILER", "PISO", "ADRIAN")
TRACKED_INCOME_BUCKETS = (
    "Nomina",
    "Alquiler piso (Adrian)",
    "Dividendos de acciones",
)
INCOME_BUCKETS = (
    ("NOMINA", "Nomina"),
    ("ANUL COMPRA TARJ.", "Devoluciones y abonos"),
    ("ABONO", "Abonos"),
    ("TRANSFER", "Transferencias recibidas"),
    ("TRASPASO", "Transferencias recibidas"),
    ("BIZUM", "Ingresos Bizum"),
)
EXPENSE_BUCKETS = (
    (("TARJETA CREDITO",), "Tarjeta de credito"),
    (("PAGO BIZUM",), "Bizum"),
    (("MERCADONA", "LIDL", "ALDI", "CARREFOUR", "EROSKI", "CONSUM"), "Supermercado"),
    (("NETFLIX", "SPOTIFY", "APPLE.COM/BILL", "ELPAIS", "PRIME VIDEO", "DISNEY"), "Suscripciones"),
    (("NATURGY", "GAS COMERCIALIZADORA", "ENDESA", "IBERDROLA", "AGUA "), "Suministros"),
    (("APROOP TELECOM", "MOVISTAR", "VODAFONE", "ORANGE", "DIGI", "TELECOM"), "Telecomunicaciones"),
    (("PARKING", "REPSOL", "CEPSA", "BP ", "GASOLINERA", "RENFE", "UBER", "CABIFY"), "Transporte"),
    (("HOLIDAY INN", "BOOKING", "AIRBNB", "HOTEL"), "Viajes"),
    (("MUCCA", "RESTAURANTE", "CAFE", "BAR ", "BURGER", "MCDONALD"), "Restauracion"),
    (("AMAZON",), "Compras online"),
    (("ASOCIACION", "SIND"), "Cuotas y asociaciones"),
    (("ADEUDO RECIBO",), "Recibos domiciliados"),
)


class StatementImportError(Exception):
    pass


@dataclass
class ParsedMovement:
    booking_date: date
    value_date: date | None
    concept: str
    amount: Decimal
    balance: Decimal | None
    reference_1: str
    reference_2: str


@dataclass
class ClassifiedMovement:
    group: str
    bucket: str


def build_uploaded_file_checksum(uploaded_file) -> str:
    digest = sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def import_statement(statement_import: BankStatementImport) -> BankStatementImport:
    try:
        parsed = parse_statement_file(statement_import.source_file.path)
    except Exception as exc:
        statement_import.import_status = BankStatementImport.ImportStatus.FAILED
        statement_import.error_message = str(exc)
        statement_import.processed_at = timezone.now()
        statement_import.save(update_fields=["import_status", "error_message", "processed_at"])
        raise StatementImportError(str(exc)) from exc

    with transaction.atomic():
        statement_import.movements.all().delete()

        total_income = ZERO
        total_expenses = ZERO
        total_pension = ZERO
        total_dividends = ZERO
        movements_to_create = []

        for movement in parsed["movements"]:
            classified = classify_movement(movement.concept, movement.amount)
            amount_abs = abs(movement.amount)

            if classified.group == BankMovement.MovementGroup.DIVIDEND:
                total_dividends += amount_abs
            elif classified.group == BankMovement.MovementGroup.PENSION:
                total_pension += amount_abs
            elif classified.group == BankMovement.MovementGroup.EXPENSE:
                total_expenses += amount_abs
            else:
                total_income += amount_abs

            movements_to_create.append(
                BankMovement(
                    statement_import=statement_import,
                    booking_date=movement.booking_date,
                    value_date=movement.value_date,
                    concept=movement.concept,
                    normalized_concept=normalize_concept(movement.concept),
                    amount=movement.amount,
                    balance=movement.balance,
                    reference_1=movement.reference_1,
                    reference_2=movement.reference_2,
                    movement_group=classified.group,
                    concept_bucket=classified.bucket,
                )
            )

        BankMovement.objects.bulk_create(movements_to_create)

        statement_import.iban = parsed["metadata"].get("iban", "")
        statement_import.holder_name = parsed["metadata"].get("holder_name", "")
        statement_import.currency = parsed["metadata"].get("currency", "EUR")
        statement_import.period_start = parsed["metadata"].get("period_start")
        statement_import.period_end = parsed["metadata"].get("period_end")
        statement_import.account_label = parsed["metadata"].get("account_label", statement_import.account_label)
        statement_import.opening_balance = parsed["metadata"].get("opening_balance")
        statement_import.closing_balance = parsed["metadata"].get("closing_balance")
        statement_import.total_income = total_income
        statement_import.total_expenses = total_expenses
        statement_import.total_pension_contributions = total_pension
        statement_import.total_dividends = total_dividends
        statement_import.import_status = BankStatementImport.ImportStatus.IMPORTED
        statement_import.error_message = ""
        statement_import.processed_at = timezone.now()
        statement_import.save()

    return statement_import


def parse_statement_file(file_path: str) -> dict:
    rows = load_rows_from_workbook(file_path)
    if not rows:
        raise ValidationError("The statement is empty.")

    metadata = extract_statement_metadata(rows)
    header_index = find_header_row_index(rows)
    movements = []

    for raw_row in rows[header_index + 1 :]:
        row = list(raw_row) + [""] * max(0, 7 - len(raw_row))
        if not str(row[0]).strip() or not str(row[1]).strip() or not str(row[3]).strip():
            continue
        movements.append(
            ParsedMovement(
                booking_date=parse_spanish_date(row[0]),
                value_date=parse_spanish_date(row[2]) if str(row[2]).strip() else None,
                concept=str(row[1]).strip(),
                amount=parse_spanish_decimal(row[3]),
                balance=parse_spanish_decimal(row[4]) if str(row[4]).strip() else None,
                reference_1=str(row[5]).strip(),
                reference_2=str(row[6]).strip(),
            )
        )

    if not movements:
        raise ValidationError("No bank movements were found in the statement.")

    chronological = list(reversed(movements))
    first_balance = chronological[0].balance
    if first_balance is not None:
        metadata["opening_balance"] = first_balance - chronological[0].amount
    metadata["closing_balance"] = movements[0].balance

    return {"metadata": metadata, "movements": movements}


def load_rows_from_workbook(file_path: str) -> list[list[str]]:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".xlsx":
        return load_rows_from_xlsx(file_path)
    if suffix == ".xls":
        return load_rows_from_excel_com(file_path)
    raise ValidationError("Unsupported file type. Please upload XLS or XLSX extracts.")


def load_rows_from_xlsx(file_path: str) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValidationError("openpyxl is required to import XLSX files.") from exc

    workbook = load_workbook(file_path, data_only=True, read_only=True)
    worksheet = workbook.worksheets[0]
    rows = []
    for row in worksheet.iter_rows(values_only=True):
        rows.append(["" if cell is None else str(cell).strip() for cell in row])
    workbook.close()
    return rows


def load_rows_from_excel_com(file_path: str) -> list[list[str]]:
    if os.name != "nt":
        raise ValidationError("XLS import currently needs Windows with Microsoft Excel installed.")

    escaped_path = file_path.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {{
    $workbook = $excel.Workbooks.Open('{escaped_path}')
    $worksheet = $workbook.Worksheets.Item(1)
    $used = $worksheet.UsedRange
    $rows = @()
    for ($r = 1; $r -le $used.Rows.Count; $r++) {{
        $values = @()
        for ($c = 1; $c -le $used.Columns.Count; $c++) {{
            $values += [string]$worksheet.Cells.Item($r, $c).Text
        }}
        $rows += ,$values
    }}
    $workbook.Close($false)
    $rows | ConvertTo-Json -Depth 4 -Compress
}} finally {{
    $excel.Quit()
}}
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "Unable to open the XLS file via Excel."
        raise ValidationError(stderr)

    payload = completed.stdout.strip()
    if not payload:
        raise ValidationError("Excel returned no data for the uploaded XLS file.")

    rows = json.loads(payload)
    if rows and isinstance(rows[0], str):
        rows = [rows]
    normalized_rows = []
    for row in rows:
        if isinstance(row, dict) and "value" in row:
            row = row["value"]
        elif not isinstance(row, list):
            row = [row]
        normalized_rows.append(["" if cell is None else str(cell).strip() for cell in row])
    return normalized_rows


def extract_statement_metadata(rows: list[list[str]]) -> dict:
    metadata = {
        "currency": "EUR",
        "account_label": "",
        "iban": "",
        "holder_name": "",
        "period_start": None,
        "period_end": None,
        "opening_balance": None,
        "closing_balance": None,
    }
    date_pattern = re.compile(r"Desde (\d{2}/\d{2}/\d{4}) hasta (\d{2}/\d{2}/\d{4})")

    for row in rows:
        text = " ".join(str(cell).strip() for cell in row if str(cell).strip())
        if not text:
            continue
        match = date_pattern.search(text)
        if match:
            metadata["period_start"] = parse_spanish_date(match.group(1))
            metadata["period_end"] = parse_spanish_date(match.group(2))

        normalized_text = normalize_header_text(text)
        if normalized_text.startswith("CUENTA:"):
            metadata["iban"] = text.split("Cuenta:", 1)[1].strip()
            if metadata["iban"]:
                metadata["account_label"] = f"Cuenta {metadata['iban'][-4:]}"
        elif normalized_text.startswith("DIVISA:"):
            metadata["currency"] = text.split("Divisa:", 1)[1].strip() or "EUR"
        elif normalized_text.startswith("TITULAR:"):
            metadata["holder_name"] = text.split("Titular:", 1)[1].strip()

    return metadata


def find_header_row_index(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows):
        padded = [str(cell).strip() for cell in row[:5]]
        if padded == HEADER_ROW:
            return index
    raise ValidationError("The statement header row could not be found.")


def parse_spanish_date(value: str):
    return datetime.strptime(str(value).strip(), "%d/%m/%Y").date()


def parse_spanish_decimal(value: str) -> Decimal:
    text = str(value).strip().replace(".", "").replace(",", ".")
    if not text:
        return ZERO
    return Decimal(text)


def normalize_concept(concept: str) -> str:
    return re.sub(r"\s+", " ", concept.strip().upper())


def normalize_header_text(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return ascii_text.strip().upper()


def classify_movement(concept: str, amount: Decimal) -> ClassifiedMovement:
    normalized = normalize_concept(concept)

    if any(keyword in normalized for keyword in PLAN_KEYWORDS):
        return ClassifiedMovement(
            group=BankMovement.MovementGroup.PENSION,
            bucket="Aportaciones a planes",
        )

    if any(keyword in normalized for keyword in DIVIDEND_KEYWORDS):
        return ClassifiedMovement(
            group=BankMovement.MovementGroup.DIVIDEND,
            bucket="Dividendos de acciones",
        )

    if amount >= ZERO:
        if any(keyword in normalized for keyword in RENT_INCOME_KEYWORDS):
            return ClassifiedMovement(
                group=BankMovement.MovementGroup.INCOME,
                bucket="Alquiler piso (Adrian)",
            )
        for keyword, bucket in INCOME_BUCKETS:
            if keyword in normalized:
                return ClassifiedMovement(
                    group=BankMovement.MovementGroup.INCOME,
                    bucket=bucket,
                )
        return ClassifiedMovement(
            group=BankMovement.MovementGroup.INCOME,
            bucket="Otros ingresos",
        )

    for keywords, bucket in EXPENSE_BUCKETS:
        if any(keyword in normalized for keyword in keywords):
            return ClassifiedMovement(
                group=BankMovement.MovementGroup.EXPENSE,
                bucket=bucket,
            )

    return ClassifiedMovement(
        group=BankMovement.MovementGroup.EXPENSE,
        bucket="Otros gastos",
    )


def build_banking_dashboard() -> dict:
    statements = list(
        BankStatementImport.objects.prefetch_related("movements").order_by("-period_end", "-imported_at")
    )
    imported_statements = [
        statement
        for statement in statements
        if statement.import_status == BankStatementImport.ImportStatus.IMPORTED
    ]

    overall_summary = {
        "statements_count": len(imported_statements),
        "months_count": len({statement.month_label for statement in imported_statements if statement.period_end}),
        "accounts_count": len({statement.iban or statement.account_name for statement in imported_statements}),
        "total_income": sum((statement.total_income for statement in imported_statements), ZERO),
        "total_expenses": sum((statement.total_expenses for statement in imported_statements), ZERO),
        "total_pension_contributions": sum((statement.total_pension_contributions for statement in imported_statements), ZERO),
        "total_dividends": sum((statement.total_dividends for statement in imported_statements), ZERO),
    }

    monthly_summaries = []
    month_map = defaultdict(
        lambda: {
            "label": "",
            "accounts": set(),
            "income": ZERO,
            "expenses": ZERO,
            "pension_contributions": ZERO,
            "dividends": ZERO,
            "closing_balance": ZERO,
        }
    )

    for statement in imported_statements:
        month_label = statement.month_label
        bucket = month_map[month_label]
        bucket["label"] = month_label
        bucket["accounts"].add(statement.account_name)
        bucket["income"] += statement.total_income
        bucket["expenses"] += statement.total_expenses
        bucket["pension_contributions"] += statement.total_pension_contributions
        bucket["dividends"] += statement.total_dividends
        bucket["closing_balance"] += statement.closing_balance or ZERO

    for month_label in sorted(month_map.keys(), reverse=True):
        row = month_map[month_label]
        row["accounts_count"] = len(row["accounts"])
        row["net_cash_flow"] = row["income"] + row["dividends"] - row["expenses"] - row["pension_contributions"]
        monthly_summaries.append(row)

    expense_months = sorted(month_map.keys())
    income_matrix = []
    income_map = defaultdict(lambda: defaultdict(lambda: ZERO))
    expense_matrix = []
    expense_map = defaultdict(lambda: defaultdict(lambda: ZERO))
    for statement in imported_statements:
        month_label = statement.month_label
        for movement in statement.movements.all():
            if (
                movement.movement_group in (BankMovement.MovementGroup.INCOME, BankMovement.MovementGroup.DIVIDEND)
                and movement.concept_bucket in TRACKED_INCOME_BUCKETS
            ):
                income_map[movement.concept_bucket][month_label] += abs(movement.amount)
            if movement.movement_group == BankMovement.MovementGroup.EXPENSE:
                expense_map[movement.concept_bucket][month_label] += abs(movement.amount)

    for concept_bucket in TRACKED_INCOME_BUCKETS:
        values = income_map[concept_bucket]
        row_values = [values.get(month_label, ZERO) for month_label in expense_months]
        income_matrix.append(
            {
                "concept": concept_bucket,
                "values": row_values,
                "total": sum(row_values, ZERO),
            }
        )

    for concept_bucket, values in sorted(
        expense_map.items(),
        key=lambda item: sum(item[1].values()),
        reverse=True,
    ):
        row_values = [values.get(month_label, ZERO) for month_label in expense_months]
        expense_matrix.append(
            {
                "concept": concept_bucket,
                "values": row_values,
                "total": sum(row_values, ZERO),
            }
        )

    return {
        "statement_summary": overall_summary,
        "monthly_summaries": monthly_summaries,
        "income_months": expense_months,
        "income_matrix": income_matrix,
        "expense_months": expense_months,
        "expense_matrix": expense_matrix,
        "recent_imports": statements[:12],
    }
