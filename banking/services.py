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
from html.parser import HTMLParser
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from portfolio.ownership import AssetOwnershipCategory

from .models import BankBalance, BankMovement, BankStatementImport


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


def normalize_lookup_text(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_header_text(value)).strip()


def normalize_iban(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def infer_ownership_category_from_holder_name(holder_name: str) -> str | None:
    normalized_holder = normalize_lookup_text(holder_name)
    has_ximo = "XIMO" in normalized_holder
    has_monica = "MONICA" in normalized_holder

    if has_ximo and has_monica:
        return AssetOwnershipCategory.JOINT
    if has_ximo:
        return AssetOwnershipCategory.XIMO
    if has_monica:
        return AssetOwnershipCategory.MONICA
    return None


def resolve_statement_ownership_category(statement_import: BankStatementImport, metadata: dict) -> str:
    if statement_import.ownership_category in {
        AssetOwnershipCategory.XIMO,
        AssetOwnershipCategory.MONICA,
    }:
        return statement_import.ownership_category

    holder_category = infer_ownership_category_from_holder_name(metadata.get("holder_name", ""))
    if holder_category:
        return holder_category

    imported_statements = list(
        BankStatementImport.objects.filter(
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        .exclude(pk=statement_import.pk)
        .order_by("-processed_at", "-imported_at", "-id")
    )

    target_iban = normalize_iban(metadata.get("iban", ""))
    if target_iban:
        for previous_statement in imported_statements:
            if normalize_iban(previous_statement.iban) == target_iban:
                return previous_statement.ownership_category

    target_account_label = normalize_lookup_text(metadata.get("account_label", ""))
    if target_account_label:
        for previous_statement in imported_statements:
            if normalize_lookup_text(previous_statement.account_label) == target_account_label:
                return previous_statement.ownership_category

        matching_account = (
            BankBalance.objects.order_by("-updated_at", "-id")
            .filter(account_name__iexact=metadata.get("account_label", "").strip())
            .first()
        )
        if matching_account:
            return matching_account.ownership_category

    return AssetOwnershipCategory.JOINT


def import_statement(statement_import: BankStatementImport) -> BankStatementImport:
    try:
        parsed = parse_statement_file(statement_import.source_file)
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
        statement_import.ownership_category = resolve_statement_ownership_category(statement_import, parsed["metadata"])
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


def get_source_name(source) -> str:
    if isinstance(source, (str, Path)):
        return str(source)
    return str(getattr(source, "name", "uploaded_file"))


def read_source_bytes(source) -> bytes:
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()

    if hasattr(source, "open"):
        source.open("rb")
    try:
        if hasattr(source, "seek"):
            source.seek(0)
        payload = source.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return payload
    finally:
        if hasattr(source, "seek"):
            source.seek(0)
        if hasattr(source, "close"):
            source.close()


def parse_statement_file(source) -> dict:
    rows = load_rows_from_workbook(source)
    if not rows:
        raise ValidationError("El extracto esta vacio.")

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
        raise ValidationError("No se han encontrado movimientos bancarios en el extracto.")

    chronological = list(reversed(movements))
    first_balance = chronological[0].balance
    if first_balance is not None:
        metadata["opening_balance"] = first_balance - chronological[0].amount
    metadata["closing_balance"] = movements[0].balance

    return {"metadata": metadata, "movements": movements}


def load_rows_from_workbook(source) -> list[list[str]]:
    source_name = get_source_name(source)
    suffix = Path(source_name).suffix.lower()
    payload = read_source_bytes(source)
    if suffix == ".xlsx":
        return load_rows_from_xlsx(payload)
    if suffix == ".xls":
        return load_rows_from_xls(payload, source_name=source_name)
    raise ValidationError("Tipo de fichero no compatible. Sube extractos XLS o XLSX.")


def load_rows_from_xlsx(source) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValidationError("Se necesita openpyxl para importar ficheros XLSX.") from exc

    workbook_source = BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    workbook = load_workbook(workbook_source, data_only=True, read_only=True)
    worksheet = workbook.worksheets[0]
    rows = []
    for row in worksheet.iter_rows(values_only=True):
        rows.append(["" if cell is None else str(cell).strip() for cell in row])
    workbook.close()
    return rows


def load_rows_from_xls(source, source_name: str = "uploaded.xls") -> list[list[str]]:
    payload = read_source_bytes(source) if not isinstance(source, (bytes, bytearray)) else bytes(source)
    if payload[:4] == b"PK\x03\x04":
        return load_rows_from_xlsx(payload)

    errors = []

    try:
        return load_rows_from_xls_with_xlrd(payload)
    except ValidationError as exc:
        errors.append(str(exc))

    try:
        return load_rows_from_html_xls(payload)
    except ValidationError as exc:
        errors.append(str(exc))

    if os.name == "nt":
        try:
            return load_rows_from_excel_com(payload, source_name=source_name)
        except ValidationError as exc:
            errors.append(str(exc))

    detail = "; ".join(dict.fromkeys(error for error in errors if error))
    message = "No se ha podido leer automaticamente el fichero XLS."
    if detail:
        message = f"{message} {detail}"
    raise ValidationError(message)


def load_rows_from_xls_with_xlrd(source) -> list[list[str]]:
    try:
        import xlrd
    except ImportError as exc:
        raise ValidationError("Se necesita xlrd para importar ficheros XLS antiguos.") from exc

    try:
        if isinstance(source, (bytes, bytearray)):
            workbook = xlrd.open_workbook(file_contents=bytes(source), on_demand=True)
        else:
            workbook = xlrd.open_workbook(source, on_demand=True)
    except Exception as exc:
        raise ValidationError(f"xlrd no ha podido leer el fichero XLS: {exc}") from exc

    try:
        worksheet = workbook.sheet_by_index(0)
        rows = []
        for row_index in range(worksheet.nrows):
            row = []
            for column_index in range(worksheet.ncols):
                row.append(normalize_xls_cell(workbook, worksheet, row_index, column_index))
            rows.append(row)
        return rows
    finally:
        release = getattr(workbook, "release_resources", None)
        if callable(release):
            release()


def normalize_xls_cell(workbook, worksheet, row_index: int, column_index: int) -> str:
    import xlrd

    cell_type = worksheet.cell_type(row_index, column_index)
    value = worksheet.cell_value(row_index, column_index)

    if cell_type in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return ""
    if cell_type == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(value, workbook.datemode).strftime("%d/%m/%Y")
    if cell_type == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if value else "FALSE"
    if cell_type == xlrd.XL_CELL_NUMBER:
        return format_excel_number(value)
    return str(value).strip()


def format_excel_number(value) -> str:
    number = Decimal(str(value))
    if number == number.to_integral():
        return str(int(number))
    return format(number.normalize(), "f")


class _HTMLTableRowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._table_depth = 0
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
            return
        if not self._table_depth:
            return
        if tag == "tr":
            self._current_row = []
            return
        if tag in {"td", "th"}:
            self._current_cell = []
            return
        if tag == "br" and self._current_cell is not None:
            self._current_cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            return
        if not self._table_depth:
            return
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            text = re.sub(r"\s+", " ", "".join(self._current_cell)).strip()
            self._current_row.append(text)
            self._current_cell = None
            return
        if tag == "tr" and self._current_row is not None:
            if any(cell.strip() for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data):
        if self._current_cell is not None:
            self._current_cell.append(data)


def load_rows_from_html_xls(source) -> list[list[str]]:
    raw_bytes = read_source_bytes(source) if not isinstance(source, (bytes, bytearray)) else bytes(source)
    text = ""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    lowered = text.lower()
    if "<table" not in lowered or "</table>" not in lowered:
        raise ValidationError("El fichero XLS no es una exportacion HTML con tabla legible.")

    parser = _HTMLTableRowParser()
    parser.feed(text)
    parser.close()
    if not parser.rows:
        raise ValidationError("No se han encontrado filas en el XLS basado en HTML.")
    return parser.rows


def load_rows_from_excel_com(source, source_name: str = "uploaded.xls") -> list[list[str]]:
    if os.name != "nt":
        raise ValidationError("La importacion de XLS necesita Windows con Microsoft Excel instalado.")

    payload = read_source_bytes(source) if not isinstance(source, (bytes, bytearray)) else bytes(source)
    suffix = Path(source_name).suffix or ".xls"
    with NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_file.write(payload)
        temp_path = temp_file.name

    escaped_path = temp_path.replace("'", "''")
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
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or "No se ha podido abrir el fichero XLS con Excel."
            raise ValidationError(stderr)

        payload = completed.stdout.strip()
        if not payload:
            raise ValidationError("Excel no ha devuelto datos para el fichero XLS subido.")

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
    finally:
        Path(temp_path).unlink(missing_ok=True)


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
    raise ValidationError("No se ha encontrado la fila de cabecera del extracto.")


def parse_spanish_date(value: str):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"Fecha no compatible: {value}")


def parse_spanish_decimal(value: str) -> Decimal:
    text = str(value).strip().replace("\xa0", "").replace(" ", "").replace("EUR", "").replace("\u20ac", "")
    if not text:
        return ZERO
    if "," in text:
        normalized = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        normalized = text.replace(".", "")
    elif "." in text:
        integer_part, decimal_part = text.rsplit(".", 1)
        normalized = text if len(decimal_part) <= 2 else f"{integer_part}{decimal_part}"
    else:
        normalized = text
    return Decimal(normalized)


def normalize_concept(concept: str) -> str:
    return re.sub(r"\s+", " ", concept.strip().upper())


def normalize_header_text(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return ascii_text.strip().upper()


def month_label_for_date(value: date) -> str:
    return value.strftime("%Y-%m")


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

    monthly_labels = set()
    accounts = {statement.iban or statement.account_name for statement in imported_statements}
    month_map = defaultdict(
        lambda: {
            "label": "",
            "accounts": set(),
            "income": ZERO,
            "expenses": ZERO,
            "pension_contributions": ZERO,
            "dividends": ZERO,
            "latest_balances": {},
        }
    )
    income_map = defaultdict(lambda: defaultdict(lambda: ZERO))
    expense_map = defaultdict(lambda: defaultdict(lambda: ZERO))
    overall_summary = {
        "statements_count": len(imported_statements),
        "months_count": 0,
        "accounts_count": len(accounts),
        "total_income": ZERO,
        "total_expenses": ZERO,
        "total_pension_contributions": ZERO,
        "total_dividends": ZERO,
    }

    for statement in imported_statements:
        account_key = statement.iban or statement.account_name
        account_name = statement.account_name
        for movement in statement.movements.all():
            movement_month = month_label_for_date(movement.booking_date)
            monthly_labels.add(movement_month)
            bucket = month_map[movement_month]
            bucket["label"] = movement_month
            bucket["accounts"].add(account_name)

            amount_abs = abs(movement.amount)
            if movement.movement_group == BankMovement.MovementGroup.DIVIDEND:
                bucket["dividends"] += amount_abs
                overall_summary["total_dividends"] += amount_abs
            elif movement.movement_group == BankMovement.MovementGroup.PENSION:
                bucket["pension_contributions"] += amount_abs
                overall_summary["total_pension_contributions"] += amount_abs
            elif movement.movement_group == BankMovement.MovementGroup.EXPENSE:
                bucket["expenses"] += amount_abs
                overall_summary["total_expenses"] += amount_abs
            else:
                bucket["income"] += amount_abs
                overall_summary["total_income"] += amount_abs

            if (
                movement.movement_group in (BankMovement.MovementGroup.INCOME, BankMovement.MovementGroup.DIVIDEND)
                and movement.concept_bucket in TRACKED_INCOME_BUCKETS
            ):
                income_map[movement.concept_bucket][movement_month] += amount_abs
            if movement.movement_group == BankMovement.MovementGroup.EXPENSE:
                expense_map[movement.concept_bucket][movement_month] += amount_abs

            if movement.balance is not None:
                latest = bucket["latest_balances"].get(account_key)
                movement_position = (movement.booking_date, movement.id)
                if latest is None or movement_position > latest["position"]:
                    bucket["latest_balances"][account_key] = {
                        "position": movement_position,
                        "balance": movement.balance,
                    }

    if not monthly_labels:
        monthly_labels = {statement.month_label for statement in imported_statements if statement.period_end}

    overall_summary["months_count"] = len(monthly_labels)

    monthly_summaries = []
    for month_label in sorted(month_map.keys(), reverse=True):
        row = month_map[month_label]
        row["accounts_count"] = len(row["accounts"])
        row["net_cash_flow"] = row["income"] + row["dividends"] - row["expenses"] - row["pension_contributions"]
        row["closing_balance"] = sum(
            (item["balance"] for item in row["latest_balances"].values()),
            ZERO,
        )
        row.pop("latest_balances", None)
        monthly_summaries.append(row)

    expense_months = sorted(monthly_labels)
    income_matrix = []
    expense_matrix = []

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
