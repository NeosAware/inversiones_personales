from __future__ import annotations

from collections import defaultdict
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone as django_timezone

from banking.services import load_rows_from_workbook
from portfolio.ownership import AssetOwnershipCategory

from .broker_costs import estimate_broker_costs, resolve_recurring_cost_used
from .models import EquityPosition, EquityPriceHistory, EquityTicketSnapshot


ZERO = Decimal("0.00")
ONE_HUNDRED = Decimal("100")
DEFAULT_MARKET_RANGE_KEY = "10y"
LONG_ANALYSIS_YEARS = 10
LONG_ANALYSIS_DAYS = 365 * LONG_ANALYSIS_YEARS
LONG_MONTHLY_OBSERVATIONS = 132
MONTHLY_CORRELATION_WINDOW = 120
QUARTERLY_CORRELATION_WINDOW = 40
MONTHLY_RECENT_WINDOW = 12
QUARTERLY_RECENT_WINDOW = 4
TRACKING_HORIZON_DAYS = 365
TRACKING_FORECAST_MARKERS = (91, 182, 273, TRACKING_HORIZON_DAYS)
OPTIMIZER_MAX_ENTRY_DRAG_PCT = Decimal("1.00")
OPTIMIZER_MAX_ROUNDTRIP_DRAG_PCT = Decimal("2.00")
OPTIMIZER_MIN_GAIN_TO_ROUNDTRIP_MULTIPLE = Decimal("1.80")
DEFAULT_BENCHMARK_SYMBOL = "^IBEX"
DEFAULT_BENCHMARK_NAME = "IBEX 35"
EURIBOR_REFERENCE_SYMBOL = "ECB:M.S0.N.C_EUR1Y.E"
EURIBOR_REFERENCE_NAME = "Euribor 12M"
SPAIN_HOUSE_PRICE_SYMBOL = "EUROSTAT:prc_hpi_q:ES:TOTAL:I15_Q"
SPAIN_HOUSE_PRICE_NAME = "Precio vivienda Espana"
SPAIN_ELECTRICITY_DEMAND_SYMBOL = "REE:demand:es:peninsular"
SPAIN_ELECTRICITY_DEMAND_NAME = "Demanda electrica Espana"
SPAIN_GAS_CONSUMPTION_SYMBOL = "EUROSTAT:nrg_cb_gasm:ES:IC_OBS:TJ_GCV"
SPAIN_GAS_CONSUMPTION_NAME = "Consumo de gas Espana"
BRENT_REFERENCE_SYMBOL = "BZ=F"
BRENT_REFERENCE_NAME = "Petroleo Brent"
COPPER_REFERENCE_SYMBOL = "HG=F"
COPPER_REFERENCE_NAME = "Cobre"
EURUSD_REFERENCE_SYMBOL = "EURUSD=X"
EURUSD_REFERENCE_NAME = "Euro / Dolar"
DEFAULT_EQUITY_COLUMN_MAP = {
    "broker": 0,
    "ticker": 1,
    "company_name": 2,
    "shares": 3,
    "average_cost_per_share": 4,
    "current_price_per_share": 5,
}
REFERENCE_PRESETS = {
    "ibex_35": {
        "key": "ibex_35",
        "label": DEFAULT_BENCHMARK_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
        "benchmark_symbol": DEFAULT_BENCHMARK_SYMBOL,
        "benchmark_name": DEFAULT_BENCHMARK_NAME,
        "description": "Pulso general de la bolsa espanola.",
    },
    "euribor_12m": {
        "key": "euribor_12m",
        "label": EURIBOR_REFERENCE_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.EURIBOR_12M,
        "benchmark_symbol": EURIBOR_REFERENCE_SYMBOL,
        "benchmark_name": EURIBOR_REFERENCE_NAME,
        "description": "Muy util para bancos, deuda y negocios sensibles a tipos.",
    },
    "spain_house_price": {
        "key": "spain_house_price",
        "label": SPAIN_HOUSE_PRICE_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE,
        "benchmark_symbol": SPAIN_HOUSE_PRICE_SYMBOL,
        "benchmark_name": SPAIN_HOUSE_PRICE_NAME,
        "description": "Sirve para constructoras, promotoras e inmobiliarias.",
    },
    "spain_electricity_demand": {
        "key": "spain_electricity_demand",
        "label": SPAIN_ELECTRICITY_DEMAND_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
        "benchmark_symbol": SPAIN_ELECTRICITY_DEMAND_SYMBOL,
        "benchmark_name": SPAIN_ELECTRICITY_DEMAND_NAME,
        "description": "Muy util para electricas, redes y negocios sensibles al pulso de demanda.",
    },
    "spain_gas_consumption": {
        "key": "spain_gas_consumption",
        "label": SPAIN_GAS_CONSUMPTION_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.SPAIN_GAS_CONSUMPTION,
        "benchmark_symbol": SPAIN_GAS_CONSUMPTION_SYMBOL,
        "benchmark_name": SPAIN_GAS_CONSUMPTION_NAME,
        "description": "Ayuda a leer gasistas, utilities y negocios expuestos al consumo de gas.",
    },
    "brent": {
        "key": "brent",
        "label": BRENT_REFERENCE_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
        "benchmark_symbol": BRENT_REFERENCE_SYMBOL,
        "benchmark_name": BRENT_REFERENCE_NAME,
        "description": "Referencia util para energia, transporte y costes industriales.",
    },
    "copper": {
        "key": "copper",
        "label": COPPER_REFERENCE_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
        "benchmark_symbol": COPPER_REFERENCE_SYMBOL,
        "benchmark_name": COPPER_REFERENCE_NAME,
        "description": "Buen termometro para industria, materiales y ciclo manufacturero.",
    },
    "eurusd": {
        "key": "eurusd",
        "label": EURUSD_REFERENCE_NAME,
        "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
        "benchmark_symbol": EURUSD_REFERENCE_SYMBOL,
        "benchmark_name": EURUSD_REFERENCE_NAME,
        "description": "Ayuda a leer negocios globales con ventas o compras fuera del euro.",
    },
}
IBEX_COMPANY_PROFILES = [
    {
        "company_name": "Acerinox, S.A.",
        "ticker": "ACX",
        "quote_symbol": "ACX.MC",
        "sector_label": "Materiales e industria",
        "aliases": ["ACERINOX"],
        "default_reference_key": "copper",
        "reference_keys": ["copper", "eurusd", "ibex_35"],
    },
    {
        "company_name": "ACS, Actividades de Construccion y Servicios, S.A.",
        "ticker": "ACS",
        "quote_symbol": "ACS.MC",
        "sector_label": "Construccion e infraestructuras",
        "aliases": ["ACS", "ACTIVIDADES DE CONSTRUCCION Y SERVICIOS"],
        "default_reference_key": "spain_house_price",
        "reference_keys": ["spain_house_price", "euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Acciona, S.A.",
        "ticker": "ANA",
        "quote_symbol": "ANA.MC",
        "sector_label": "Infraestructuras y energia",
        "aliases": ["ACCIONA"],
        "default_reference_key": "spain_house_price",
        "reference_keys": ["spain_house_price", "ibex_35", "brent"],
    },
    {
        "company_name": "Acciona Energia, S.A.",
        "ticker": "ANE",
        "quote_symbol": "ANE.MC",
        "sector_label": "Energia renovable",
        "aliases": ["ACCIONA ENERGIA"],
        "default_reference_key": "spain_electricity_demand",
        "reference_keys": ["spain_electricity_demand", "ibex_35", "eurusd", "brent"],
    },
    {
        "company_name": "Aena SME, S.A.",
        "ticker": "AENA",
        "quote_symbol": "AENA.MC",
        "sector_label": "Aeropuertos y movilidad",
        "aliases": ["AENA"],
        "default_reference_key": "brent",
        "reference_keys": ["brent", "eurusd", "ibex_35"],
    },
    {
        "company_name": "Amadeus IT Group, S.A.",
        "ticker": "AMS",
        "quote_symbol": "AMS.MC",
        "sector_label": "Tecnologia de viajes",
        "aliases": ["AMADEUS", "AMADEUS IT GROUP"],
        "default_reference_key": "eurusd",
        "reference_keys": ["eurusd", "brent", "ibex_35"],
    },
    {
        "company_name": "ArcelorMittal, S.A.",
        "ticker": "MTS",
        "quote_symbol": "MTS.MC",
        "sector_label": "Acero e industria",
        "aliases": ["ARCELORMITTAL", "ARCELOR MITTAL"],
        "default_reference_key": "copper",
        "reference_keys": ["copper", "eurusd", "ibex_35"],
    },
    {
        "company_name": "Banco Bilbao Vizcaya Argentaria, S.A.",
        "ticker": "BBVA",
        "quote_symbol": "BBVA.MC",
        "sector_label": "Banca",
        "aliases": ["BBVA", "BANCO BILBAO VIZCAYA ARGENTARIA"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35", "eurusd"],
    },
    {
        "company_name": "Banco de Sabadell, S.A.",
        "ticker": "SAB",
        "quote_symbol": "SAB.MC",
        "sector_label": "Banca",
        "aliases": ["SABADELL", "BANCO SABADELL", "BANCO DE SABADELL"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Banco Santander, S.A.",
        "ticker": "SAN",
        "quote_symbol": "SAN.MC",
        "sector_label": "Banca",
        "aliases": ["SANTANDER", "BANCO SANTANDER"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35", "eurusd"],
    },
    {
        "company_name": "Bankinter, S.A.",
        "ticker": "BKT",
        "quote_symbol": "BKT.MC",
        "sector_label": "Banca",
        "aliases": ["BANKINTER"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35"],
    },
    {
        "company_name": "CaixaBank, S.A.",
        "ticker": "CABK",
        "quote_symbol": "CABK.MC",
        "sector_label": "Banca",
        "aliases": ["CAIXABANK", "CAIXA BANK"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Cellnex Telecom, S.A.",
        "ticker": "CLNX",
        "quote_symbol": "CLNX.MC",
        "sector_label": "Telecomunicaciones",
        "aliases": ["CELLNEX", "CELLNEX TELECOM"],
        "default_reference_key": "ibex_35",
        "reference_keys": ["ibex_35", "euribor_12m", "eurusd"],
    },
    {
        "company_name": "Enagas, S.A.",
        "ticker": "ENG",
        "quote_symbol": "ENG.MC",
        "sector_label": "Infraestructura energetica",
        "aliases": ["ENAGAS"],
        "default_reference_key": "spain_gas_consumption",
        "reference_keys": ["spain_gas_consumption", "brent", "ibex_35", "euribor_12m"],
    },
    {
        "company_name": "Endesa, S.A.",
        "ticker": "ELE",
        "quote_symbol": "ELE.MC",
        "sector_label": "Electrica",
        "aliases": ["ENDESA"],
        "default_reference_key": "spain_electricity_demand",
        "reference_keys": ["spain_electricity_demand", "ibex_35", "brent", "euribor_12m"],
    },
    {
        "company_name": "Ferrovial SE",
        "ticker": "FER",
        "quote_symbol": "FER.MC",
        "sector_label": "Infraestructuras y concesiones",
        "aliases": ["FERROVIAL"],
        "default_reference_key": "spain_house_price",
        "reference_keys": ["spain_house_price", "euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Fluidra, S.A.",
        "ticker": "FDR",
        "quote_symbol": "FDR.MC",
        "sector_label": "Equipamiento y consumo ligado al hogar",
        "aliases": ["FLUIDRA"],
        "default_reference_key": "spain_house_price",
        "reference_keys": ["spain_house_price", "ibex_35", "eurusd"],
    },
    {
        "company_name": "Grifols, S.A.",
        "ticker": "GRF",
        "quote_symbol": "GRF.MC",
        "sector_label": "Salud",
        "aliases": ["GRIFOLS"],
        "default_reference_key": "eurusd",
        "reference_keys": ["eurusd", "ibex_35"],
    },
    {
        "company_name": "Iberdrola, S.A.",
        "ticker": "IBE",
        "quote_symbol": "IBE.MC",
        "sector_label": "Electrica",
        "aliases": ["IBERDROLA"],
        "default_reference_key": "spain_electricity_demand",
        "reference_keys": ["spain_electricity_demand", "ibex_35", "brent", "euribor_12m"],
    },
    {
        "company_name": "Inditex, S.A.",
        "ticker": "ITX",
        "quote_symbol": "ITX.MC",
        "sector_label": "Consumo global",
        "aliases": ["INDITEX", "INDUSTRIA DE DISENO TEXTIL", "ZARA"],
        "default_reference_key": "eurusd",
        "reference_keys": ["eurusd", "ibex_35"],
    },
    {
        "company_name": "Indra Sistemas, S.A.",
        "ticker": "IDR",
        "quote_symbol": "IDR.MC",
        "sector_label": "Tecnologia y defensa",
        "aliases": ["INDRA", "INDRA SISTEMAS"],
        "default_reference_key": "ibex_35",
        "reference_keys": ["ibex_35", "eurusd"],
    },
    {
        "company_name": "International Consolidated Airlines Group, S.A.",
        "ticker": "IAG",
        "quote_symbol": "IAG.MC",
        "sector_label": "Aerolineas",
        "aliases": ["IAG", "INTERNATIONAL AIRLINES GROUP"],
        "default_reference_key": "brent",
        "reference_keys": ["brent", "eurusd", "ibex_35"],
    },
    {
        "company_name": "Logista Integral, S.A.",
        "ticker": "LOG",
        "quote_symbol": "LOG.MC",
        "sector_label": "Logistica y distribucion",
        "aliases": ["LOGISTA", "LOGISTA INTEGRAL"],
        "default_reference_key": "brent",
        "reference_keys": ["brent", "ibex_35", "eurusd"],
    },
    {
        "company_name": "Mapfre, S.A.",
        "ticker": "MAP",
        "quote_symbol": "MAP.MC",
        "sector_label": "Seguros",
        "aliases": ["MAPFRE"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Merlin Properties SOCIMI, S.A.",
        "ticker": "MRL",
        "quote_symbol": "MRL.MC",
        "sector_label": "Inmobiliario",
        "aliases": ["MERLIN", "MERLIN PROPERTIES"],
        "default_reference_key": "spain_house_price",
        "reference_keys": ["spain_house_price", "euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Naturgy Energy Group, S.A.",
        "ticker": "NTGY",
        "quote_symbol": "NTGY.MC",
        "sector_label": "Gas y energia",
        "aliases": ["NATURGY"],
        "default_reference_key": "spain_gas_consumption",
        "reference_keys": ["spain_gas_consumption", "spain_electricity_demand", "brent", "ibex_35", "euribor_12m"],
    },
    {
        "company_name": "Redeia Corporacion, S.A.",
        "ticker": "RED",
        "quote_symbol": "RED.MC",
        "sector_label": "Redes electricas",
        "aliases": ["REDEIA", "RED ELECTRICA", "RED ELECTRICA CORPORACION"],
        "default_reference_key": "spain_electricity_demand",
        "reference_keys": ["spain_electricity_demand", "ibex_35", "euribor_12m"],
    },
    {
        "company_name": "Repsol, S.A.",
        "ticker": "REP",
        "quote_symbol": "REP.MC",
        "sector_label": "Energia",
        "aliases": ["REPSOL"],
        "default_reference_key": "brent",
        "reference_keys": ["brent", "spain_gas_consumption", "eurusd", "ibex_35"],
    },
    {
        "company_name": "Sacyr, S.A.",
        "ticker": "SCYR",
        "quote_symbol": "SCYR.MC",
        "sector_label": "Construccion e infraestructuras",
        "aliases": ["SACYR"],
        "default_reference_key": "spain_house_price",
        "reference_keys": ["spain_house_price", "euribor_12m", "ibex_35"],
    },
    {
        "company_name": "Solaria Energia y Medio Ambiente, S.A.",
        "ticker": "SLR",
        "quote_symbol": "SLR.MC",
        "sector_label": "Solar",
        "aliases": ["SOLARIA", "SOLARIA ENERGIA"],
        "default_reference_key": "spain_electricity_demand",
        "reference_keys": ["spain_electricity_demand", "ibex_35", "eurusd", "euribor_12m"],
    },
    {
        "company_name": "Telefonica, S.A.",
        "ticker": "TEF",
        "quote_symbol": "TEF.MC",
        "sector_label": "Telecomunicaciones",
        "aliases": ["TELEFONICA", "TELEFONICA SA"],
        "default_reference_key": "eurusd",
        "reference_keys": ["eurusd", "ibex_35", "euribor_12m"],
    },
    {
        "company_name": "Unicaja Banco, S.A.",
        "ticker": "UNI",
        "quote_symbol": "UNI.MC",
        "sector_label": "Banca",
        "aliases": ["UNICAJA", "UNICAJA BANCO"],
        "default_reference_key": "euribor_12m",
        "reference_keys": ["euribor_12m", "ibex_35"],
    },
]

IBEX_REFERENCE_WORKBOOK_FILENAME = "IBEX35_Indicadores_Referencia.xlsx"
WORKBOOK_CORRELATION_INDICATOR_ALIASES = {
    "Euribor 12m": "Euribor 12m (%)",
    "Precio electricidad": "Precio electricidad OMIE (EUR/MWh)",
    "Gasto defensa": "Gasto defensa Espana (% PIB)",
    "IPV vivienda": "Indice Precio Vivienda INE (2015=100)",
    "Trafico aereo": "Trafico aereo AENA (M pax)",
    "Brent": "Precio Brent (USD/barril prom.)",
    "Licitacion obra": "Licitacion obra publica (MrdEUR)",
    "Consumo privado": "Consumo privado Espana (var. %)",
}
WORKBOOK_LIVE_REFERENCE_ALIASES = {
    "Euribor 12m (%)": "euribor_12m",
    "IBEX 35 (puntos)": "ibex_35",
    "Consumo electrico Espana (TWh)": "spain_electricity_demand",
    "Indice Precio Vivienda INE (2015=100)": "spain_house_price",
    "Precio Brent (USD/barril prom.)": "brent",
}
WORKBOOK_BASELINE_INDICATORS = [
    "IBEX 35 (puntos)",
]


class MarketDataError(Exception):
    pass


class EquityDocumentImportError(Exception):
    pass


@dataclass
class MarketSeries:
    symbol: str
    name: str
    latest_price: Decimal
    latest_date: date
    points: list[dict]


@dataclass
class EquityDocumentPrefill:
    data: dict
    detected_fields: list[str]
    candidate_count: int
    source_kind: str


def normalize_company_lookup(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Z0-9]+", " ", ascii_text.upper())
    return re.sub(r"\s+", " ", cleaned).strip()


def build_security_lookup_keys(ticker: str = "", company_name: str = "", quote_symbol: str = "") -> set[str]:
    return {
        key
        for key in {
            normalize_company_lookup(ticker),
            normalize_company_lookup(company_name),
            normalize_company_lookup(quote_symbol),
        }
        if key
    }


def resolve_equity_sector_label(company_name: str = "", ticker: str = "", quote_symbol: str = "") -> str:
    profile = (
        find_equity_company_profile(company_name)
        or find_equity_company_profile(ticker)
        or find_equity_company_profile(quote_symbol)
    )
    return profile.get("sector_label", "") if profile else ""


def get_reference_preset(reference_key: str) -> dict:
    preset = REFERENCE_PRESETS.get(reference_key)
    return dict(preset) if preset else {}


def get_equity_company_catalog() -> list[dict]:
    catalog = []
    for profile in IBEX_COMPANY_PROFILES:
        default_reference = get_reference_preset(profile["default_reference_key"])
        reference_suggestions = [get_reference_preset(reference_key) for reference_key in profile["reference_keys"]]
        lookup_keys = {
            normalize_company_lookup(profile["company_name"]),
            normalize_company_lookup(profile["ticker"]),
            normalize_company_lookup(profile["quote_symbol"]),
        }
        lookup_keys.update(normalize_company_lookup(alias) for alias in profile.get("aliases", []))
        catalog.append(
            {
                "company_name": profile["company_name"],
                "ticker": profile["ticker"],
                "quote_symbol": profile["quote_symbol"],
                "sector_label": profile["sector_label"],
                "default_reference": default_reference,
                "reference_suggestions": reference_suggestions,
                "lookup_keys": sorted(key for key in lookup_keys if key),
            }
        )
    return catalog


def find_equity_company_profile(query: str) -> dict | None:
    normalized_query = normalize_company_lookup(query)
    if not normalized_query:
        return None

    catalog = get_equity_company_catalog()
    for entry in catalog:
        if normalized_query in entry["lookup_keys"]:
            return entry

    partial_matches = []
    for entry in catalog:
        best_key = None
        for lookup_key in entry["lookup_keys"]:
            if normalized_query and (lookup_key.startswith(normalized_query) or normalized_query in lookup_key):
                best_key = lookup_key
                break
        if best_key:
            partial_matches.append((len(best_key), entry["company_name"], entry))
    if partial_matches:
        partial_matches.sort(key=lambda item: (item[0], item[1]))
        return partial_matches[0][2]
    return None


def build_reference_suggestions_for_equity(company_name: str = "", ticker: str = "") -> list[dict]:
    profile = find_equity_company_profile(company_name) or find_equity_company_profile(ticker)
    if profile:
        return [dict(item) for item in profile["reference_suggestions"]]
    return [get_reference_preset("ibex_35")]


def apply_equity_company_defaults(data: dict, override_generic_reference: bool = True) -> dict:
    result = dict(data)
    profile = (
        find_equity_company_profile(result.get("company_name"))
        or find_equity_company_profile(result.get("ticker"))
        or find_equity_company_profile(result.get("quote_symbol"))
    )
    if not profile:
        return result

    result["company_name"] = result.get("company_name") or profile["company_name"]
    result["ticker"] = result.get("ticker") or profile["ticker"]
    result["quote_symbol"] = result.get("quote_symbol") or profile["quote_symbol"]

    generic_reference = (
        result.get("reference_profile") in {None, "", EquityPosition.ReferenceProfile.MARKET_INDEX}
        and (result.get("benchmark_symbol") in {None, "", DEFAULT_BENCHMARK_SYMBOL})
        and (result.get("benchmark_name") in {None, "", DEFAULT_BENCHMARK_NAME})
    )
    if generic_reference and override_generic_reference:
        default_reference = profile["default_reference"]
        result["reference_profile"] = default_reference["reference_profile"]
        result["benchmark_symbol"] = default_reference["benchmark_symbol"]
        result["benchmark_name"] = default_reference["benchmark_name"]

    return result


def workbook_text(value) -> str:
    return str(value or "").strip()


def workbook_decimal(value) -> Decimal | None:
    if value in {None, ""}:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = workbook_text(value)
    if not text or text in {"-", "—"}:
        return None
    text = (
        text.replace("\xa0", "")
        .replace("%", "")
        .replace("EUR", "")
        .replace("Mrd", "")
        .replace("€", "")
        .replace(",", ".")
    )
    try:
        return Decimal(text)
    except Exception:
        return None


def describe_reference_score(score: Decimal | None) -> str:
    if score is None:
        return "Sin referencia"
    if score >= Decimal("0.70"):
        return "Alta"
    if score >= Decimal("0.50"):
        return "Buena"
    if score >= Decimal("0.35"):
        return "Media"
    return "Baja"


def resolve_equities_reference_workbook_path() -> Path | None:
    configured_path = getattr(settings, "EQUITIES_REFERENCE_WORKBOOK", "")
    candidates = []
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    candidates.append(Path(settings.BASE_DIR) / IBEX_REFERENCE_WORKBOOK_FILENAME)
    candidates.append(Path(settings.BASE_DIR).parent / IBEX_REFERENCE_WORKBOOK_FILENAME)
    candidates.append(Path.cwd() / IBEX_REFERENCE_WORKBOOK_FILENAME)
    candidates.append(Path.home() / "Downloads" / IBEX_REFERENCE_WORKBOOK_FILENAME)

    for candidate in candidates:
        try:
            resolved = candidate.expanduser()
        except Exception:
            continue
        if resolved.exists():
            return resolved
    return None


def find_workbook_sheet(workbook, keyword: str):
    normalized_keyword = normalize_company_lookup(keyword)
    for worksheet in workbook.worksheets:
        if normalized_keyword in normalize_company_lookup(worksheet.title):
            return worksheet
    return None


def find_workbook_header_index(rows: list[list], first_header: str) -> int | None:
    expected = normalize_company_lookup(first_header)
    for index, row in enumerate(rows):
        if not row:
            continue
        current = normalize_company_lookup(row[0])
        if current == expected or current.startswith(expected):
            return index
    return None


def extract_year_values(row: list, start_index: int = 3) -> dict[int, Decimal]:
    values = {}
    for offset, year in enumerate(range(2019, 2026), start=start_index):
        if len(row) <= offset:
            continue
        numeric_value = workbook_decimal(row[offset])
        if numeric_value is not None:
            values[year] = numeric_value
    return values


def resolve_workbook_live_reference(indicator_name: str) -> dict | None:
    normalized_name = normalize_company_lookup(indicator_name)
    for alias, reference_key in WORKBOOK_LIVE_REFERENCE_ALIASES.items():
        if normalize_company_lookup(alias) == normalized_name:
            preset = get_reference_preset(reference_key)
            return preset or None
    return None


def match_workbook_indicator_name(raw_label: str, workbook_snapshot: dict) -> str | None:
    normalized_label = normalize_company_lookup(raw_label)
    if not normalized_label:
        return None

    candidates = []
    for short_name, full_name in workbook_snapshot.get("indicator_name_by_short", {}).items():
        for candidate_name in (short_name, full_name):
            normalized_candidate = normalize_company_lookup(candidate_name)
            if not normalized_candidate:
                continue
            if normalized_candidate in normalized_label or normalized_label in normalized_candidate:
                candidates.append((len(normalized_candidate), full_name))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def calculate_series_change_correlation(primary_series: dict[int, Decimal], secondary_series: dict[int, Decimal]) -> tuple[Decimal | None, int]:
    shared_years = sorted(set(primary_series.keys()) & set(secondary_series.keys()))
    if len(shared_years) < 4:
        return None, 0

    primary_changes = []
    secondary_changes = []
    for previous_year, current_year in zip(shared_years, shared_years[1:]):
        primary_previous = primary_series.get(previous_year)
        primary_current = primary_series.get(current_year)
        secondary_previous = secondary_series.get(previous_year)
        secondary_current = secondary_series.get(current_year)
        if None in {primary_previous, primary_current, secondary_previous, secondary_current}:
            continue
        primary_changes.append(primary_current - primary_previous)
        secondary_changes.append(secondary_current - secondary_previous)

    if len(primary_changes) < 3:
        return None, len(primary_changes)
    return pearson_correlation(primary_changes, secondary_changes), len(primary_changes)


@lru_cache(maxsize=1)
def load_ibex_reference_workbook_snapshot() -> dict:
    workbook_path = resolve_equities_reference_workbook_path()
    if workbook_path is None:
        return {
            "available": False,
            "path": "",
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

    try:
        from openpyxl import load_workbook
    except ImportError:
        return {
            "available": False,
            "path": str(workbook_path),
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

    workbook = load_workbook(workbook_path, data_only=True, read_only=True)
    try:
        summary_sheet = find_workbook_sheet(workbook, "Resumen")
        quotes_sheet = find_workbook_sheet(workbook, "Cotizaciones")
        indicators_sheet = find_workbook_sheet(workbook, "Indicadores")
        correlations_sheet = find_workbook_sheet(workbook, "Correlaciones")

        if not summary_sheet or not quotes_sheet or not indicators_sheet or not correlations_sheet:
            return {
                "available": False,
                "path": str(workbook_path),
                "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        summary_rows = [list(row) for row in summary_sheet.iter_rows(values_only=True)]
        quotes_rows = [list(row) for row in quotes_sheet.iter_rows(values_only=True)]
        indicators_rows = [list(row) for row in indicators_sheet.iter_rows(values_only=True)]
        correlations_rows = [list(row) for row in correlations_sheet.iter_rows(values_only=True)]

        summary_header_index = find_workbook_header_index(summary_rows, "Ticker")
        quotes_header_index = find_workbook_header_index(quotes_rows, "Ticker")
        indicators_header_index = find_workbook_header_index(indicators_rows, "Indicador")
        correlations_header_index = find_workbook_header_index(correlations_rows, "Sector")
        if None in {summary_header_index, quotes_header_index, indicators_header_index, correlations_header_index}:
            return {
                "available": False,
                "path": str(workbook_path),
                "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        companies = []
        companies_by_key = {}
        for row in summary_rows[summary_header_index + 1 :]:
            ticker = workbook_text(row[0] if len(row) > 0 else "")
            company_name = workbook_text(row[1] if len(row) > 1 else "")
            if not ticker or not company_name:
                continue
            company = {
                "ticker": ticker.upper(),
                "company_name": company_name,
                "sector": workbook_text(row[2] if len(row) > 2 else ""),
                "return_2025": (
                    workbook_decimal(row[3] if len(row) > 3 else None) * Decimal("100")
                    if workbook_decimal(row[3] if len(row) > 3 else None) is not None
                    else None
                ),
                "primary_reference": workbook_text(row[4] if len(row) > 4 else ""),
                "reference_source": workbook_text(row[5] if len(row) > 5 else ""),
                "primary_correlation": workbook_decimal(row[6] if len(row) > 6 else None),
                "per_2025": workbook_decimal(row[7] if len(row) > 7 else None),
                "dividend_yield": (
                    workbook_decimal(row[8] if len(row) > 8 else None) * Decimal("100")
                    if workbook_decimal(row[8] if len(row) > 8 else None) is not None
                    else None
                ),
                "market_cap_billion": workbook_decimal(row[9] if len(row) > 9 else None),
                "notes": workbook_text(row[10] if len(row) > 10 else ""),
                "quote_symbol": workbook_text(row[11] if len(row) > 11 else ""),
                "price_history": {},
            }
            companies.append(company)
            for lookup_key in {
                normalize_company_lookup(company["ticker"]),
                normalize_company_lookup(company["company_name"]),
                normalize_company_lookup(company["quote_symbol"]),
            }:
                if lookup_key:
                    companies_by_key[lookup_key] = company

        quote_prices_by_key = {}
        for row in quotes_rows[quotes_header_index + 1 :]:
            ticker = workbook_text(row[0] if len(row) > 0 else "")
            company_name = workbook_text(row[1] if len(row) > 1 else "")
            quote_symbol = workbook_text(row[15] if len(row) > 15 else "")
            price_history = extract_year_values(row)
            for lookup_key in {
                normalize_company_lookup(ticker),
                normalize_company_lookup(company_name),
                normalize_company_lookup(quote_symbol),
            }:
                if lookup_key:
                    quote_prices_by_key[lookup_key] = price_history

        for company in companies:
            for lookup_key in (
                normalize_company_lookup(company["ticker"]),
                normalize_company_lookup(company["company_name"]),
                normalize_company_lookup(company["quote_symbol"]),
            ):
                if lookup_key in quote_prices_by_key:
                    company["price_history"] = quote_prices_by_key[lookup_key]
                    break

        indicators_by_name = {}
        indicators_by_key = {}
        for row in indicators_rows[indicators_header_index + 1 :]:
            indicator_name = workbook_text(row[0] if len(row) > 0 else "")
            if not indicator_name:
                continue
            indicator_entry = {
                "name": indicator_name,
                "source": workbook_text(row[1] if len(row) > 1 else ""),
                "sector_label": workbook_text(row[2] if len(row) > 2 else ""),
                "values": extract_year_values(row),
            }
            indicators_by_name[indicator_name] = indicator_entry
            indicators_by_key[normalize_company_lookup(indicator_name)] = indicator_entry

        correlation_headers = [
            workbook_text(value)
            for value in correlations_rows[correlations_header_index][1:]
            if workbook_text(value)
        ]
        indicator_name_by_short = {}
        for short_name in correlation_headers:
            full_name = WORKBOOK_CORRELATION_INDICATOR_ALIASES.get(short_name, short_name)
            indicator_name_by_short[short_name] = full_name

        sector_map = {}
        for row in correlations_rows[correlations_header_index + 1 :]:
            sector_label = workbook_text(row[0] if len(row) > 0 else "")
            if not sector_label or sector_label.upper().startswith("NOTAS"):
                continue
            scores = {}
            for column_index, short_name in enumerate(correlation_headers, start=1):
                score = workbook_decimal(row[column_index] if len(row) > column_index else None)
                if score is not None:
                    scores[short_name] = score
            if scores:
                sector_map[sector_label] = scores

        return {
            "available": True,
            "path": str(workbook_path),
            "companies": companies,
            "companies_by_key": companies_by_key,
            "indicators_by_name": indicators_by_name,
            "indicators_by_key": indicators_by_key,
            "indicator_name_by_short": indicator_name_by_short,
            "sector_map": sector_map,
        }
    finally:
        workbook.close()


def build_workbook_reference_candidates(company: dict, workbook_snapshot: dict, position: EquityPosition | None = None) -> list[dict]:
    sector_scores = workbook_snapshot.get("sector_map", {}).get(company.get("sector", ""), {})
    selected_reference_key = None
    if position is not None:
        selected_reference_key = (
            position.reference_profile,
            position.benchmark_symbol,
            position.benchmark_name,
        )

    primary_indicator_name = match_workbook_indicator_name(company.get("primary_reference", ""), workbook_snapshot)
    candidate_names = []
    for short_name in sector_scores.keys():
        full_name = workbook_snapshot.get("indicator_name_by_short", {}).get(short_name, short_name)
        candidate_names.append(full_name)
    if primary_indicator_name:
        candidate_names.append(primary_indicator_name)
    candidate_names.extend(WORKBOOK_BASELINE_INDICATORS)

    seen = set()
    candidates = []
    for indicator_name in candidate_names:
        normalized_indicator = normalize_company_lookup(indicator_name)
        if not normalized_indicator or normalized_indicator in seen:
            continue
        seen.add(normalized_indicator)

        short_name = next(
            (
                item_short_name
                for item_short_name, item_full_name in workbook_snapshot.get("indicator_name_by_short", {}).items()
                if normalize_company_lookup(item_full_name) == normalized_indicator
            ),
            indicator_name,
        )
        sector_score = sector_scores.get(short_name)
        if primary_indicator_name and normalize_company_lookup(primary_indicator_name) == normalized_indicator:
            if company.get("primary_correlation") is not None:
                sector_score = max(sector_score or ZERO, company["primary_correlation"])

        indicator_entry = workbook_snapshot.get("indicators_by_key", {}).get(normalized_indicator)
        historical_coefficient = None
        observations_count = 0
        if company.get("price_history") and indicator_entry:
            historical_coefficient, observations_count = calculate_series_change_correlation(
                company["price_history"],
                indicator_entry.get("values", {}),
            )

        live_reference = resolve_workbook_live_reference(indicator_name)
        is_active_reference = False
        if selected_reference_key and live_reference:
            candidate_key = (
                live_reference["reference_profile"],
                live_reference["benchmark_symbol"],
                live_reference["benchmark_name"],
            )
            is_active_reference = candidate_key == selected_reference_key

        sector_component = sector_score or ZERO
        history_component = abs(historical_coefficient) if historical_coefficient is not None else ZERO
        composite_score = (sector_component * Decimal("0.65")) + (history_component * Decimal("0.35"))
        if normalize_company_lookup(primary_indicator_name or "") == normalized_indicator:
            composite_score += Decimal("0.05")

        candidates.append(
            {
                "name": indicator_name,
                "short_name": short_name,
                "sector_score": sector_score,
                "sector_score_label": describe_reference_score(sector_score),
                "historical_coefficient": historical_coefficient,
                "historical_label": describe_correlation(historical_coefficient),
                "observations_count": observations_count,
                "live_reference": live_reference,
                "supports_chart": bool(live_reference),
                "is_active_reference": is_active_reference,
                "is_primary_reference": normalize_company_lookup(primary_indicator_name or "") == normalized_indicator,
                "composite_score": composite_score,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["composite_score"],
            item["sector_score"] or ZERO,
            abs(item["historical_coefficient"]) if item["historical_coefficient"] is not None else ZERO,
            item["name"],
        ),
        reverse=True,
    )

    best_marked = False
    for candidate in candidates:
        candidate["is_best"] = not best_marked
        if not best_marked:
            best_marked = True
    return candidates


def build_reference_playbook_from_card(card: dict) -> dict:
    candidates = []
    for suggestion in card.get("suggested_references", []):
        live_reference = {
            "reference_profile": suggestion["reference_profile"],
            "benchmark_symbol": suggestion["benchmark_symbol"],
            "benchmark_name": suggestion["benchmark_name"],
        }
        historical_coefficient = suggestion["correlation"].get("coefficient")
        candidates.append(
            {
                "name": suggestion["benchmark_name"],
                "short_name": suggestion["benchmark_name"],
                "sector_score": None,
                "sector_score_label": "Sin guia sectorial",
                "historical_coefficient": historical_coefficient,
                "historical_label": suggestion["correlation"].get("label") or describe_correlation(historical_coefficient),
                "observations_count": suggestion["correlation"].get("observations_count", 0),
                "live_reference": live_reference,
                "supports_chart": True,
                "is_active_reference": suggestion.get("is_selected", False),
                "is_primary_reference": suggestion.get("is_best", False),
                "is_best": suggestion.get("is_best", False),
                "composite_score": abs(historical_coefficient) if historical_coefficient is not None else ZERO,
            }
        )

    return {
        "available": bool(candidates),
        "source_label": "Historico de la app",
        "company_sector": "",
        "current_reference_label": card.get("reference_label"),
        "current_candidate": next((candidate for candidate in candidates if candidate["is_active_reference"]), None),
        "best_candidate": next((candidate for candidate in candidates if candidate["is_best"]), None),
        "candidates": candidates[:4],
        "return_2025": None,
        "per_2025": None,
        "dividend_yield": None,
        "notes": card["position"].notes,
    }


def build_workbook_reference_playbook(company: dict, workbook_snapshot: dict, card: dict | None = None) -> dict:
    position = card["position"] if card else None
    ranked_candidates = build_workbook_reference_candidates(company, workbook_snapshot, position=position)
    current_candidate = next((candidate for candidate in ranked_candidates if candidate["is_active_reference"]), None)
    candidates = ranked_candidates[:4]
    if current_candidate and current_candidate not in candidates:
        candidates = [current_candidate, *candidates[:3]]
    current_candidate = next((candidate for candidate in candidates if candidate["is_active_reference"]), None)
    if current_candidate is None and card is None:
        current_candidate = next((candidate for candidate in candidates if candidate["is_primary_reference"]), None)

    return {
        "available": bool(candidates),
        "source_label": "Excel IBEX 2019-2025",
        "company_sector": company.get("sector", ""),
        "current_reference_label": card.get("reference_label") if card else company.get("primary_reference"),
        "current_candidate": current_candidate,
        "best_candidate": next((candidate for candidate in candidates if candidate["is_best"]), None),
        "candidates": candidates,
        "return_2025": company.get("return_2025"),
        "per_2025": company.get("per_2025"),
        "dividend_yield": company.get("dividend_yield"),
        "notes": company.get("notes", ""),
    }


def find_history_card_for_workbook_company(company: dict, history_cards: list[dict]) -> dict | None:
    lookup_keys = build_security_lookup_keys(
        ticker=company.get("ticker", ""),
        company_name=company.get("company_name", ""),
        quote_symbol=company.get("quote_symbol", ""),
    )
    for card in history_cards:
        position = card["position"]
        position_keys = build_security_lookup_keys(
            ticker=position.ticker,
            company_name=position.company_name,
            quote_symbol=position.quote_symbol,
        )
        if lookup_keys & position_keys:
            return card
    return None


def build_reference_guide_row(playbook: dict, card: dict | None = None, company: dict | None = None) -> dict:
    current_candidate = playbook.get("current_candidate")
    position = card["position"] if card else None
    status_key = "guide"
    status_label = "Solo guia"
    detail_anchor = ""
    current_live_correlation = None
    one_year_return = None

    if position is not None:
        status_key = "owned" if position.is_owned else "watchlist"
        status_label = position.get_position_kind_display()
        detail_anchor = f"stock-{position.id}"
        current_live_correlation = card.get("correlation", {}).get("coefficient")
        one_year_snapshot = next((snapshot for snapshot in card.get("period_snapshots", []) if snapshot.get("label") == "1Y"), None)
        if one_year_snapshot and one_year_snapshot.get("available"):
            one_year_return = one_year_snapshot.get("stock_return_pct")

    ticker = company.get("ticker") if company else position.ticker
    company_name = company.get("company_name") if company else position.company_name
    sector = playbook.get("company_sector") or (company.get("sector") if company else "")

    return {
        "ticker": ticker,
        "company_name": company_name,
        "sector": sector,
        "status_key": status_key,
        "status_label": status_label,
        "is_tracked": bool(position),
        "detail_anchor": detail_anchor,
        "current_reference_label": playbook.get("current_reference_label"),
        "current_candidate": current_candidate,
        "current_live_correlation": current_live_correlation,
        "best_candidate": playbook.get("best_candidate"),
        "top_candidates": playbook.get("candidates", [])[:3],
        "return_2025": playbook.get("return_2025"),
        "per_2025": playbook.get("per_2025"),
        "dividend_yield": playbook.get("dividend_yield"),
        "notes": playbook.get("notes"),
        "one_year_return": one_year_return,
        "source_label": playbook.get("source_label"),
    }


def build_equity_reference_guide(history_cards: list[dict]) -> dict:
    workbook_snapshot = load_ibex_reference_workbook_snapshot()
    rows = []
    matched_position_ids = set()

    if workbook_snapshot.get("available"):
        for company in workbook_snapshot.get("companies", []):
            card = find_history_card_for_workbook_company(company, history_cards)
            if card is not None:
                matched_position_ids.add(card["position"].id)
            playbook = build_workbook_reference_playbook(company, workbook_snapshot, card=card)
            if card is not None:
                card["reference_playbook"] = playbook
            rows.append(build_reference_guide_row(playbook, card=card, company=company))

    for card in history_cards:
        if card["position"].id in matched_position_ids or card.get("reference_playbook"):
            continue
        playbook = build_reference_playbook_from_card(card)
        card["reference_playbook"] = playbook
        rows.append(build_reference_guide_row(playbook, card=card))

    rows.sort(
        key=lambda item: (
            0 if item["status_key"] == "owned" else 1 if item["status_key"] == "watchlist" else 2,
            item["sector"],
            item["company_name"],
        )
    )
    tracked_rows = [row for row in rows if row["is_tracked"]]
    if not tracked_rows:
        tracked_rows = rows[:6]

    return {
        "rows": rows,
        "tracked_rows": tracked_rows,
        "summary": {
            "available": bool(rows),
            "workbook_loaded": workbook_snapshot.get("available", False),
            "source_label": Path(workbook_snapshot["path"]).name if workbook_snapshot.get("path") else "",
            "tracked_count": len([row for row in rows if row["is_tracked"]]),
            "owned_count": len([row for row in rows if row["status_key"] == "owned"]),
            "watchlist_count": len([row for row in rows if row["status_key"] == "watchlist"]),
            "guide_only_count": len([row for row in rows if row["status_key"] == "guide"]),
        },
    }


def fetch_market_series(symbol: str, range_key: str = DEFAULT_MARKET_RANGE_KEY, interval: str = "1d") -> MarketSeries:
    params = urlencode({"range": range_key, "interval": interval, "includePrePost": "false"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    error = payload.get("chart", {}).get("error")
    if error:
        raise MarketDataError(error.get("description", f"No se han podido cargar los datos de mercado de {symbol}."))

    result = payload["chart"]["result"][0]
    meta = result["meta"]
    timestamps = result.get("timestamp", [])
    quote_data = result["indicators"]["quote"][0]
    opens = quote_data.get("open", [])
    highs = quote_data.get("high", [])
    lows = quote_data.get("low", [])
    closes = quote_data.get("close", [])
    points = []

    for index, timestamp in enumerate(timestamps):
        close = closes[index] if index < len(closes) else None
        if close is None:
            continue
        open_value = opens[index] if index < len(opens) else None
        high_value = highs[index] if index < len(highs) else None
        low_value = lows[index] if index < len(lows) else None
        points.append(
            {
                "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date(),
                "open": Decimal(str(round(open_value, 4))) if open_value is not None else None,
                "high": Decimal(str(round(high_value, 4))) if high_value is not None else None,
                "low": Decimal(str(round(low_value, 4))) if low_value is not None else None,
                "close": Decimal(str(round(close, 4))),
            }
        )

    if not points:
        raise MarketDataError(f"No se han recibido precios historicos para {symbol}.")

    latest_raw = meta.get("regularMarketPrice")
    latest_timestamp = meta.get("regularMarketTime") or timestamps[-1]
    latest_price = Decimal(str(round(latest_raw if latest_raw is not None else float(points[-1]["close"]), 4)))
    latest_date = datetime.fromtimestamp(latest_timestamp, tz=timezone.utc).date()

    return MarketSeries(
        symbol=symbol,
        name=meta.get("longName") or meta.get("shortName") or symbol,
        latest_price=latest_price,
        latest_date=latest_date,
        points=points,
    )


def fetch_ecb_reference_series(
    series_key: str,
    series_name: str,
    last_n_observations: int = LONG_MONTHLY_OBSERVATIONS,
) -> MarketSeries:
    url = (
        "https://data-api.ecb.europa.eu/service/data/RTD/"
        f"{series_key}?format=jsondata&lastNObservations={last_n_observations}"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    time_values = payload["structure"]["dimensions"]["observation"][0]["values"]
    series_map = payload["dataSets"][0]["series"]
    if not series_map:
        raise MarketDataError(f"No se han recibido observaciones del BCE para {series_name}.")

    observations = next(iter(series_map.values()))["observations"]
    points = []
    for raw_index, values in observations.items():
        period_label = time_values[int(raw_index)]["id"]
        year, month = map(int, period_label.split("-"))
        points.append(
            {
                "date": date(year, month, 1),
                "open": None,
                "high": None,
                "low": None,
                "close": Decimal(str(values[0])),
            }
        )

    if not points:
        raise MarketDataError(f"No se han recibido observaciones del BCE para {series_name}.")

    latest = points[-1]
    return MarketSeries(
        symbol=f"ECB:{series_key}",
        name=series_name,
        latest_price=latest["close"],
        latest_date=latest["date"],
        points=points,
    )


def parse_eurostat_quarter_label(value: str) -> date:
    year_text, quarter_text = value.split("-Q")
    year = int(year_text)
    quarter = int(quarter_text)
    month = quarter * 3
    month_day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    return date(year, month, month_day)


def fetch_eurostat_house_price_series() -> MarketSeries:
    url = (
        "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
        "prc_hpi_q?geo=ES&purchase=TOTAL&unit=I15_Q"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    time_index = payload["dimension"]["time"]["category"]["index"]
    value_map = payload.get("value", {})
    points = []
    for label, raw_index in sorted(time_index.items(), key=lambda item: item[1]):
        if str(raw_index) not in value_map:
            continue
        points.append(
            {
                "date": parse_eurostat_quarter_label(label),
                "open": None,
                "high": None,
                "low": None,
                "close": Decimal(str(value_map[str(raw_index)])),
            }
        )

    if not points:
        raise MarketDataError("No se han recibido observaciones del indice de vivienda de Eurostat.")

    latest = points[-1]
    return MarketSeries(
        symbol=SPAIN_HOUSE_PRICE_SYMBOL,
        name=SPAIN_HOUSE_PRICE_NAME,
        latest_price=latest["close"],
        latest_date=latest["date"],
        points=points,
    )


def parse_datetime_to_date(value: str) -> date:
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()


def fetch_ree_electricity_demand_series() -> MarketSeries:
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=LONG_ANALYSIS_DAYS)
    params = urlencode(
        {
            "start_date": f"{start_date.isoformat()}T00:00",
            "end_date": f"{end_date.isoformat()}T23:59",
            "time_trunc": "month",
            "geo_trunc": "electric_system",
            "geo_limit": "peninsular",
            "geo_ids": "8741",
        }
    )
    url = f"https://apidatos.ree.es/es/datos/demanda/evolucion?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    series = (payload.get("included") or [{}])[0]
    values = series.get("attributes", {}).get("values", [])
    points = [
        {
            "date": parse_datetime_to_date(item["datetime"]),
            "open": None,
            "high": None,
            "low": None,
            "close": Decimal(str(item["value"])),
        }
        for item in values
        if item.get("value") is not None and item.get("datetime")
    ]
    if not points:
        raise MarketDataError("No se han recibido observaciones de demanda electrica de REE.")

    latest = points[-1]
    return MarketSeries(
        symbol=SPAIN_ELECTRICITY_DEMAND_SYMBOL,
        name=SPAIN_ELECTRICITY_DEMAND_NAME,
        latest_price=latest["close"],
        latest_date=latest["date"],
        points=points,
    )


def fetch_eurostat_gas_consumption_series() -> MarketSeries:
    url = (
        "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
        "nrg_cb_gasm?freq=M&nrg_bal=IC_OBS&siec=G3000&unit=TJ_GCV&geo=ES"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    time_index = payload["dimension"]["time"]["category"]["index"]
    value_map = payload.get("value", {})
    points = []
    for label, raw_index in sorted(time_index.items(), key=lambda item: item[1]):
        raw_value = value_map.get(str(raw_index))
        if raw_value is None:
            continue
        year, month = map(int, label.split("-"))
        points.append(
            {
                "date": date(year, month, 1),
                "open": None,
                "high": None,
                "low": None,
                "close": Decimal(str(raw_value)),
            }
        )

    if not points:
        raise MarketDataError("No se han recibido observaciones del consumo de gas de Eurostat.")

    latest = points[-1]
    return MarketSeries(
        symbol=SPAIN_GAS_CONSUMPTION_SYMBOL,
        name=SPAIN_GAS_CONSUMPTION_NAME,
        latest_price=latest["close"],
        latest_date=latest["date"],
        points=points,
    )


def fetch_reference_series_for_choice(
    reference_profile: str,
    benchmark_symbol: str = "",
    benchmark_name: str = "",
) -> MarketSeries | None:
    if reference_profile == EquityPosition.ReferenceProfile.EURIBOR_12M:
        return fetch_ecb_reference_series("M.S0.N.C_EUR1Y.E", EURIBOR_REFERENCE_NAME)
    if reference_profile == EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE:
        return fetch_eurostat_house_price_series()
    if reference_profile == EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND:
        return fetch_ree_electricity_demand_series()
    if reference_profile == EquityPosition.ReferenceProfile.SPAIN_GAS_CONSUMPTION:
        return fetch_eurostat_gas_consumption_series()
    if benchmark_symbol:
        return fetch_market_series(benchmark_symbol)
    return None


def fetch_reference_series(position: EquityPosition) -> MarketSeries | None:
    return fetch_reference_series_for_choice(
        position.reference_profile,
        benchmark_symbol=position.benchmark_symbol,
        benchmark_name=position.benchmark_name,
    )


def normalize_document_text(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().upper()


def parse_document_decimal(value) -> Decimal | None:
    text = str(value or "").strip().replace("\xa0", "").replace(" ", "")
    text = text.replace("EUR", "").replace("€", "").replace("%", "")
    if not text:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if text.endswith("-"):
        negative = True
        text = text[:-1]

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            normalized = text.replace(".", "").replace(",", ".")
        else:
            normalized = text.replace(",", "")
    elif "," in text:
        normalized = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        normalized = text.replace(".", "")
    else:
        normalized = text

    try:
        number = Decimal(normalized)
    except Exception:
        return None
    return -number if negative else number


def clean_company_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def clean_symbol(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def clean_ticker(value: str) -> str:
    symbol = clean_symbol(value)
    if "." in symbol:
        symbol = symbol.split(".", 1)[0]
    return symbol


def infer_ownership_category_from_text(text: str, default_category: str) -> str:
    normalized = normalize_document_text(text)
    has_ximo = "XIMO" in normalized
    has_monica = "MONICA" in normalized
    if has_ximo and has_monica:
        return AssetOwnershipCategory.JOINT
    if has_ximo:
        return AssetOwnershipCategory.XIMO
    if has_monica:
        return AssetOwnershipCategory.MONICA
    return default_category


def label_matches(label: str, *tokens: str) -> bool:
    return any(token in label for token in tokens)


def build_equity_column_map(row: list[str]) -> dict[str, int]:
    column_map: dict[str, int] = {}
    for index, cell in enumerate(row):
        label = normalize_document_text(cell)
        if not label:
            continue
        if "broker" not in column_map and label_matches(label, "BROKER", "ENTIDAD", "CUSTODIO", "INTERMEDIARIO"):
            column_map["broker"] = index
        elif "ticker" not in column_map and label_matches(label, "TICKER", "SIMBOLO", "SYMBOL", "CODIGO VALOR"):
            column_map["ticker"] = index
        elif "quote_symbol" not in column_map and label_matches(label, "SIMBOLO DE MERCADO", "MARKET SYMBOL", "COTIZACION"):
            column_map["quote_symbol"] = index
        elif "company_name" not in column_map and label_matches(
            label,
            "EMPRESA",
            "COMPANIA",
            "COMPANY",
            "DESCRIPCION",
            "INSTRUMENTO",
            "VALOR",
            "NOMBRE",
        ):
            column_map["company_name"] = index
        elif "shares" not in column_map and label_matches(label, "ACCIONES", "TITULOS", "SHARES", "UNIDADES", "CANTIDAD"):
            column_map["shares"] = index
        elif "average_cost_per_share" not in column_map and label_matches(
            label,
            "COSTE MEDIO",
            "PRECIO MEDIO",
            "AVERAGE COST",
            "COST BASIS",
        ):
            column_map["average_cost_per_share"] = index
        elif "current_price_per_share" not in column_map and label_matches(
            label,
            "PRECIO ACTUAL",
            "ULTIMO PRECIO",
            "LAST PRICE",
            "MARKET PRICE",
            "COTIZACION",
        ):
            column_map["current_price_per_share"] = index
        elif "current_value" not in column_map and label_matches(
            label,
            "VALOR ACTUAL",
            "VALOR MERCADO",
            "MARKET VALUE",
            "VALORACION",
        ):
            column_map["current_value"] = index
        elif "invested_amount" not in column_map and label_matches(
            label,
            "IMPORTE INVERTIDO",
            "COSTE TOTAL",
            "TOTAL COSTE",
            "INVESTED AMOUNT",
        ):
            column_map["invested_amount"] = index
        elif "annual_dividend_income" not in column_map and label_matches(
            label,
            "DIVIDENDO ANUAL",
            "ANNUAL DIVIDEND",
            "DIVIDENDOS ANUALES",
        ):
            column_map["annual_dividend_income"] = index
    return column_map


def has_useful_equity_columns(column_map: dict[str, int]) -> bool:
    return (
        ("ticker" in column_map or "company_name" in column_map)
        and "shares" in column_map
        and (
            "average_cost_per_share" in column_map
            or "current_price_per_share" in column_map
            or "invested_amount" in column_map
            or "current_value" in column_map
        )
    )


def get_row_cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def build_equity_data_from_table_row(
    row: list[str],
    column_map: dict[str, int],
    default_broker: str,
    default_ownership_category: str,
) -> dict:
    shares = parse_document_decimal(get_row_cell(row, column_map.get("shares")))
    if shares in {None, ZERO}:
        return {}

    invested_amount = parse_document_decimal(get_row_cell(row, column_map.get("invested_amount")))
    current_value = parse_document_decimal(get_row_cell(row, column_map.get("current_value")))
    average_cost = parse_document_decimal(get_row_cell(row, column_map.get("average_cost_per_share")))
    current_price = parse_document_decimal(get_row_cell(row, column_map.get("current_price_per_share")))

    if average_cost is None and invested_amount is not None and shares:
        average_cost = invested_amount / shares
    if current_price is None and current_value is not None and shares:
        current_price = current_value / shares
    if current_price is None and average_cost is not None:
        current_price = average_cost

    ticker = clean_ticker(get_row_cell(row, column_map.get("ticker")))
    quote_symbol = clean_symbol(get_row_cell(row, column_map.get("quote_symbol")))
    if not ticker and quote_symbol:
        ticker = clean_ticker(quote_symbol)

    company_name = clean_company_name(get_row_cell(row, column_map.get("company_name")))
    broker = clean_company_name(get_row_cell(row, column_map.get("broker"))) or default_broker
    annual_dividend_income = parse_document_decimal(get_row_cell(row, column_map.get("annual_dividend_income"))) or ZERO
    ownership_category = infer_ownership_category_from_text(
        " ".join(str(cell) for cell in row),
        default_ownership_category,
    )

    if not ticker and not company_name:
        return {}

    return apply_equity_company_defaults(
        {
            "position_kind": EquityPosition.PositionKind.OWNED,
            "ownership_category": ownership_category,
            "broker": broker,
            "ticker": ticker,
            "company_name": company_name,
        "quote_symbol": quote_symbol,
        "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
        "benchmark_symbol": DEFAULT_BENCHMARK_SYMBOL,
        "benchmark_name": DEFAULT_BENCHMARK_NAME,
        "shares": shares,
        "average_cost_per_share": average_cost,
            "current_price_per_share": current_price,
            "annual_dividend_income": annual_dividend_income,
            "notes": "",
        }
    )


def assign_prefill_value(target: dict, label: str, value: str):
    if not value:
        return
    if label_matches(label, "BROKER", "ENTIDAD", "CUSTODIO", "INTERMEDIARIO"):
        target["broker"] = clean_company_name(value)
    elif label_matches(label, "TICKER", "SIMBOLO", "SYMBOL", "CODIGO VALOR"):
        target["ticker"] = clean_ticker(value)
    elif label_matches(label, "SIMBOLO DE MERCADO", "MARKET SYMBOL", "COTIZACION"):
        target["quote_symbol"] = clean_symbol(value)
    elif label_matches(label, "EMPRESA", "COMPANIA", "COMPANY", "DESCRIPCION", "INSTRUMENTO", "NOMBRE"):
        target["company_name"] = clean_company_name(value)
    elif label_matches(label, "ACCIONES", "TITULOS", "SHARES", "UNIDADES", "CANTIDAD"):
        number = parse_document_decimal(value)
        if number is not None:
            target["shares"] = number
    elif label_matches(label, "COSTE MEDIO", "PRECIO MEDIO", "AVERAGE COST", "COST BASIS"):
        number = parse_document_decimal(value)
        if number is not None:
            target["average_cost_per_share"] = number
    elif label_matches(label, "PRECIO ACTUAL", "ULTIMO PRECIO", "LAST PRICE", "MARKET PRICE", "COTIZACION"):
        number = parse_document_decimal(value)
        if number is not None:
            target["current_price_per_share"] = number
    elif label_matches(label, "VALOR ACTUAL", "VALOR MERCADO", "MARKET VALUE", "VALORACION"):
        number = parse_document_decimal(value)
        if number is not None:
            target["current_value"] = number
    elif label_matches(label, "IMPORTE INVERTIDO", "COSTE TOTAL", "TOTAL COSTE", "INVESTED AMOUNT"):
        number = parse_document_decimal(value)
        if number is not None:
            target["invested_amount"] = number
    elif label_matches(label, "DIVIDENDO ANUAL", "ANNUAL DIVIDEND", "DIVIDENDOS ANUALES"):
        number = parse_document_decimal(value)
        if number is not None:
            target["annual_dividend_income"] = number


def build_equity_data_from_key_value_rows(
    rows: list[list[str]],
    default_broker: str,
    default_ownership_category: str,
) -> dict:
    parsed: dict = {}
    for row in rows[:80]:
        non_empty = [str(cell).strip() for cell in row if str(cell).strip()]
        if len(non_empty) < 2:
            continue
        pairs = [(non_empty[0], non_empty[1])]
        if len(non_empty) >= 4:
            pairs.append((non_empty[2], non_empty[3]))
        for raw_label, raw_value in pairs:
            assign_prefill_value(parsed, normalize_document_text(raw_label), raw_value)

    if not parsed:
        return {}

    if "quote_symbol" in parsed and "ticker" not in parsed:
        parsed["ticker"] = clean_ticker(parsed["quote_symbol"])

    shares = parsed.get("shares")
    invested_amount = parsed.get("invested_amount")
    current_value = parsed.get("current_value")
    average_cost = parsed.get("average_cost_per_share")
    current_price = parsed.get("current_price_per_share")

    if average_cost is None and shares and invested_amount is not None:
        parsed["average_cost_per_share"] = invested_amount / shares
    if current_price is None and shares and current_value is not None:
        parsed["current_price_per_share"] = current_value / shares
    if parsed.get("current_price_per_share") is None and parsed.get("average_cost_per_share") is not None:
        parsed["current_price_per_share"] = parsed["average_cost_per_share"]

    parsed.setdefault("broker", default_broker)
    parsed.setdefault("position_kind", EquityPosition.PositionKind.OWNED)
    parsed.setdefault("ownership_category", default_ownership_category)
    parsed.setdefault("reference_profile", EquityPosition.ReferenceProfile.MARKET_INDEX)
    parsed.setdefault("benchmark_symbol", DEFAULT_BENCHMARK_SYMBOL)
    parsed.setdefault("benchmark_name", DEFAULT_BENCHMARK_NAME)
    parsed.setdefault("annual_dividend_income", ZERO)
    parsed.setdefault("notes", "")

    if not parsed.get("ticker") and not parsed.get("company_name"):
        return {}
    if parsed.get("shares") in {None, ZERO}:
        return {}
    if parsed.get("average_cost_per_share") is None:
        return {}

    return apply_equity_company_defaults(parsed)


def score_equity_data(data: dict) -> int:
    return sum(
        1
        for field_name in (
            "broker",
            "ticker",
            "company_name",
            "quote_symbol",
            "shares",
            "average_cost_per_share",
            "current_price_per_share",
            "annual_dividend_income",
        )
        if data.get(field_name) not in {None, "", ZERO}
    )


def finalize_equity_prefill(data: dict, default_ownership_category: str, document_text: str) -> dict:
    result = apply_equity_company_defaults(data)
    result["ownership_category"] = infer_ownership_category_from_text(
        document_text,
        result.get("ownership_category") or default_ownership_category,
    )
    result.setdefault("position_kind", EquityPosition.PositionKind.OWNED)
    result.setdefault("reference_profile", EquityPosition.ReferenceProfile.MARKET_INDEX)
    result.setdefault("benchmark_symbol", DEFAULT_BENCHMARK_SYMBOL)
    result.setdefault("benchmark_name", DEFAULT_BENCHMARK_NAME)
    result.setdefault("annual_dividend_income", ZERO)
    result.setdefault("notes", "")
    result.setdefault("broker", "")
    result.setdefault("quote_symbol", "")
    if result.get("quote_symbol") and not result.get("ticker"):
        result["ticker"] = clean_ticker(result["quote_symbol"])
    if result.get("current_price_per_share") is None and result.get("average_cost_per_share") is not None:
        result["current_price_per_share"] = result["average_cost_per_share"]
    return result


def extract_equity_prefill_from_rows(
    rows: list[list[str]],
    default_broker: str,
    default_ownership_category: str,
) -> tuple[dict, int]:
    document_text = " ".join(" ".join(str(cell) for cell in row) for row in rows)
    candidates = []

    key_value_data = build_equity_data_from_key_value_rows(rows, default_broker, default_ownership_category)
    if key_value_data:
        candidates.append(key_value_data)

    for index, row in enumerate(rows):
        column_map = build_equity_column_map(row)
        if not has_useful_equity_columns(column_map):
            continue
        table_candidates = []
        for data_row in rows[index + 1 :]:
            candidate = build_equity_data_from_table_row(
                data_row,
                column_map,
                default_broker,
                default_ownership_category,
            )
            if candidate:
                table_candidates.append(candidate)
        if table_candidates:
            candidates.extend(table_candidates)
            break

    if not candidates:
        for index, row in enumerate(rows):
            candidate = build_equity_data_from_table_row(
                row,
                DEFAULT_EQUITY_COLUMN_MAP,
                default_broker,
                default_ownership_category,
            )
            if candidate:
                candidates.append(candidate)
                for data_row in rows[index + 1 :]:
                    next_candidate = build_equity_data_from_table_row(
                        data_row,
                        DEFAULT_EQUITY_COLUMN_MAP,
                        default_broker,
                        default_ownership_category,
                    )
                    if next_candidate:
                        candidates.append(next_candidate)
                break

    if not candidates:
        return {}, 0

    best = max(candidates, key=score_equity_data)
    return finalize_equity_prefill(best, default_ownership_category, document_text), len(candidates)


def read_pdf_pages(file_source) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise EquityDocumentImportError("Se necesita pypdf para leer documentos PDF de acciones.") from exc

    if hasattr(file_source, "open"):
        file_source.open("rb")
    try:
        if hasattr(file_source, "seek"):
            file_source.seek(0)
        reader = PdfReader(file_source)
        return [(page.extract_text() or "") for page in reader.pages]
    finally:
        if hasattr(file_source, "seek"):
            file_source.seek(0)
        if hasattr(file_source, "close"):
            file_source.close()


def extract_equity_position_prefill(
    uploaded_file,
    default_broker: str = "",
    default_ownership_category: str = AssetOwnershipCategory.JOINT,
) -> EquityDocumentPrefill:
    source_name = str(getattr(uploaded_file, "name", "documento")).lower()
    suffix = Path(source_name).suffix.lower()

    if suffix in {".xls", ".xlsx"}:
        try:
            rows = load_rows_from_workbook(uploaded_file)
        except ValidationError as exc:
            raise EquityDocumentImportError(str(exc)) from exc
        data, candidate_count = extract_equity_prefill_from_rows(rows, default_broker, default_ownership_category)
        source_kind = "XLS"
    elif suffix == ".pdf":
        pages = read_pdf_pages(uploaded_file)
        rows = []
        for page in pages:
            for line in page.splitlines():
                cells = [segment.strip() for segment in re.split(r"\s{2,}|\t+", line) if segment.strip()]
                if cells:
                    rows.append(cells)
        data, candidate_count = extract_equity_prefill_from_rows(rows, default_broker, default_ownership_category)
        source_kind = "PDF"
    else:
        raise EquityDocumentImportError("Tipo de fichero no compatible. Sube un XLS, XLSX o PDF.")

    if not data:
        raise EquityDocumentImportError(
            "No se han reconocido suficientes datos de la posicion en el documento. Revisa el fichero o completa el formulario manualmente."
        )

    detected_fields = [
        field_name
        for field_name in (
            "ownership_category",
            "broker",
            "ticker",
            "company_name",
            "quote_symbol",
            "shares",
            "average_cost_per_share",
            "current_price_per_share",
            "annual_dividend_income",
        )
        if data.get(field_name) not in {None, ""}
    ]

    return EquityDocumentPrefill(
        data=data,
        detected_fields=detected_fields,
        candidate_count=candidate_count,
        source_kind=source_kind,
    )


def align_reference_points(position_points: list[dict], reference_points: list[dict]) -> dict[date, Decimal | None]:
    ordered_reference = sorted(reference_points, key=lambda item: item["date"])
    aligned: dict[date, Decimal | None] = {}
    current_reference = None
    reference_index = 0

    for point in sorted(position_points, key=lambda item: item["date"]):
        while reference_index < len(ordered_reference) and ordered_reference[reference_index]["date"] <= point["date"]:
            current_reference = ordered_reference[reference_index]["close"]
            reference_index += 1
        aligned[point["date"]] = current_reference
    return aligned


def sync_equity_market_data(position: EquityPosition) -> EquityPosition:
    if not position.quote_symbol:
        raise MarketDataError(f"{position.ticker} no tiene configurado un simbolo de cotizacion.")

    position_series = fetch_market_series(position.quote_symbol)
    benchmark_series = fetch_reference_series(position)
    benchmark_map = align_reference_points(position_series.points, benchmark_series.points) if benchmark_series else {}
    point_dates = {point["date"] for point in position_series.points}

    with transaction.atomic():
        EquityPriceHistory.objects.filter(position=position).exclude(price_date__in=point_dates).delete()
        for point in position_series.points:
            EquityPriceHistory.objects.update_or_create(
                position=position,
                price_date=point["date"],
                defaults={
                    "open_price": point.get("open"),
                    "high_price": point.get("high"),
                    "low_price": point.get("low"),
                    "close_price": point["close"],
                    "benchmark_close": benchmark_map.get(point["date"]),
                },
            )

        position.current_price_per_share = position_series.latest_price
        position.latest_price_date = position_series.latest_date
        position.last_synced_at = django_timezone.now()
        if benchmark_series:
            position.benchmark_name = benchmark_series.name
            position.benchmark_symbol = benchmark_series.symbol
        position.save(
            update_fields=[
                "current_price_per_share",
                "latest_price_date",
                "last_synced_at",
                "benchmark_symbol",
                "benchmark_name",
            ]
        )

    return position


def sync_all_equities_market_data(positions) -> list[tuple[EquityPosition, str | None]]:
    results = []
    for position in positions:
        try:
            sync_equity_market_data(position)
            results.append((position, None))
        except Exception as exc:
            results.append((position, str(exc)))
    return results


def build_svg_polyline(values, width: int = 640, height: int = 220, padding: int = 18) -> str:
    filtered = [value for value in values if value is not None]
    if len(filtered) < 2:
        return ""

    min_value = min(filtered)
    max_value = max(filtered)
    if max_value == min_value:
        max_value += 1

    span_x = width - 2 * padding
    span_y = height - 2 * padding
    points = []
    total_points = len(values) - 1 or 1
    for index, value in enumerate(values):
        if value is None:
            continue
        x = padding + (span_x * index / total_points)
        normalized = (value - min_value) / (max_value - min_value)
        y = height - padding - (normalized * span_y)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def format_axis_value(value: Decimal | None) -> str:
    if value is None:
        return "-"
    absolute = abs(value)
    if absolute >= Decimal("1000000"):
        return f"{(value / Decimal('1000000')):.1f}M"
    if absolute >= Decimal("1000"):
        return f"{(value / Decimal('1000')):.1f}k"
    if absolute >= Decimal("100"):
        return f"{value:.0f}"
    if absolute >= Decimal("10"):
        return f"{value:.1f}"
    return f"{value:.2f}"


def build_dual_axis_chart(
    stock_points,
    reference_points,
    projection_points=None,
    width: int = 640,
    height: int = 220,
    padding: int = 18,
) -> dict:
    projection_points = projection_points or []

    def normalize_points(points):
        normalized = []
        for point in points:
            raw_date = point.get("date") if isinstance(point, dict) else None
            raw_value = point.get("value") if isinstance(point, dict) else None
            if raw_date is None or raw_value is None:
                continue
            normalized.append((raw_date, Decimal(str(raw_value))))
        return normalized

    stock_filtered = normalize_points(stock_points)
    reference_filtered = normalize_points(reference_points)
    projection_filtered = normalize_points(projection_points)
    if len(stock_filtered) < 2:
        return {
            "stock_line": "",
            "reference_line": "",
            "projection_line": "",
            "stock_min_label": "-",
            "stock_max_label": "-",
            "reference_min_label": "-",
            "reference_max_label": "-",
        }

    all_stock_values = [value for _, value in stock_filtered]
    if projection_filtered:
        all_stock_values.extend(value for _, value in projection_filtered)

    stock_min = min(all_stock_values)
    stock_max = max(all_stock_values)
    if stock_min == stock_max:
        stock_max += Decimal("1")

    if reference_filtered:
        reference_values_only = [value for _, value in reference_filtered]
        reference_min = min(reference_values_only)
        reference_max = max(reference_values_only)
        if reference_min == reference_max:
            reference_max += Decimal("1")
    else:
        reference_min = reference_max = None

    all_dates = [point_date for point_date, _ in stock_filtered]
    all_dates.extend(point_date for point_date, _ in reference_filtered)
    all_dates.extend(point_date for point_date, _ in projection_filtered)
    min_date = min(all_dates)
    max_date = max(all_dates)
    total_days = max((max_date - min_date).days, 1)

    def scale_series(points, series_min: Decimal | None, series_max: Decimal | None) -> str:
        if series_min is None or series_max is None:
            return ""
        span_x = width - 2 * padding
        span_y = height - 2 * padding
        line_points = []
        for point_date, value in points:
            x = padding + (span_x * ((point_date - min_date).days / total_days))
            normalized = (value - series_min) / (series_max - series_min)
            y = height - padding - (normalized * span_y)
            line_points.append(f"{x:.1f},{y:.1f}")
        return " ".join(line_points)

    return {
        "stock_line": scale_series(stock_filtered, stock_min, stock_max),
        "reference_line": scale_series(reference_filtered, reference_min, reference_max),
        "projection_line": scale_series(projection_filtered, stock_min, stock_max),
        "stock_min_label": format_axis_value(stock_min),
        "stock_max_label": format_axis_value(stock_max),
        "reference_min_label": format_axis_value(reference_min),
        "reference_max_label": format_axis_value(reference_max),
    }


def build_stock_history_chart(history) -> dict:
    if len(history) < 2:
        return {"available": False}

    stock_series = [{"date": point.price_date, "value": point.close_price} for point in history]
    chart = build_dual_axis_chart(stock_series, [])
    return {
        "available": bool(chart.get("stock_line")),
        "stock_line": chart.get("stock_line", ""),
        "stock_min_label": chart.get("stock_min_label", "-"),
        "stock_max_label": chart.get("stock_max_label", "-"),
        "start_label": history[0].price_date.isoformat(),
        "end_label": history[-1].price_date.isoformat(),
        "points_count": len(stock_series),
    }


def build_best_correlation_chart(history, suggestions: list[dict], reference_cache: dict) -> dict:
    best_suggestion = next(
        (
            suggestion
            for suggestion in suggestions
            if suggestion.get("correlation", {}).get("coefficient") is not None
        ),
        None,
    )
    if best_suggestion is None or len(history) < 2:
        return {"available": False}

    cache_key = (
        best_suggestion["reference_profile"],
        best_suggestion["benchmark_symbol"],
        best_suggestion["benchmark_name"],
    )
    cached_value = reference_cache.get(cache_key)
    if cached_value is None:
        try:
            cached_value = fetch_reference_series_for_choice(
                best_suggestion["reference_profile"],
                benchmark_symbol=best_suggestion["benchmark_symbol"],
                benchmark_name=best_suggestion["benchmark_name"],
            )
            reference_cache[cache_key] = cached_value
        except Exception as exc:
            reference_cache[cache_key] = exc
            cached_value = exc
    if isinstance(cached_value, Exception):
        return {"available": False}

    stock_series = [{"date": point.price_date, "value": point.close_price} for point in history]
    aligned_reference = align_reference_points(
        [{"date": point.price_date} for point in history],
        cached_value.points,
    )
    reference_series = [
        {"date": point.price_date, "value": aligned_reference.get(point.price_date)}
        for point in history
        if aligned_reference.get(point.price_date) is not None
    ]
    chart = build_dual_axis_chart(stock_series, reference_series)
    coefficient = best_suggestion["correlation"].get("coefficient")
    return {
        "available": bool(chart.get("stock_line") and chart.get("reference_line")),
        "stock_line": chart.get("stock_line", ""),
        "reference_line": chart.get("reference_line", ""),
        "stock_min_label": chart.get("stock_min_label", "-"),
        "stock_max_label": chart.get("stock_max_label", "-"),
        "reference_min_label": chart.get("reference_min_label", "-"),
        "reference_max_label": chart.get("reference_max_label", "-"),
        "reference_label": best_suggestion["benchmark_name"],
        "coefficient": coefficient,
        "start_label": history[0].price_date.isoformat(),
        "end_label": history[-1].price_date.isoformat(),
        "points_count": len(stock_series),
        "is_selected_reference": best_suggestion.get("is_selected", False),
    }


def build_projection_12m_chart(history, projection: dict) -> dict:
    if len(history) < 2 or not projection.get("available"):
        return {"available": False}

    latest_date = history[-1].price_date
    recent_history = filter_history_window(
        history,
        start_date=latest_date - timedelta(days=365 * 2),
        end_date=latest_date,
    )
    if len(recent_history) < 2:
        recent_history = history
    stock_series = [{"date": point.price_date, "value": point.close_price} for point in recent_history]
    projection_series = [{"date": latest_date, "value": recent_history[-1].close_price}]
    projection_series.extend(
        {
            "date": step["projected_date"],
            "value": step["projected_price"],
        }
        for step in projection.get("quarterly_path", [])
        if step.get("projected_date") and step.get("projected_price") is not None
    )
    chart = build_dual_axis_chart(stock_series, [], projection_points=projection_series)
    end_projection_step = projection.get("quarterly_path", [])[-1] if projection.get("quarterly_path") else None
    return {
        "available": bool(chart.get("stock_line") and chart.get("projection_line")),
        "stock_line": chart.get("stock_line", ""),
        "projection_line": chart.get("projection_line", ""),
        "stock_min_label": chart.get("stock_min_label", "-"),
        "stock_max_label": chart.get("stock_max_label", "-"),
        "start_label": recent_history[0].price_date.isoformat(),
        "end_label": latest_date.isoformat(),
        "projection_end_label": end_projection_step.get("projected_date").isoformat() if end_projection_step and end_projection_step.get("projected_date") else "",
        "points_count": len(stock_series),
    }


def build_ticket_snapshot_projection_values(card: dict) -> dict:
    position = card["position"]
    projection = card.get("projection", {})
    projected_price = projection.get("projected_price")
    projected_market_value_12m = None
    projected_total_value_12m = None
    if projected_price is not None:
        projected_market_value_12m = quantize_decimal(position.shares * projected_price, "0.01")
    if projection.get("base_return_pct") is not None:
        projected_total_value_12m = quantize_decimal(
            position.current_value * (Decimal("1") + (projection["base_return_pct"] / ONE_HUNDRED)),
            "0.01",
        )
    if projected_total_value_12m is None:
        projected_total_value_12m = projected_market_value_12m
    return {
        "projected_price_12m": projected_price,
        "projected_market_value_12m": projected_market_value_12m,
        "projected_total_value_12m": projected_total_value_12m,
    }


def capture_equity_ticket_snapshots(history_cards: list[dict], snapshot_date: date | None = None) -> list[EquityTicketSnapshot]:
    snapshot_date = snapshot_date or django_timezone.localdate()
    snapshots = []
    owned_cards = [card for card in history_cards if card["position"].is_owned]
    with transaction.atomic():
        for card in owned_cards:
            position = card["position"]
            projection_values = build_ticket_snapshot_projection_values(card)
            snapshot, _ = EquityTicketSnapshot.objects.update_or_create(
                position=position,
                snapshot_date=snapshot_date,
                defaults={
                    "invested_amount": quantize_decimal(position.invested_amount, "0.01") or ZERO,
                    "current_value": quantize_decimal(position.current_value, "0.01") or ZERO,
                    "projected_market_value_12m": projection_values["projected_market_value_12m"],
                    "projected_total_value_12m": projection_values["projected_total_value_12m"],
                    "projected_price_12m": projection_values["projected_price_12m"],
                },
            )
            snapshots.append(snapshot)
    return snapshots


def project_expected_value_on_date(
    start_value: Decimal | None,
    target_value: Decimal | None,
    start_date: date | None,
    point_date: date | None,
    horizon_days: int = TRACKING_HORIZON_DAYS,
) -> Decimal | None:
    if start_value is None or target_value is None or start_date is None or point_date is None:
        return None
    if point_date <= start_date:
        return quantize_decimal(start_value, "0.01")

    elapsed_days = min(max((point_date - start_date).days, 0), horizon_days)
    ratio = Decimal(str(elapsed_days / horizon_days))
    if start_value > ZERO and target_value > ZERO:
        multiplier = float(target_value / start_value)
        projected_multiplier = Decimal(str(multiplier ** float(ratio)))
        return quantize_decimal(start_value * projected_multiplier, "0.01")
    return quantize_decimal(start_value + ((target_value - start_value) * ratio), "0.01")


def build_value_tracking_chart(
    actual_series: list[dict],
    expected_series: list[dict],
    width: int = 640,
    height: int = 220,
    padding: int = 18,
) -> dict:
    def normalize_series(points: list[dict]) -> list[dict]:
        grouped = {}
        for point in points:
            point_date = point.get("date")
            value = point.get("value")
            if point_date is None or value is None:
                continue
            grouped[point_date] = Decimal(str(value))
        return [{"date": point_date, "value": grouped[point_date]} for point_date in sorted(grouped)]

    actual_points = normalize_series(actual_series)
    expected_points = normalize_series(expected_series)
    if not actual_points or not expected_points:
        return {
            "available": False,
            "actual_line": "",
            "expected_line": "",
            "actual_points": [],
            "expected_points": [],
            "min_label": "-",
            "max_label": "-",
            "start_label": "",
            "latest_label": "",
            "projection_end_label": "",
            "points_count": 0,
        }

    all_values = [point["value"] for point in actual_points] + [point["value"] for point in expected_points]
    series_min = min(all_values)
    series_max = max(all_values)
    if series_min == series_max:
        series_max += Decimal("1")

    min_date = min(actual_points[0]["date"], expected_points[0]["date"])
    max_date = max(actual_points[-1]["date"], expected_points[-1]["date"])
    total_days = max((max_date - min_date).days, 1)
    span_x = width - (padding * 2)
    span_y = height - (padding * 2)

    def scale_point(point_date: date, value: Decimal) -> tuple[float, float]:
        x = padding + (span_x * ((point_date - min_date).days / total_days))
        normalized_value = (value - series_min) / (series_max - series_min)
        y = height - padding - (normalized_value * span_y)
        return x, y

    def build_series(points: list[dict], prefix: str) -> tuple[str, list[dict]]:
        line_points = []
        point_rows = []
        for point in points:
            x, y = scale_point(point["date"], point["value"])
            line_points.append(f"{x:.1f},{y:.1f}")
            point_rows.append(
                {
                    "x": f"{x:.1f}",
                    "y": f"{y:.1f}",
                    "value_label": f"{format_axis_value(point['value'])} EUR",
                    "date_label": point["date"].isoformat(),
                    "key": f"{prefix}-{point['date'].isoformat()}",
                }
            )
        return " ".join(line_points) if len(line_points) >= 2 else "", point_rows

    actual_line, actual_point_rows = build_series(actual_points, "actual")
    expected_line, expected_point_rows = build_series(expected_points, "expected")
    return {
        "available": True,
        "actual_line": actual_line,
        "expected_line": expected_line,
        "actual_points": actual_point_rows,
        "expected_points": expected_point_rows,
        "min_label": f"{format_axis_value(series_min)} EUR",
        "max_label": f"{format_axis_value(series_max)} EUR",
        "start_label": min_date.isoformat(),
        "latest_label": actual_points[-1]["date"].isoformat(),
        "projection_end_label": expected_points[-1]["date"].isoformat(),
        "points_count": len(actual_points),
    }


def build_ticket_expected_series(
    snapshots: list[EquityTicketSnapshot],
    target_value: Decimal | None,
) -> tuple[list[dict], Decimal | None, date | None]:
    if not snapshots:
        return [], None, None
    baseline = snapshots[0]
    projected_end_date = baseline.snapshot_date + timedelta(days=TRACKING_HORIZON_DAYS)
    series_dates = {baseline.snapshot_date, projected_end_date}
    for days in TRACKING_FORECAST_MARKERS:
        series_dates.add(baseline.snapshot_date + timedelta(days=days))
    for snapshot in snapshots:
        series_dates.add(snapshot.snapshot_date)

    series = []
    for point_date in sorted(series_dates):
        expected_value = project_expected_value_on_date(
            baseline.current_value,
            target_value or baseline.current_value,
            baseline.snapshot_date,
            point_date,
        )
        if expected_value is not None:
            series.append({"date": point_date, "value": expected_value})

    latest_snapshot_date = snapshots[-1].snapshot_date
    latest_expected_value = next(
        (point["value"] for point in reversed(series) if point["date"] <= latest_snapshot_date),
        None,
    )
    return series, latest_expected_value, projected_end_date


def build_equity_ticket_tracking_item(
    card: dict,
    snapshots: list[EquityTicketSnapshot],
) -> dict | None:
    if not snapshots:
        return None

    position = card["position"]
    baseline = snapshots[0]
    latest = snapshots[-1]
    previous = snapshots[-2] if len(snapshots) >= 2 else None
    expected_market_value_12m = baseline.projected_market_value_12m or baseline.current_value
    expected_total_value_12m = baseline.projected_total_value_12m or expected_market_value_12m
    actual_series = [{"date": snapshot.snapshot_date, "value": snapshot.current_value} for snapshot in snapshots]
    expected_series, current_expected_value, projected_end_date = build_ticket_expected_series(
        snapshots,
        expected_market_value_12m,
    )
    chart = build_value_tracking_chart(actual_series, expected_series)
    gap_value = (
        quantize_decimal(latest.current_value - current_expected_value, "0.01")
        if current_expected_value is not None
        else None
    )
    gap_pct = percentage_change(latest.current_value, current_expected_value) if current_expected_value is not None else None
    daily_change_pct = (
        percentage_change(latest.current_value, previous.current_value)
        if previous and previous.current_value
        else None
    )
    return {
        "position": position,
        "card": card,
        "baseline_snapshot": baseline,
        "latest_snapshot": latest,
        "snapshot_count": len(snapshots),
        "days_tracked": max((latest.snapshot_date - baseline.snapshot_date).days, 0),
        "actual_series": actual_series,
        "expected_series": expected_series,
        "chart": chart,
        "current_expected_value": current_expected_value,
        "expected_market_value_12m": expected_market_value_12m,
        "expected_total_value_12m": expected_total_value_12m,
        "actual_change_pct": percentage_change(latest.current_value, baseline.current_value),
        "expected_change_pct": percentage_change(current_expected_value, baseline.current_value)
        if current_expected_value is not None
        else None,
        "gap_value": gap_value,
        "gap_pct": gap_pct,
        "gap_tone": "good" if gap_value is not None and gap_value >= ZERO else "warn",
        "daily_change_pct": daily_change_pct,
        "projection_end_date": projected_end_date,
    }


def build_global_equity_ticket_tracking_item(ticket_items: list[dict]) -> dict:
    actual_map: dict[date, Decimal] = defaultdict(lambda: ZERO)
    tracked_dates: set[date] = set()
    for item in ticket_items:
        for point in item["actual_series"]:
            actual_map[point["date"]] += point["value"]
            tracked_dates.add(point["date"])

    if not actual_map:
        return {"available": False}

    expected_dates: set[date] = set(tracked_dates)
    descriptors = []
    for item in ticket_items:
        baseline = item["baseline_snapshot"]
        expected_target = item["expected_market_value_12m"] or baseline.current_value
        descriptors.append(
            {
                "start_date": baseline.snapshot_date,
                "start_value": baseline.current_value,
                "target_value": expected_target,
            }
        )
        expected_dates.add(baseline.snapshot_date + timedelta(days=TRACKING_HORIZON_DAYS))
        for days in TRACKING_FORECAST_MARKERS:
            expected_dates.add(baseline.snapshot_date + timedelta(days=days))

    actual_series = [
        {"date": point_date, "value": quantize_decimal(actual_map[point_date], "0.01") or ZERO}
        for point_date in sorted(actual_map)
    ]
    expected_series = []
    for point_date in sorted(expected_dates):
        expected_total = ZERO
        for descriptor in descriptors:
            if point_date < descriptor["start_date"]:
                continue
            expected_value = project_expected_value_on_date(
                descriptor["start_value"],
                descriptor["target_value"],
                descriptor["start_date"],
                point_date,
            )
            if expected_value is not None:
                expected_total += expected_value
        expected_series.append(
            {
                "date": point_date,
                "value": quantize_decimal(expected_total, "0.01") or ZERO,
            }
        )

    chart = build_value_tracking_chart(actual_series, expected_series)
    baseline_value = actual_series[0]["value"]
    latest_actual = actual_series[-1]["value"]
    latest_date = actual_series[-1]["date"]
    current_expected_value = next(
        (point["value"] for point in reversed(expected_series) if point["date"] <= latest_date),
        None,
    )
    expected_total_value_12m = sum(
        (item["expected_total_value_12m"] or item["expected_market_value_12m"] or ZERO)
        for item in ticket_items
    )
    previous_actual = actual_series[-2]["value"] if len(actual_series) >= 2 else None
    gap_value = (
        quantize_decimal(latest_actual - current_expected_value, "0.01")
        if current_expected_value is not None
        else None
    )
    return {
        "available": True,
        "chart": chart,
        "baseline_value": baseline_value,
        "latest_value": latest_actual,
        "expected_today_value": current_expected_value,
        "expected_market_value_12m": expected_series[-1]["value"] if expected_series else baseline_value,
        "expected_total_value_12m": quantize_decimal(expected_total_value_12m, "0.01") or ZERO,
        "actual_change_pct": percentage_change(latest_actual, baseline_value),
        "expected_change_pct": percentage_change(current_expected_value, baseline_value)
        if current_expected_value is not None
        else None,
        "gap_value": gap_value,
        "gap_pct": percentage_change(latest_actual, current_expected_value) if current_expected_value is not None else None,
        "gap_tone": "good" if gap_value is not None and gap_value >= ZERO else "warn",
        "daily_change_pct": percentage_change(latest_actual, previous_actual) if previous_actual else None,
        "tracked_days": max((latest_date - actual_series[0]["date"]).days, 0),
    }


def build_equity_ticket_tracking_context(history_cards: list[dict]) -> dict:
    owned_cards = [card for card in history_cards if card["position"].is_owned]
    if not owned_cards:
        return {
            "available": False,
            "tickets": [],
            "tracked_ticket_count": 0,
            "snapshot_days_count": 0,
            "global": {"available": False},
        }

    cards_by_id = {card["position"].id: card for card in owned_cards if card["position"].id}
    snapshots = list(
        EquityTicketSnapshot.objects.filter(position_id__in=cards_by_id.keys())
        .select_related("position")
        .order_by("snapshot_date", "position__company_name", "position__ticker")
    )
    grouped_snapshots: dict[int, list[EquityTicketSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped_snapshots[snapshot.position_id].append(snapshot)

    ticket_items = []
    for card in owned_cards:
        position_id = card["position"].id
        if not position_id:
            continue
        item = build_equity_ticket_tracking_item(card, grouped_snapshots.get(position_id, []))
        if item:
            ticket_items.append(item)

    ticket_items.sort(
        key=lambda item: (
            -(item["latest_snapshot"].current_value if item.get("latest_snapshot") else ZERO),
            item["position"].company_name,
        )
    )
    snapshot_days = sorted({snapshot.snapshot_date for snapshot in snapshots})
    return {
        "available": bool(ticket_items),
        "tickets": ticket_items,
        "tracked_ticket_count": len(ticket_items),
        "snapshot_days_count": len(snapshot_days),
        "global": build_global_equity_ticket_tracking_item(ticket_items) if ticket_items else {"available": False},
    }


def average_decimal(values: list[Decimal]) -> Decimal | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return sum(filtered, ZERO) / Decimal(len(filtered))


def percentage_change(current: Decimal | None, reference: Decimal | None) -> Decimal | None:
    if current is None or reference in {None, ZERO}:
        return None
    return ((current / reference) - Decimal("1")) * Decimal("100")


def infer_reference_frequency_from_profile(reference_profile: str) -> str:
    if reference_profile == EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE:
        return "quarterly"
    return "monthly"


def infer_reference_frequency(position: EquityPosition) -> str:
    return infer_reference_frequency_from_profile(position.reference_profile)


def bucket_label_for_date(value: date, frequency: str) -> str:
    if frequency == "yearly":
        return f"{value.year}"
    if frequency == "quarterly":
        quarter = ((value.month - 1) // 3) + 1
        return f"{value.year}-Q{quarter}"
    return value.strftime("%Y-%m")


def collapse_history_to_frequency(history, frequency: str) -> list:
    buckets = {}
    for point in history:
        buckets[bucket_label_for_date(point.price_date, frequency)] = point
    return [buckets[label] for label in sorted(buckets.keys())]


def build_return_series_from_collapsed_rows(
    collapsed_rows: list[dict],
    primary_key: str,
    secondary_key: str,
) -> tuple[list[Decimal], list[Decimal]]:
    primary_returns = []
    secondary_returns = []
    for previous, current in zip(collapsed_rows, collapsed_rows[1:]):
        primary_return = percentage_change(current.get(primary_key), previous.get(primary_key))
        secondary_return = percentage_change(current.get(secondary_key), previous.get(secondary_key))
        if primary_return is None or secondary_return is None:
            continue
        primary_returns.append(primary_return)
        secondary_returns.append(secondary_return)
    return primary_returns, secondary_returns


def correlation_window_size(frequency: str) -> int:
    if frequency == "quarterly":
        return QUARTERLY_CORRELATION_WINDOW
    return MONTHLY_CORRELATION_WINDOW


def recent_correlation_window_size(frequency: str) -> int:
    if frequency == "quarterly":
        return QUARTERLY_RECENT_WINDOW
    return MONTHLY_RECENT_WINDOW


def calculate_years_between(start_date: date | None, end_date: date | None) -> Decimal:
    if not start_date or not end_date or end_date <= start_date:
        return ZERO
    return Decimal(str(round((end_date - start_date).days / 365.25, 2)))


def calculate_series_cagr_pct(
    start_value: Decimal | None,
    end_value: Decimal | None,
    years_covered: Decimal | None,
) -> Decimal | None:
    if start_value in {None, ZERO} or end_value is None or years_covered in {None, ZERO}:
        return None
    base = float(end_value / start_value)
    years_float = float(years_covered)
    if base <= 0 or years_float <= 0:
        return None
    annualized = (base ** (1 / years_float) - 1) * 100
    return Decimal(str(round(annualized, 4)))


def calculate_max_drawdown_pct(values: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    filtered = [value for value in values if value is not None]
    if len(filtered) < 2:
        return None, None

    peak = filtered[0]
    max_drawdown_pct = ZERO
    for value in filtered:
        if value > peak:
            peak = value
        drawdown_pct = percentage_change(value, peak)
        if drawdown_pct is not None and drawdown_pct < max_drawdown_pct:
            max_drawdown_pct = drawdown_pct

    current_peak = max(filtered)
    current_drawdown_pct = percentage_change(filtered[-1], current_peak)
    return max_drawdown_pct, current_drawdown_pct


def describe_cycle_phase(
    current_drawdown_pct: Decimal | None,
    one_year_return_pct: Decimal | None,
    cagr_pct: Decimal | None,
) -> str:
    if current_drawdown_pct is None:
        return "Sin ciclo"
    one_year_return_pct = one_year_return_pct or ZERO
    cagr_pct = cagr_pct or ZERO
    if current_drawdown_pct <= Decimal("-20.00") and one_year_return_pct < 0:
        return "Correccion"
    if current_drawdown_pct <= Decimal("-8.00") and one_year_return_pct >= 0:
        return "Recuperacion"
    if current_drawdown_pct >= Decimal("-6.00") and one_year_return_pct >= 0 and cagr_pct >= 0:
        return "Expansion"
    return "Transicion"


def build_cycle_metrics(history) -> dict:
    if not history:
        return {
            "available": False,
            "years_covered": ZERO,
            "cycle_phase": "Sin ciclo",
        }

    latest_date = history[-1].price_date
    window = filter_history_window(
        history,
        start_date=latest_date - timedelta(days=LONG_ANALYSIS_DAYS),
        end_date=latest_date,
    )
    monthly_history = collapse_history_to_frequency(window, "monthly")
    if len(monthly_history) < 6:
        return {
            "available": False,
            "years_covered": calculate_years_between(window[0].price_date, window[-1].price_date) if len(window) >= 2 else ZERO,
            "cycle_phase": "Sin ciclo",
        }

    monthly_returns = []
    for previous, current in zip(monthly_history, monthly_history[1:]):
        period_return = percentage_change(current.close_price, previous.close_price)
        if period_return is not None:
            monthly_returns.append(period_return)

    monthly_volatility_pct = standard_deviation_decimal(monthly_returns)
    annualized_volatility_pct = (
        monthly_volatility_pct * Decimal(str(round(math.sqrt(12), 4)))
        if monthly_volatility_pct is not None
        else None
    )
    years_covered = calculate_years_between(monthly_history[0].price_date, monthly_history[-1].price_date)
    cagr_pct = calculate_series_cagr_pct(
        monthly_history[0].close_price,
        monthly_history[-1].close_price,
        years_covered,
    )
    max_drawdown_pct, current_drawdown_pct = calculate_max_drawdown_pct(
        [point.close_price for point in monthly_history]
    )
    one_year_snapshot = build_period_snapshot(
        monthly_history,
        "1Y",
        start_date=latest_date - timedelta(days=365),
        end_date=latest_date,
    )
    three_year_snapshot = build_period_snapshot(
        monthly_history,
        "3Y",
        start_date=latest_date - timedelta(days=365 * 3),
        end_date=latest_date,
    )
    yearly_history = collapse_history_to_frequency(window, "yearly")
    yearly_returns = []
    for previous, current in zip(yearly_history, yearly_history[1:]):
        yearly_return = percentage_change(current.close_price, previous.close_price)
        if yearly_return is not None:
            yearly_returns.append(yearly_return)
    positive_year_ratio_pct = None
    if yearly_returns:
        positive_year_ratio_pct = Decimal(sum(1 for value in yearly_returns if value > 0)) * ONE_HUNDRED / Decimal(
            len(yearly_returns)
        )

    return {
        "available": True,
        "start_date": monthly_history[0].price_date,
        "end_date": monthly_history[-1].price_date,
        "years_covered": years_covered,
        "monthly_observations_count": len(monthly_history),
        "yearly_observations_count": len(yearly_returns),
        "monthly_volatility_pct": monthly_volatility_pct,
        "annualized_volatility_pct": annualized_volatility_pct,
        "cagr_pct": cagr_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "current_drawdown_pct": current_drawdown_pct,
        "cycle_total_return_pct": percentage_change(monthly_history[-1].close_price, monthly_history[0].close_price),
        "one_year_return_pct": one_year_snapshot.get("stock_return_pct") if one_year_snapshot.get("available") else None,
        "three_year_return_pct": three_year_snapshot.get("stock_return_pct") if three_year_snapshot.get("available") else None,
        "positive_year_ratio_pct": positive_year_ratio_pct,
        "cycle_phase": describe_cycle_phase(
            current_drawdown_pct,
            one_year_snapshot.get("stock_return_pct") if one_year_snapshot.get("available") else None,
            cagr_pct,
        ),
    }


def pearson_correlation(xs: list[Decimal], ys: list[Decimal]) -> Decimal | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None

    x_values = [float(value) for value in xs]
    y_values = [float(value) for value in ys]
    mean_x = sum(x_values) / len(x_values)
    mean_y = sum(y_values) / len(y_values)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_values, y_values))
    variance_x = sum((x - mean_x) ** 2 for x in x_values)
    variance_y = sum((y - mean_y) ** 2 for y in y_values)
    if variance_x <= 0 or variance_y <= 0:
        return None
    correlation = covariance / math.sqrt(variance_x * variance_y)
    return Decimal(str(round(correlation, 4)))


def describe_correlation(coefficient: Decimal | None) -> str:
    if coefficient is None:
        return "Sin correlacion suficiente"
    if coefficient >= Decimal("0.70"):
        return "Relacion positiva alta"
    if coefficient >= Decimal("0.35"):
        return "Relacion positiva moderada"
    if coefficient <= Decimal("-0.35"):
        return "Relacion inversa"
    return "Relacion debil"


def build_correlation_from_rows(rows: list[dict], frequency: str) -> dict:
    buckets = {}
    for row in rows:
        buckets[bucket_label_for_date(row["date"], frequency)] = row
    collapsed = [buckets[label] for label in sorted(buckets.keys())]

    stock_returns, reference_returns = build_return_series_from_collapsed_rows(
        collapsed,
        "stock_close",
        "reference_close",
    )
    window_size = correlation_window_size(frequency)
    recent_window = recent_correlation_window_size(frequency)
    coefficient = pearson_correlation(stock_returns[-window_size:], reference_returns[-window_size:])
    recent_coefficient = pearson_correlation(stock_returns[-recent_window:], reference_returns[-recent_window:])
    stability_gap = abs(coefficient - recent_coefficient) if coefficient is not None and recent_coefficient is not None else None
    if stability_gap is None or stability_gap <= Decimal("0.15"):
        stability_label = "Estable"
    elif stability_gap <= Decimal("0.35"):
        stability_label = "Aceptable"
    else:
        stability_label = "Cambiante"
    return {
        "frequency": frequency,
        "coefficient": coefficient,
        "recent_coefficient": recent_coefficient,
        "label": describe_correlation(coefficient),
        "observations_count": len(stock_returns),
        "years_covered": calculate_years_between(
            collapsed[0]["date"] if collapsed else None,
            collapsed[-1]["date"] if collapsed else None,
        ),
        "window_observations": min(len(stock_returns), window_size),
        "stability_gap": stability_gap,
        "stability_label": stability_label,
    }


def build_reference_correlation(history, position: EquityPosition) -> dict:
    frequency = infer_reference_frequency(position)
    rows = [
        {
            "date": point.price_date,
            "stock_close": point.close_price,
            "reference_close": point.benchmark_close,
        }
        for point in history
    ]
    return build_correlation_from_rows(rows, frequency)


def build_reference_correlation_for_series(history, reference_series: MarketSeries, reference_profile: str) -> dict:
    aligned = align_reference_points(
        [{"date": point.price_date} for point in history],
        reference_series.points,
    )
    rows = [
        {
            "date": point.price_date,
            "stock_close": point.close_price,
            "reference_close": aligned.get(point.price_date),
        }
        for point in history
    ]
    return build_correlation_from_rows(rows, infer_reference_frequency_from_profile(reference_profile))


def filter_history_window(history, start_date: date | None = None, end_date: date | None = None):
    return [
        point
        for point in history
        if (start_date is None or point.price_date >= start_date) and (end_date is None or point.price_date <= end_date)
    ]


def build_period_snapshot(history, label: str, start_date: date | None = None, end_date: date | None = None) -> dict:
    window = filter_history_window(history, start_date=start_date, end_date=end_date)
    if len(window) < 2:
        return {
            "label": label,
            "available": False,
            "stock_return_pct": None,
            "benchmark_return_pct": None,
            "alpha_pct": None,
            "start_date": window[0].price_date if window else start_date,
            "end_date": window[-1].price_date if window else end_date,
        }

    first_point = window[0]
    last_point = window[-1]
    stock_return_pct = percentage_change(last_point.close_price, first_point.close_price)
    benchmark_return_pct = percentage_change(last_point.benchmark_close, first_point.benchmark_close)
    alpha_pct = (
        stock_return_pct - benchmark_return_pct
        if stock_return_pct is not None and benchmark_return_pct is not None
        else None
    )

    return {
        "label": label,
        "available": True,
        "stock_return_pct": stock_return_pct,
        "benchmark_return_pct": benchmark_return_pct,
        "alpha_pct": alpha_pct,
        "start_date": first_point.price_date,
        "end_date": last_point.price_date,
    }


def quantize_decimal(value: Decimal | None, places: str = "0.01") -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(places))


def clamp_decimal(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(upper, value))


def annualize_return_pct(return_pct: Decimal | None, months: int) -> Decimal | None:
    if return_pct is None or months <= 0:
        return None
    base = 1 + (float(return_pct) / 100)
    if base <= 0:
        return Decimal("-100.00")
    annualized = (base ** (12 / months) - 1) * 100
    return Decimal(str(round(annualized, 4)))


def build_projection_signal(return_pct: Decimal | None, months: int) -> Decimal | None:
    if return_pct is None:
        return None
    annualized = annualize_return_pct(return_pct, months)
    if annualized is None:
        return None
    linearized = return_pct * (Decimal("12") / Decimal(months))
    return (annualized * Decimal("0.55")) + (linearized * Decimal("0.45"))


def standard_deviation_decimal(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    numeric_values = [float(value) for value in values]
    mean_value = sum(numeric_values) / len(numeric_values)
    variance = sum((value - mean_value) ** 2 for value in numeric_values) / (len(numeric_values) - 1)
    return Decimal(str(round(math.sqrt(variance), 4)))


def build_recent_monthly_stock_returns(history, months_back: int = 6) -> list[Decimal]:
    if not history:
        return []
    latest_date = history[-1].price_date
    recent_history = filter_history_window(
        history,
        start_date=latest_date - timedelta(days=35 * (months_back + 1)),
        end_date=latest_date,
    )
    monthly_history = collapse_history_to_frequency(recent_history, "monthly")[-(months_back + 1):]
    monthly_returns = []
    for previous, current in zip(monthly_history, monthly_history[1:]):
        period_return = percentage_change(current.close_price, previous.close_price)
        if period_return is not None:
            monthly_returns.append(period_return)
    return monthly_returns


def decimal_linear_slope(values: list[Decimal]) -> Decimal | None:
    filtered = [value for value in values if value is not None]
    if len(filtered) < 2:
        return None

    xs = list(range(len(filtered)))
    mean_x = sum(xs) / len(xs)
    mean_y = sum(float(value) for value in filtered) / len(filtered)
    numerator = sum((x - mean_x) * (float(value) - mean_y) for x, value in zip(xs, filtered))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return None
    return Decimal(str(round(numerator / denominator, 4)))


def consecutive_tail_count(values: list[Decimal], positive: bool) -> int:
    streak = 0
    for value in reversed(values):
        if value is None:
            continue
        if positive and value > ZERO:
            streak += 1
            continue
        if not positive and value < ZERO:
            streak += 1
            continue
        break
    return streak


def build_relative_strength_trend(
    history,
    position: EquityPosition,
    coefficient: Decimal | None,
    periods_back: int = 8,
) -> dict:
    if not history:
        return {
            "available": False,
            "label": "Sin tendencia relativa",
            "periods_label": "periodos",
        }

    frequency = infer_reference_frequency(position)
    periods_label = "meses" if frequency == "monthly" else "trimestres"
    buffer_days = 35 if frequency == "monthly" else 95
    latest_date = history[-1].price_date
    recent_history = filter_history_window(
        history,
        start_date=latest_date - timedelta(days=buffer_days * (periods_back + 2)),
        end_date=latest_date,
    )
    collapsed_history = collapse_history_to_frequency(recent_history, frequency)[-(periods_back + 1) :]

    rows = []
    for previous, current in zip(collapsed_history, collapsed_history[1:]):
        stock_return_pct = percentage_change(current.close_price, previous.close_price)
        reference_return_pct = percentage_change(current.benchmark_close, previous.benchmark_close)
        if stock_return_pct is None or reference_return_pct is None:
            continue
        expected_return_pct = reference_return_pct * coefficient if coefficient is not None else reference_return_pct
        relative_gap_pct = stock_return_pct - expected_return_pct
        rows.append(
            {
                "start_date": previous.price_date,
                "end_date": current.price_date,
                "stock_return_pct": stock_return_pct,
                "reference_return_pct": reference_return_pct,
                "expected_return_pct": expected_return_pct,
                "relative_gap_pct": relative_gap_pct,
            }
        )

    if len(rows) < 3:
        return {
            "available": False,
            "label": "Sin tendencia relativa",
            "periods_label": periods_label,
            "rows": rows,
        }

    gap_values = [row["relative_gap_pct"] for row in rows]
    positive_streak = consecutive_tail_count(gap_values, positive=True)
    negative_streak = consecutive_tail_count(gap_values, positive=False)
    recent_gap_avg_pct = average_decimal(gap_values[-3:])
    overall_gap_avg_pct = average_decimal(gap_values)
    gap_slope_pct = decimal_linear_slope(gap_values)
    stock_slope_pct = decimal_linear_slope([row["stock_return_pct"] for row in rows])
    threshold = 4 if len(gap_values) >= 4 else len(gap_values)
    prolonged_positive = (
        positive_streak >= threshold
        and (recent_gap_avg_pct or ZERO) > Decimal("0.35")
        and (gap_slope_pct or ZERO) >= ZERO
    )
    prolonged_negative = (
        negative_streak >= threshold
        and (recent_gap_avg_pct or ZERO) < Decimal("-0.35")
        and (gap_slope_pct or ZERO) <= ZERO
    )

    if prolonged_positive:
        label = "Pendiente positiva prolongada"
    elif prolonged_negative:
        label = "Pendiente negativa prolongada"
    elif (recent_gap_avg_pct or ZERO) > ZERO:
        label = "Mejora moderada"
    elif (recent_gap_avg_pct or ZERO) < ZERO:
        label = "Debilitamiento moderado"
    else:
        label = "Sin sesgo claro"

    return {
        "available": True,
        "label": label,
        "periods_label": periods_label,
        "rows": rows,
        "periods_count": len(rows),
        "positive_streak": positive_streak,
        "negative_streak": negative_streak,
        "recent_gap_avg_pct": recent_gap_avg_pct,
        "overall_gap_avg_pct": overall_gap_avg_pct,
        "gap_slope_pct": gap_slope_pct,
        "stock_slope_pct": stock_slope_pct,
        "prolonged_positive": prolonged_positive,
        "prolonged_negative": prolonged_negative,
    }


def build_projection_confidence(
    coefficient: Decimal | None,
    observations_count: int,
    monthly_returns_count: int,
    years_covered: Decimal = ZERO,
    positive_year_ratio_pct: Decimal | None = None,
    stability_gap: Decimal | None = None,
) -> dict:
    score = 0
    absolute_coefficient = abs(coefficient) if coefficient is not None else None
    if absolute_coefficient is not None:
        if absolute_coefficient >= Decimal("0.20"):
            score += 1
        if absolute_coefficient >= Decimal("0.45"):
            score += 1
    if observations_count >= 12:
        score += 1
    if observations_count >= 36:
        score += 1
    if observations_count >= 72:
        score += 1
    if monthly_returns_count >= 24:
        score += 1
    if years_covered >= Decimal("5.00"):
        score += 1
    if years_covered >= Decimal("8.00"):
        score += 1
    if positive_year_ratio_pct is not None and positive_year_ratio_pct >= Decimal("55.00"):
        score += 1
    if positive_year_ratio_pct is not None and positive_year_ratio_pct >= Decimal("70.00"):
        score += 1
    if stability_gap is not None and stability_gap <= Decimal("0.20"):
        score += 1

    if score >= 8:
        return {
            "label": "Alta",
            "note": "Hay historico largo, ciclo coherente y una relacion bastante estable con la referencia elegida.",
            "score_pct": Decimal("85.00"),
        }
    if score >= 5:
        return {
            "label": "Media",
            "note": "La lectura es util, aunque la fiabilidad sigue siendo orientativa y conviene vigilar la volatilidad.",
            "score_pct": Decimal("65.00"),
        }
    return {
        "label": "Baja",
        "note": "La proyeccion se apoya en una base limitada o en una relacion debil con la referencia, asi que hay que leerla con prudencia.",
        "score_pct": Decimal("40.00"),
    }


def build_safety_score(cycle_metrics: dict, correlation: dict) -> dict:
    score = Decimal("62.00")
    annualized_volatility_pct = cycle_metrics.get("annualized_volatility_pct")
    max_drawdown_pct = cycle_metrics.get("max_drawdown_pct")
    current_drawdown_pct = cycle_metrics.get("current_drawdown_pct")
    positive_year_ratio_pct = cycle_metrics.get("positive_year_ratio_pct")
    cagr_pct = cycle_metrics.get("cagr_pct")
    coefficient = correlation.get("coefficient")

    if annualized_volatility_pct is not None:
        score -= clamp_decimal(annualized_volatility_pct - Decimal("16.00"), ZERO, Decimal("26.00"))
    if max_drawdown_pct is not None:
        score -= clamp_decimal(abs(max_drawdown_pct) * Decimal("0.45"), ZERO, Decimal("18.00"))
    if current_drawdown_pct is not None and current_drawdown_pct >= Decimal("-10.00"):
        score += Decimal("6.00")
    elif current_drawdown_pct is not None and current_drawdown_pct <= Decimal("-25.00"):
        score -= Decimal("6.00")
    if positive_year_ratio_pct is not None:
        score += clamp_decimal((positive_year_ratio_pct - Decimal("50.00")) * Decimal("0.18"), Decimal("-8.00"), Decimal("10.00"))
    if cagr_pct is not None and cagr_pct >= Decimal("6.00"):
        score += Decimal("5.00")
    if coefficient is not None and abs(coefficient) >= Decimal("0.35"):
        score += Decimal("4.00")
    if correlation.get("stability_label") == "Estable":
        score += Decimal("4.00")
    score = clamp_decimal(score, Decimal("15.00"), Decimal("92.00"))

    if score >= Decimal("72.00"):
        label = "Alta"
    elif score >= Decimal("56.00"):
        label = "Media"
    else:
        label = "Baja"
    return {
        "score": score,
        "label": label,
    }


def project_price_from_return(current_price: Decimal | None, return_pct: Decimal | None) -> Decimal | None:
    if current_price in {None, ZERO} or return_pct is None:
        return None
    multiplier = Decimal("1") + (return_pct / Decimal("100"))
    if multiplier <= 0:
        multiplier = Decimal("0.01")
    return current_price * multiplier


def build_projection_path(current_price: Decimal | None, annual_return_pct: Decimal | None, anchor_date: date | None = None) -> list[dict]:
    if current_price in {None, ZERO} or annual_return_pct is None:
        return []
    annual_multiplier = max(0.01, 1 + (float(annual_return_pct) / 100))
    path = []
    for months, label, days in ((3, "3M", 91), (6, "6M", 182), (9, "9M", 273), (12, "12M", 365)):
        projected_multiplier = annual_multiplier ** (months / 12)
        projected_price = Decimal(str(round(float(current_price) * projected_multiplier, 4)))
        path.append(
            {
                "label": label,
                "projected_date": anchor_date + timedelta(days=days) if anchor_date else None,
                "projected_price": projected_price,
            }
        )
    return path


def build_backtest_monthly_chart(rows: list[dict], width: int = 640, height: int = 220, padding: int = 18, max_points: int = 60) -> dict:
    normalized_rows = []
    for row in rows[-max_points:]:
        forecast_date = row.get("forecast_date")
        target_date = row.get("target_date")
        forecast_return_pct = row.get("forecast_return_pct")
        actual_return_pct = row.get("actual_return_pct")
        if forecast_date is None or target_date is None or forecast_return_pct is None or actual_return_pct is None:
            continue
        normalized_rows.append(
            {
                "forecast_date": forecast_date,
                "target_date": target_date,
                "forecast_return_pct": Decimal(str(forecast_return_pct)),
                "actual_return_pct": Decimal(str(actual_return_pct)),
            }
        )

    if len(normalized_rows) < 2:
        return {
            "available": False,
            "forecast_line": "",
            "actual_line": "",
            "forecast_points": [],
            "actual_points": [],
            "min_label": "-",
            "max_label": "-",
            "start_label": "",
            "end_label": "",
            "points_count": len(normalized_rows),
        }

    all_values = []
    for row in normalized_rows:
        all_values.append(row["forecast_return_pct"])
        all_values.append(row["actual_return_pct"])
    series_min = min(all_values)
    series_max = max(all_values)
    if series_min == series_max:
        series_max += Decimal("1")

    min_date = normalized_rows[0]["forecast_date"]
    max_date = normalized_rows[-1]["forecast_date"]
    total_days = max((max_date - min_date).days, 1)
    span_x = width - (padding * 2)
    span_y = height - (padding * 2)

    def scale_point(point_date: date, value: Decimal) -> tuple[float, float]:
        x = padding + (span_x * ((point_date - min_date).days / total_days))
        normalized_value = (value - series_min) / (series_max - series_min)
        y = height - padding - (normalized_value * span_y)
        return x, y

    def build_series(series_key: str) -> tuple[str, list[dict]]:
        line_points = []
        point_rows = []
        for row in normalized_rows:
            value = row[series_key]
            x, y = scale_point(row["forecast_date"], value)
            line_points.append(f"{x:.1f},{y:.1f}")
            point_rows.append(
                {
                    "x": f"{x:.1f}",
                    "y": f"{y:.1f}",
                    "value_label": f"{value:.1f} %",
                    "forecast_date_label": row["forecast_date"].isoformat(),
                    "target_date_label": row["target_date"].isoformat(),
                }
            )
        return " ".join(line_points), point_rows

    forecast_line, forecast_points = build_series("forecast_return_pct")
    actual_line, actual_points = build_series("actual_return_pct")
    return {
        "available": True,
        "forecast_line": forecast_line,
        "actual_line": actual_line,
        "forecast_points": forecast_points,
        "actual_points": actual_points,
        "min_label": f"{format_axis_value(series_min)} %",
        "max_label": f"{format_axis_value(series_max)} %",
        "start_label": normalized_rows[0]["forecast_date"].isoformat(),
        "end_label": normalized_rows[-1]["forecast_date"].isoformat(),
        "points_count": len(normalized_rows),
    }


def build_one_year_projection(
    history,
    position: EquityPosition,
    correlation: dict,
    six_month_snapshot: dict,
    cycle_metrics: dict | None = None,
) -> dict:
    if not history:
        return {"available": False}

    latest_price = history[-1].close_price if history[-1].close_price else position.current_price_per_share
    latest_date = history[-1].price_date
    cycle_metrics = cycle_metrics or build_cycle_metrics(history)
    stock_6m_return_pct = six_month_snapshot.get("stock_return_pct") if six_month_snapshot.get("available") else None
    reference_6m_return_pct = six_month_snapshot.get("benchmark_return_pct") if six_month_snapshot.get("available") else None
    one_year_snapshot = build_period_snapshot(
        history,
        "1Y",
        start_date=latest_date - timedelta(days=365),
        end_date=latest_date,
    )
    three_year_snapshot = build_period_snapshot(
        history,
        "3Y",
        start_date=latest_date - timedelta(days=365 * 3),
        end_date=latest_date,
    )
    coefficient = correlation.get("coefficient")
    observations_count = correlation.get("observations_count", 0)
    price_return_components = []

    stock_6m_signal = build_projection_signal(stock_6m_return_pct, 6)
    if stock_6m_signal is not None:
        price_return_components.append(stock_6m_signal * Decimal("0.20"))
    if one_year_snapshot.get("available") and one_year_snapshot.get("stock_return_pct") is not None:
        price_return_components.append(one_year_snapshot["stock_return_pct"] * Decimal("0.30"))
    if three_year_snapshot.get("available") and three_year_snapshot.get("stock_return_pct") is not None:
        three_year_signal = annualize_return_pct(three_year_snapshot["stock_return_pct"], 36)
        if three_year_signal is not None:
            price_return_components.append(three_year_signal * Decimal("0.20"))
    if cycle_metrics.get("cagr_pct") is not None:
        price_return_components.append(cycle_metrics["cagr_pct"] * Decimal("0.30"))
    if cycle_metrics.get("current_drawdown_pct") is not None:
        current_drawdown_pct = cycle_metrics["current_drawdown_pct"]
        mean_reversion_pct = clamp_decimal(abs(current_drawdown_pct) * Decimal("0.18"), ZERO, Decimal("10.00"))
        if current_drawdown_pct <= Decimal("-8.00"):
            price_return_components.append(mean_reversion_pct)
        elif current_drawdown_pct >= Decimal("-3.00"):
            price_return_components.append(-mean_reversion_pct * Decimal("0.40"))

    reference_one_year_return_pct = one_year_snapshot.get("benchmark_return_pct") if one_year_snapshot.get("available") else None
    if coefficient is not None and reference_one_year_return_pct is not None:
        price_return_components.append(reference_one_year_return_pct * coefficient * Decimal("0.15"))

    if not price_return_components:
        return {"available": False}

    price_return_pct = sum(price_return_components, ZERO)
    annualized_volatility_pct = cycle_metrics.get("annualized_volatility_pct")
    confidence = build_projection_confidence(
        coefficient,
        observations_count,
        cycle_metrics.get("monthly_observations_count", 0),
        years_covered=cycle_metrics.get("years_covered", ZERO),
        positive_year_ratio_pct=cycle_metrics.get("positive_year_ratio_pct"),
        stability_gap=correlation.get("stability_gap"),
    )
    evidence_factor = Decimal("0.76")
    if confidence["label"] == "Alta":
        evidence_factor = Decimal("0.98")
    elif confidence["label"] == "Media":
        evidence_factor = Decimal("0.88")
    safety = build_safety_score(cycle_metrics, correlation)
    safety_multiplier = clamp_decimal(Decimal("0.88") + (safety["score"] / Decimal("600")), Decimal("0.88"), Decimal("1.05"))
    price_return_pct = clamp_decimal(
        price_return_pct * evidence_factor * safety_multiplier,
        Decimal("-35.00"),
        Decimal("40.00"),
    )

    current_value = position.current_value if position.current_value else position.invested_amount
    broker_costs = position.estimated_broker_costs
    net_income_yield_pct = None
    transaction_drag_pct = None
    gross_dividend_yield_pct = None
    if current_value and current_value > 0:
        gross_dividend_yield_pct = (position.annual_dividend_income / current_value) * ONE_HUNDRED
        net_income_yield_pct = (position.net_annual_income / current_value) * ONE_HUNDRED
        transaction_drag_pct = (broker_costs.get("roundtrip_total_cost", ZERO) / current_value) * ONE_HUNDRED
    base_return_pct = price_return_pct
    if net_income_yield_pct is not None:
        base_return_pct += net_income_yield_pct
    if transaction_drag_pct is not None:
        base_return_pct -= transaction_drag_pct
    base_return_pct = clamp_decimal(base_return_pct, Decimal("-45.00"), Decimal("45.00"))

    band_pct = annualized_volatility_pct * Decimal("0.65") if annualized_volatility_pct is not None else Decimal("16.00")
    if confidence["label"] == "Alta":
        band_pct -= Decimal("2.50")
    elif confidence["label"] == "Baja":
        band_pct += Decimal("5.00")
    if safety["label"] == "Alta":
        band_pct -= Decimal("1.50")
    elif safety["label"] == "Baja":
        band_pct += Decimal("3.00")
    band_pct = clamp_decimal(band_pct, Decimal("8.00"), Decimal("32.00"))

    low_return_pct = clamp_decimal(base_return_pct - band_pct, Decimal("-80.00"), Decimal("120.00"))
    high_return_pct = clamp_decimal(base_return_pct + band_pct, Decimal("-80.00"), Decimal("140.00"))
    benefit_risk_ratio = None
    if band_pct > 0:
        benefit_risk_ratio = base_return_pct / band_pct
    decision_score = base_return_pct * projection_confidence_multiplier(confidence["label"]) * (safety["score"] / ONE_HUNDRED)
    years_covered = cycle_metrics.get("years_covered", ZERO)
    if coefficient is None or reference_one_year_return_pct is None:
        explanation = (
            f"La proyeccion 12M mezcla el ciclo propio de la accion y su historico de {years_covered:.2f} anos. "
            f"Hoy pesa mas la trayectoria del valor que la referencia porque la relacion con {position.analysis_reference_label} "
            f"todavia no es lo bastante robusta."
        )
    else:
        explanation = (
            f"La proyeccion usa hasta {years_covered:.2f} anos de serie, fase {cycle_metrics.get('cycle_phase', 'sin ciclo').lower()} "
            f"y la referencia {position.analysis_reference_label} con correlacion 10A de {coefficient:.2f}. "
            f"El retorno total incluye dividendos netos y penaliza comisiones y custodia del broker."
        )

    price_low_return_pct = clamp_decimal(price_return_pct - band_pct, Decimal("-80.00"), Decimal("120.00"))
    price_high_return_pct = clamp_decimal(price_return_pct + band_pct, Decimal("-80.00"), Decimal("140.00"))

    return {
        "available": True,
        "price_return_pct": price_return_pct,
        "price_low_return_pct": price_low_return_pct,
        "price_high_return_pct": price_high_return_pct,
        "base_return_pct": base_return_pct,
        "low_return_pct": low_return_pct,
        "high_return_pct": high_return_pct,
        "projected_price": project_price_from_return(latest_price, price_return_pct),
        "low_price": project_price_from_return(latest_price, price_low_return_pct),
        "high_price": project_price_from_return(latest_price, price_high_return_pct),
        "quarterly_path": build_projection_path(latest_price, price_return_pct, anchor_date=history[-1].price_date),
        "confidence_label": confidence["label"],
        "confidence_note": confidence["note"],
        "confidence_score_pct": confidence["score_pct"],
        "safety_score": safety["score"],
        "safety_label": safety["label"],
        "benefit_risk_ratio": benefit_risk_ratio,
        "decision_score": decision_score,
        "stock_6m_return_pct": stock_6m_return_pct,
        "reference_6m_return_pct": reference_6m_return_pct,
        "stock_1y_return_pct": one_year_snapshot.get("stock_return_pct") if one_year_snapshot.get("available") else None,
        "reference_1y_return_pct": reference_one_year_return_pct,
        "coefficient": coefficient,
        "cycle_phase": cycle_metrics.get("cycle_phase"),
        "years_covered": years_covered,
        "cagr_pct": cycle_metrics.get("cagr_pct"),
        "annualized_volatility_pct": annualized_volatility_pct,
        "max_drawdown_pct": cycle_metrics.get("max_drawdown_pct"),
        "current_drawdown_pct": cycle_metrics.get("current_drawdown_pct"),
        "positive_year_ratio_pct": cycle_metrics.get("positive_year_ratio_pct"),
        "gross_dividend_yield_pct": gross_dividend_yield_pct,
        "net_income_yield_pct": net_income_yield_pct,
        "transaction_drag_pct": transaction_drag_pct,
        "broker_costs": broker_costs,
        "reference_label": position.analysis_reference_label,
        "explanation": explanation,
    }


def find_closest_history_point(history, target_date: date, tolerance_days: int = 20):
    candidates = [point for point in history if abs((point.price_date - target_date).days) <= tolerance_days]
    if not candidates:
        return None
    return min(candidates, key=lambda point: (abs((point.price_date - target_date).days), point.price_date))


def build_projection_backtest(history, position: EquityPosition, max_rows: int = 8) -> dict:
    monthly_points = collapse_history_to_frequency(history, "monthly")
    if len(monthly_points) < 20:
        return {
            "available": False,
            "comparisons_count": 0,
            "rows": [],
            "monthly_chart": {"available": False},
            "mean_absolute_error_pct": None,
            "direction_hit_rate_pct": None,
            "in_range_rate_pct": None,
            "precision_label": "Sin historico suficiente",
        }

    rows = []
    for anchor_point in monthly_points:
        anchor_date = anchor_point.price_date
        if anchor_date - monthly_points[0].price_date < timedelta(days=182):
            continue

        future_point = find_closest_history_point(history, anchor_date + timedelta(days=365))
        if future_point is None or future_point.price_date <= anchor_date:
            continue

        visible_history = [point for point in history if point.price_date <= anchor_date]
        six_month_snapshot = build_period_snapshot(
            visible_history,
            "6M",
            start_date=anchor_date - timedelta(days=182),
            end_date=anchor_date,
        )
        if not six_month_snapshot["available"]:
            continue

        correlation = build_reference_correlation(visible_history, position)
        projection = build_one_year_projection(visible_history, position, correlation, six_month_snapshot)
        if not projection.get("available"):
            continue

        actual_return_pct = percentage_change(future_point.close_price, anchor_point.close_price)
        if actual_return_pct is None:
            continue

        forecast_return_pct = projection.get("price_return_pct")
        if forecast_return_pct is None:
            continue
        absolute_error_pct = abs(forecast_return_pct - actual_return_pct)
        direction_hit = (
            (forecast_return_pct >= 0 and actual_return_pct >= 0)
            or (forecast_return_pct < 0 and actual_return_pct < 0)
        )
        in_range = False
        if projection.get("price_low_return_pct") is not None and projection.get("price_high_return_pct") is not None:
            in_range = projection["price_low_return_pct"] <= actual_return_pct <= projection["price_high_return_pct"]

        rows.append(
            {
                "forecast_date": anchor_date,
                "target_date": future_point.price_date,
                "forecast_return_pct": quantize_decimal(forecast_return_pct),
                "actual_return_pct": quantize_decimal(actual_return_pct),
                "absolute_error_pct": quantize_decimal(absolute_error_pct),
                "direction_hit": direction_hit,
                "in_range": in_range,
                "forecast_price": quantize_decimal(projection.get("projected_price"), "0.0001"),
                "actual_price": quantize_decimal(future_point.close_price, "0.0001"),
                "confidence_label": projection.get("confidence_label"),
            }
        )

    if not rows:
        return {
            "available": False,
            "comparisons_count": 0,
            "rows": [],
            "monthly_chart": {"available": False},
            "mean_absolute_error_pct": None,
            "direction_hit_rate_pct": None,
            "in_range_rate_pct": None,
            "precision_label": "Sin historico suficiente",
        }

    recent_rows = list(reversed(rows[-max_rows:]))
    comparisons_count = len(rows)
    monthly_chart = build_backtest_monthly_chart(rows)
    mean_absolute_error_pct = quantize_decimal(
        sum((row["absolute_error_pct"] for row in rows), ZERO) / Decimal(comparisons_count)
    )
    direction_hit_rate_pct = quantize_decimal(
        Decimal(sum(1 for row in rows if row["direction_hit"])) * Decimal("100") / Decimal(comparisons_count)
    )
    in_range_rate_pct = quantize_decimal(
        Decimal(sum(1 for row in rows if row["in_range"])) * Decimal("100") / Decimal(comparisons_count)
    )

    if mean_absolute_error_pct <= Decimal("8") and direction_hit_rate_pct >= Decimal("70"):
        precision_label = "Alta"
    elif mean_absolute_error_pct <= Decimal("15") and direction_hit_rate_pct >= Decimal("55"):
        precision_label = "Media"
    else:
        precision_label = "Baja"

    return {
        "available": True,
        "comparisons_count": comparisons_count,
        "rows": recent_rows,
        "monthly_chart": monthly_chart,
        "mean_absolute_error_pct": mean_absolute_error_pct,
        "direction_hit_rate_pct": direction_hit_rate_pct,
        "in_range_rate_pct": in_range_rate_pct,
        "precision_label": precision_label,
    }


def build_projection_reliability(projection: dict, backtest: dict) -> dict:
    projection_score = projection.get("confidence_score_pct") or Decimal("40.00")
    backtest_score_map = {
        "Alta": Decimal("82.00"),
        "Media": Decimal("64.00"),
        "Baja": Decimal("42.00"),
        "Sin historico suficiente": Decimal("45.00"),
    }
    if backtest.get("available"):
        backtest_score = backtest_score_map.get(backtest.get("precision_label"), Decimal("45.00"))
        reliability_score = (projection_score * Decimal("0.45")) + (backtest_score * Decimal("0.55"))
    else:
        reliability_score = projection_score

    if reliability_score >= Decimal("75.00"):
        label = "Alta"
    elif reliability_score >= Decimal("58.00"):
        label = "Media"
    else:
        label = "Baja"
    return {
        "score": quantize_decimal(reliability_score),
        "label": label,
    }


def build_trade_alert(
    position: EquityPosition,
    projection: dict,
    correlation: dict,
    reliability: dict,
    relative_trend: dict,
    six_month_snapshot: dict,
    one_year_snapshot: dict,
) -> dict:
    base_payload = {
        "label": "Vigilar",
        "tone": "watch",
        "score": ZERO,
        "note": "No hay una pendiente prolongada lo bastante clara frente a la referencia como para activar compra o venta.",
        "trend_label": relative_trend.get("label", "Sin tendencia relativa"),
        "periods_label": relative_trend.get("periods_label", "periodos"),
        "gap_slope_pct": relative_trend.get("gap_slope_pct"),
        "recent_gap_avg_pct": relative_trend.get("recent_gap_avg_pct"),
        "positive_streak": relative_trend.get("positive_streak", 0),
        "negative_streak": relative_trend.get("negative_streak", 0),
        "trigger_label": "Sin confirmacion prolongada",
    }
    if not projection.get("available"):
        base_payload["note"] = "Todavia no hay suficiente historico para activar una alerta operativa."
        return base_payload

    reliability_score = reliability.get("score") or Decimal("40.00")
    safety_score = projection.get("safety_score") or Decimal("55.00")
    projected_return_pct = projection.get("base_return_pct") or ZERO
    one_year_alpha_pct = one_year_snapshot.get("alpha_pct") if one_year_snapshot.get("available") else None
    six_month_alpha_pct = six_month_snapshot.get("alpha_pct") if six_month_snapshot.get("available") else None
    trade_score = ZERO

    if relative_trend.get("prolonged_positive"):
        trade_score += Decimal("4.00")
    elif relative_trend.get("prolonged_negative"):
        trade_score -= Decimal("4.00")

    recent_gap_avg_pct = relative_trend.get("recent_gap_avg_pct")
    if recent_gap_avg_pct is not None:
        trade_score += clamp_decimal(recent_gap_avg_pct * Decimal("0.40"), Decimal("-2.00"), Decimal("2.00"))

    gap_slope_pct = relative_trend.get("gap_slope_pct")
    if gap_slope_pct is not None:
        trade_score += clamp_decimal(gap_slope_pct * Decimal("2.50"), Decimal("-1.50"), Decimal("1.50"))

    alpha_signal_pct = one_year_alpha_pct if one_year_alpha_pct is not None else six_month_alpha_pct
    if alpha_signal_pct is not None:
        trade_score += clamp_decimal(alpha_signal_pct * Decimal("0.12"), Decimal("-1.50"), Decimal("1.50"))

    trade_score += clamp_decimal((projected_return_pct - Decimal("4.00")) * Decimal("0.08"), Decimal("-1.50"), Decimal("1.50"))
    trade_score += clamp_decimal((safety_score - Decimal("55.00")) * Decimal("0.03"), Decimal("-0.75"), Decimal("0.75"))

    if reliability_score < Decimal("55.00"):
        trade_score *= Decimal("0.78")

    coefficient = correlation.get("coefficient")
    if coefficient is not None and abs(coefficient) >= Decimal("0.35"):
        trade_score += Decimal("0.35") if trade_score > ZERO else Decimal("-0.35") if trade_score < ZERO else ZERO

    positive_streak = relative_trend.get("positive_streak", 0)
    negative_streak = relative_trend.get("negative_streak", 0)
    periods_label = relative_trend.get("periods_label", "periodos")
    trend_label = relative_trend.get("label", "Sin tendencia relativa")

    if trade_score >= Decimal("3.20"):
        note = (
            f"La accion encadena {positive_streak} {periods_label} superando su referencia ajustada por coeficiente. "
            f"La pendiente relativa es positiva y el retorno neto 12M sigue apoyando compras."
        )
        if position.is_owned:
            note += " Si ya esta en cartera, la lectura es compatible con mantener o ampliar."
        return {
            **base_payload,
            "label": "Comprar",
            "tone": "buy",
            "score": quantize_decimal(trade_score) or ZERO,
            "note": note,
            "trend_label": trend_label,
            "trigger_label": f"{positive_streak} {periods_label} con alpha positiva",
        }

    if trade_score <= Decimal("-3.20"):
        note = (
            f"La accion encadena {negative_streak} {periods_label} perdiendo fuerza frente a su referencia ajustada por coeficiente. "
            f"La pendiente relativa es negativa y la proyeccion neta se deteriora."
        )
        if position.is_owned:
            note += " Conviene revisar venta total o parcial."
        return {
            **base_payload,
            "label": "Vender",
            "tone": "sell",
            "score": quantize_decimal(trade_score) or ZERO,
            "note": note,
            "trend_label": trend_label,
            "trigger_label": f"{negative_streak} {periods_label} con pendiente negativa",
        }

    note = base_payload["note"]
    if recent_gap_avg_pct is not None:
        direction = "mejorando" if recent_gap_avg_pct > ZERO else "debilitandose" if recent_gap_avg_pct < ZERO else "sin cambio"
        note = (
            f"La lectura relativa esta {direction}, pero todavia no acumula suficiente persistencia para activar compra o venta."
        )
    return {
        **base_payload,
        "score": quantize_decimal(trade_score) or ZERO,
        "note": note,
        "trend_label": trend_label,
    }


def build_decision_action_label(
    position: EquityPosition,
    projected_return_pct: Decimal | None,
    safety_score: Decimal | None,
    reliability_score: Decimal | None,
) -> str:
    projected_return_pct = projected_return_pct or ZERO
    safety_score = safety_score or ZERO
    reliability_score = reliability_score or ZERO
    if projected_return_pct >= Decimal("10.00") and safety_score >= Decimal("65.00") and reliability_score >= Decimal("70.00"):
        return "Priorizar"
    if projected_return_pct >= Decimal("4.00") and safety_score >= Decimal("55.00"):
        return "Mantener" if position.is_owned else "Seguir"
    if projected_return_pct >= ZERO:
        return "Vigilar"
    return "Reducir riesgo" if position.is_owned else "Esperar"


def build_equity_decision_rows(history_cards: list[dict]) -> list[dict]:
    rows = []
    status_order = {"owned": 0, "watchlist": 1, "ibex": 2, "guide": 3}
    for card in history_cards:
        position = card["position"]
        projection = card.get("projection", {})
        if not card.get("has_history") or not projection.get("available"):
            continue
        reliability = card.get("projection_reliability", {"label": "Baja", "score": Decimal("40.00")})
        best_candidate = card.get("reference_playbook", {}).get("best_candidate")
        projected_return_pct = projection.get("base_return_pct")
        trade_alert = card.get("trade_alert", {})
        rows.append(
            {
                "position": position,
                "status_key": card.get("status_key") or ("owned" if position.is_owned else "watchlist"),
                "status_label": card.get("status_label") or position.get_position_kind_display(),
                "company_name": position.company_name,
                "ticker": position.ticker,
                "sector_label": card.get("sector_label") or "Sin sector",
                "detail_anchor": card.get("detail_anchor") or "",
                "reference_label": card.get("reference_label"),
                "best_reference_label": best_candidate.get("name") if best_candidate else card.get("reference_label"),
                "correlation": card.get("correlation", {}).get("coefficient"),
                "years_covered": projection.get("years_covered"),
                "projected_return_pct": projected_return_pct,
                "projected_price": projection.get("projected_price"),
                "safety_score": projection.get("safety_score"),
                "safety_label": projection.get("safety_label"),
                "reliability_score": reliability.get("score"),
                "reliability_label": reliability.get("label"),
                "benefit_risk_ratio": projection.get("benefit_risk_ratio"),
                "cycle_phase": projection.get("cycle_phase"),
                "decision_score": projection.get("decision_score"),
                "trade_alert_label": trade_alert.get("label", "Vigilar"),
                "trade_alert_tone": trade_alert.get("tone", "watch"),
                "trade_alert_note": trade_alert.get("note", ""),
                "trade_alert_score": trade_alert.get("score", ZERO),
                "trade_alert_trigger": trade_alert.get("trigger_label", ""),
                "action_label": build_decision_action_label(
                    position,
                    projected_return_pct,
                    projection.get("safety_score"),
                    reliability.get("score"),
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            status_order.get(item["status_key"], 9),
            -(item["decision_score"] if item["decision_score"] is not None else Decimal("-9999")),
            -(item["trade_alert_score"] if item["trade_alert_score"] is not None else Decimal("-9999")),
            item["company_name"],
        )
    )
    return rows


def build_candlestick_svg(history, width: int = 640, height: int = 220, padding: int = 18) -> str:
    candles = history[-32:]
    if len(candles) < 2:
        return ""

    highs = [point.high_price or point.close_price for point in candles]
    lows = [point.low_price or point.close_price for point in candles]
    min_price = min(lows)
    max_price = max(highs)
    if max_price == min_price:
        max_price += Decimal("1")

    span_x = width - 2 * padding
    span_y = height - 2 * padding
    step = span_x / max(len(candles) - 1, 1)
    body_width = max(step * 0.55, 6)

    def to_y(value: Decimal) -> float:
        normalized = float((value - min_price) / (max_price - min_price))
        return float(height - padding - (normalized * span_y))

    fragments = [
        f'<line x1="{padding}" y1="{height - padding}" x2="{width - padding}" y2="{height - padding}" stroke="#d9e4ec" stroke-width="1" />',
        f'<line x1="{padding}" y1="{padding}" x2="{padding}" y2="{height - padding}" stroke="#d9e4ec" stroke-width="1" />',
    ]

    for index, point in enumerate(candles):
        open_price = point.open_price or point.close_price
        close_price = point.close_price
        high_price = point.high_price or max(open_price, close_price)
        low_price = point.low_price or min(open_price, close_price)
        center_x = padding + (step * index)
        wick_top = to_y(high_price)
        wick_bottom = to_y(low_price)
        body_top_price = max(open_price, close_price)
        body_bottom_price = min(open_price, close_price)
        body_top = to_y(body_top_price)
        body_bottom = to_y(body_bottom_price)
        body_height = max(body_bottom - body_top, 2.0)
        tone = "#177245" if close_price >= open_price else "#a85f00"
        body_y = min(body_top, body_bottom)
        fragments.append(
            f'<line x1="{center_x:.1f}" y1="{wick_top:.1f}" x2="{center_x:.1f}" y2="{wick_bottom:.1f}" stroke="{tone}" stroke-width="1.5" />'
        )
        fragments.append(
            f'<rect x="{center_x - (body_width / 2):.1f}" y="{body_y:.1f}" width="{body_width:.1f}" height="{body_height:.1f}" rx="2" fill="{tone}" opacity="0.88" />'
        )

    return "".join(fragments)


def build_candlestick_metrics(history) -> dict:
    recent = history[-50:]
    if not recent:
        return {
            "trend_label": "Sin historico",
            "last_candle_label": "Sin vela",
            "average_range_pct": None,
            "support_level": None,
            "resistance_level": None,
            "candlestick_svg": "",
        }

    closes = [point.close_price for point in recent]
    sma20 = average_decimal(closes[-20:]) or recent[-1].close_price
    sma50 = average_decimal(closes[-50:]) or sma20
    latest_close = recent[-1].close_price

    if latest_close > sma20 and sma20 >= sma50:
        trend_label = "Tendencia alcista"
    elif latest_close < sma20 and sma20 <= sma50:
        trend_label = "Tendencia bajista"
    else:
        trend_label = "Tendencia lateral"

    last_point = recent[-1]
    last_open = last_point.open_price or last_point.close_price
    last_high = last_point.high_price or max(last_open, last_point.close_price)
    last_low = last_point.low_price or min(last_open, last_point.close_price)
    body_size = abs(last_point.close_price - last_open)
    candle_range = last_high - last_low
    if candle_range and body_size <= candle_range * Decimal("0.15"):
        last_candle_label = "Doji"
    elif last_point.close_price >= last_open:
        last_candle_label = "Vela alcista"
    else:
        last_candle_label = "Vela bajista"

    range_percentages = []
    for point in recent[-20:]:
        base_open = point.open_price or point.close_price
        high_price = point.high_price or point.close_price
        low_price = point.low_price or point.close_price
        if base_open:
            range_percentages.append(((high_price - low_price) / base_open) * Decimal("100"))

    support_level = min((point.low_price or point.close_price for point in recent[-20:]), default=None)
    resistance_level = max((point.high_price or point.close_price for point in recent[-20:]), default=None)

    return {
        "trend_label": trend_label,
        "last_candle_label": last_candle_label,
        "average_range_pct": average_decimal(range_percentages),
        "support_level": support_level,
        "resistance_level": resistance_level,
        "candlestick_svg": build_candlestick_svg(recent),
    }


def build_suggested_reference_cards(history, position: EquityPosition, reference_cache: dict) -> list[dict]:
    suggestions = []
    selected_key = (
        position.reference_profile,
        position.benchmark_symbol,
        position.benchmark_name,
    )
    for reference in build_reference_suggestions_for_equity(position.company_name, position.ticker):
        cache_key = (
            reference["reference_profile"],
            reference["benchmark_symbol"],
            reference["benchmark_name"],
        )
        correlation = {
            "frequency": infer_reference_frequency_from_profile(reference["reference_profile"]),
            "coefficient": None,
            "label": "Sin datos",
            "observations_count": 0,
        }
        try:
            cached_value = reference_cache.get(cache_key)
            if cached_value is None:
                cached_value = fetch_reference_series_for_choice(
                    reference["reference_profile"],
                    benchmark_symbol=reference["benchmark_symbol"],
                    benchmark_name=reference["benchmark_name"],
                )
                reference_cache[cache_key] = cached_value
            if isinstance(cached_value, Exception):
                raise cached_value
            correlation = build_reference_correlation_for_series(
                history,
                cached_value,
                reference["reference_profile"],
            )
        except Exception as exc:
            reference_cache[cache_key] = exc

        suggestions.append(
            {
                **reference,
                "is_selected": cache_key == selected_key,
                "correlation": correlation,
            }
        )
    suggestions.sort(
        key=lambda item: (
            0 if item["correlation"]["coefficient"] is not None else 1,
            -(abs(item["correlation"]["coefficient"])) if item["correlation"]["coefficient"] is not None else Decimal("0"),
            item["benchmark_name"],
        )
    )
    best_marked = False
    for suggestion in suggestions:
        coefficient = suggestion["correlation"]["coefficient"]
        is_best = coefficient is not None and not best_marked
        suggestion["is_best"] = is_best
        if is_best:
            best_marked = True
    return suggestions


def build_equity_history_card(
    position: EquityPosition,
    history,
    reference_cache: dict,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    status_key: str | None = None,
    status_label: str | None = None,
    detail_anchor: str | None = None,
    sector_label: str | None = None,
) -> dict:
    resolved_status_key = status_key or ("owned" if position.is_owned else "watchlist")
    resolved_status_label = status_label or position.get_position_kind_display()
    resolved_detail_anchor = detail_anchor if detail_anchor is not None else (f"stock-{position.id}" if position.id else "")
    resolved_sector_label = sector_label or resolve_equity_sector_label(
        company_name=position.company_name,
        ticker=position.ticker,
        quote_symbol=position.quote_symbol,
    )

    if not history:
        return {
            "position": position,
            "status_key": resolved_status_key,
            "status_label": resolved_status_label,
            "detail_anchor": resolved_detail_anchor,
            "sector_label": resolved_sector_label,
            "has_history": False,
            "projection": {"available": False},
            "projection_backtest": {"available": False, "monthly_chart": {"available": False}},
            "projection_reliability": {"label": "Baja", "score": Decimal("40.00")},
            "trade_alert": {
                "label": "Vigilar",
                "tone": "watch",
                "score": ZERO,
                "note": "Todavia no hay historico suficiente para activar una alerta.",
                "trend_label": "Sin tendencia relativa",
                "trigger_label": "Sin historico suficiente",
            },
            "reference_playbook": {"available": False, "candidates": []},
            "suggested_references": [],
        }

    first_price = history[0].close_price
    first_benchmark = next((point.benchmark_close for point in history if point.benchmark_close is not None), None)
    latest_point = history[-1]
    if selected_start_date or selected_end_date:
        selected_period = build_period_snapshot(
            history,
            "Periodo elegido",
            start_date=selected_start_date,
            end_date=selected_end_date,
        )
    else:
        selected_period = {"available": False}
    if not selected_period["available"] and latest_point.price_date:
        selected_period = build_period_snapshot(
            history,
            "1Y",
            start_date=latest_point.price_date - timedelta(days=365),
            end_date=latest_point.price_date,
        )

    period_snapshots = [
        build_period_snapshot(history, "1Y", start_date=latest_point.price_date - timedelta(days=365), end_date=latest_point.price_date),
        build_period_snapshot(history, "3Y", start_date=latest_point.price_date - timedelta(days=365 * 3), end_date=latest_point.price_date),
        build_period_snapshot(history, "5Y", start_date=latest_point.price_date - timedelta(days=365 * 5), end_date=latest_point.price_date),
        build_period_snapshot(history, "10Y", start_date=latest_point.price_date - timedelta(days=LONG_ANALYSIS_DAYS), end_date=latest_point.price_date),
    ]
    one_year_snapshot = next((snapshot for snapshot in period_snapshots if snapshot["label"] == "1Y"), {"available": False})
    correlation = build_reference_correlation(history, position)
    six_month_snapshot = build_period_snapshot(
        history,
        "6M",
        start_date=latest_point.price_date - timedelta(days=182),
        end_date=latest_point.price_date,
    )
    cycle_metrics = build_cycle_metrics(history)
    projection = build_one_year_projection(history, position, correlation, six_month_snapshot, cycle_metrics=cycle_metrics)
    projection_backtest = build_projection_backtest(history, position)
    projection_reliability = (
        build_projection_reliability(projection, projection_backtest)
        if projection.get("available")
        else {"label": "Baja", "score": Decimal("40.00")}
    )
    relative_trend = build_relative_strength_trend(history, position, correlation.get("coefficient"))
    trade_alert = build_trade_alert(
        position,
        projection,
        correlation,
        projection_reliability,
        relative_trend,
        six_month_snapshot,
        one_year_snapshot,
    )
    stock_series = [{"date": point.price_date, "value": point.close_price} for point in history]
    benchmark_series = [
        {"date": point.price_date, "value": point.benchmark_close}
        for point in history
        if point.benchmark_close is not None
    ]
    projection_series = []
    if projection.get("available"):
        projection_series = [{"date": latest_point.price_date, "value": latest_point.close_price}]
        projection_series.extend(
            {
                "date": step["projected_date"],
                "value": step["projected_price"],
            }
            for step in projection.get("quarterly_path", [])
            if step.get("projected_date") and step.get("projected_price") is not None
        )
    dual_axis_chart = build_dual_axis_chart(stock_series, benchmark_series, projection_points=projection_series)
    maintenance_drag_pct = (
        (position.recurring_cost_used / position.invested_amount) * Decimal("100")
        if position.invested_amount
        else ZERO
    )
    broker_costs = {
        **position.estimated_broker_costs,
        "annual_cost_used": position.recurring_cost_used,
        "annual_cost_source": position.recurring_cost_source,
        "net_dividend_income": position.net_dividend_income,
    }
    suggested_references = build_suggested_reference_cards(history, position, reference_cache)
    historical_chart = build_stock_history_chart(history)
    best_correlation_chart = build_best_correlation_chart(history, suggested_references, reference_cache)
    projection_12m_chart = build_projection_12m_chart(history, projection)

    card = {
        "position": position,
        "status_key": resolved_status_key,
        "status_label": resolved_status_label,
        "detail_anchor": resolved_detail_anchor,
        "sector_label": resolved_sector_label or "Sin sector",
        "has_history": True,
        "points_count": len(history),
        "start_date": history[0].price_date,
        "end_date": history[-1].price_date,
        "reference_label": position.analysis_reference_label,
        "reference_profile_label": position.get_reference_profile_display(),
        "stock_return_pct": ((history[-1].close_price / first_price) - 1) * Decimal("100") if first_price else ZERO,
        "benchmark_return_pct": (
            ((history[-1].benchmark_close / first_benchmark) - 1) * Decimal("100")
            if first_benchmark and history[-1].benchmark_close
            else None
        ),
        "stock_line": dual_axis_chart["stock_line"],
        "benchmark_line": dual_axis_chart["reference_line"],
        "projection_line": dual_axis_chart["projection_line"],
        "dual_axis_chart": dual_axis_chart,
        "period_snapshots": period_snapshots,
        "selected_period": selected_period,
        "net_unrealized_gain": position.unrealized_gain_after_costs,
        "net_unrealized_return_pct": position.unrealized_return_pct,
        "net_annual_income": position.net_annual_income,
        "maintenance_drag_pct": maintenance_drag_pct,
        "price_vs_cost_pct": percentage_change(position.current_price_per_share, position.average_cost_per_share),
        "broker_costs": broker_costs,
        "correlation": correlation,
        "cycle_metrics": cycle_metrics,
        "projection": projection,
        "projection_backtest": projection_backtest,
        "projection_reliability": projection_reliability,
        "relative_trend": relative_trend,
        "trade_alert": trade_alert,
        "suggested_references": suggested_references,
        "historical_chart": historical_chart,
        "best_correlation_chart": best_correlation_chart,
        "projection_12m_chart": projection_12m_chart,
    }
    card["reference_playbook"] = build_reference_playbook_from_card(card)
    return card


def build_equity_history_cards(
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    reference_cache: dict | None = None,
) -> list[dict]:
    cards = []
    reference_cache = reference_cache if reference_cache is not None else {}
    for position in positions:
        history = list(position.price_history.order_by("price_date"))
        cards.append(
            build_equity_history_card(
                position,
                history,
                reference_cache,
                selected_start_date=selected_start_date,
                selected_end_date=selected_end_date,
            )
        )
    return cards


def resolve_analysis_broker_profile(positions) -> dict:
    fallback = {
        "broker": "Interactive Brokers",
        "trade_channel": EquityPosition.TradeChannel.APP,
        "ownership_category": AssetOwnershipCategory.JOINT,
    }
    candidates = [position for position in positions if position.is_owned] or list(positions)
    if not candidates:
        return {
            **fallback,
            "trade_channel_label": dict(EquityPosition.TradeChannel.choices).get(EquityPosition.TradeChannel.APP, "App"),
        }

    grouped: dict[tuple[str, str, str], dict] = {}
    for position in candidates:
        key = (position.broker, position.trade_channel, position.ownership_category)
        summary = grouped.setdefault(key, {"count": 0, "current_value": ZERO})
        summary["count"] += 1
        summary["current_value"] += position.current_value if position.is_owned else ZERO

    selected_key, _ = max(
        grouped.items(),
        key=lambda item: (item[1]["count"], item[1]["current_value"]),
    )
    broker, trade_channel, ownership_category = selected_key
    return {
        "broker": broker,
        "trade_channel": trade_channel,
        "ownership_category": ownership_category,
        "trade_channel_label": dict(EquityPosition.TradeChannel.choices).get(trade_channel, trade_channel),
    }


def build_ibex_universe_companies(workbook_snapshot: dict) -> list[dict]:
    merged: dict[str, dict] = {}
    catalog = get_equity_company_catalog()
    catalog_by_ticker = {clean_ticker(entry["ticker"]): entry for entry in catalog}

    for company in workbook_snapshot.get("companies", []):
        profile = (
            catalog_by_ticker.get(clean_ticker(company.get("ticker", "")))
            or find_equity_company_profile(company.get("ticker", ""))
            or find_equity_company_profile(company.get("company_name", ""))
        )
        ticker = clean_ticker(company.get("ticker", "")) or (profile["ticker"] if profile else "")
        if not ticker:
            continue
        merged[ticker] = {
            **company,
            "ticker": ticker,
            "company_name": company.get("company_name") or (profile["company_name"] if profile else ticker),
            "quote_symbol": company.get("quote_symbol") or (profile["quote_symbol"] if profile else ""),
            "sector": company.get("sector") or (profile["sector_label"] if profile else ""),
            "catalog_profile": profile,
        }

    for profile in catalog:
        ticker = clean_ticker(profile["ticker"])
        if ticker in merged:
            merged[ticker].setdefault("quote_symbol", profile["quote_symbol"])
            merged[ticker].setdefault("sector", profile["sector_label"])
            merged[ticker].setdefault("company_name", profile["company_name"])
            merged[ticker]["catalog_profile"] = merged[ticker].get("catalog_profile") or profile
            continue
        merged[ticker] = {
            "ticker": ticker,
            "company_name": profile["company_name"],
            "quote_symbol": profile["quote_symbol"],
            "sector": profile["sector_label"],
            "dividend_yield": None,
            "catalog_profile": profile,
        }

    return sorted(merged.values(), key=lambda item: item.get("company_name") or item.get("ticker") or "")


def resolve_ibex_reference_choice(company: dict, workbook_snapshot: dict) -> tuple[dict, dict | None]:
    workbook_playbook = None
    if workbook_snapshot.get("available"):
        workbook_playbook = build_workbook_reference_playbook(company, workbook_snapshot)
        for candidate in workbook_playbook.get("candidates", []):
            if candidate.get("supports_chart") and candidate.get("live_reference"):
                return dict(candidate["live_reference"]), workbook_playbook

    profile = company.get("catalog_profile") or find_equity_company_profile(company.get("ticker", ""))
    if profile and profile.get("default_reference"):
        return dict(profile["default_reference"]), workbook_playbook
    return get_reference_preset("ibex_35"), workbook_playbook


def build_virtual_ibex_position(
    company: dict,
    reference_choice: dict,
    broker_profile: dict,
    latest_price: Decimal,
) -> EquityPosition:
    dividend_yield_pct = company.get("dividend_yield") or ZERO
    annual_dividend_income = (latest_price * dividend_yield_pct) / ONE_HUNDRED if dividend_yield_pct else ZERO
    return EquityPosition(
        position_kind=EquityPosition.PositionKind.WATCHLIST,
        ownership_category=broker_profile["ownership_category"],
        broker=broker_profile["broker"],
        ticker=clean_ticker(company.get("ticker", "")),
        quote_symbol=company.get("quote_symbol", ""),
        reference_profile=reference_choice["reference_profile"],
        benchmark_symbol=reference_choice["benchmark_symbol"],
        benchmark_name=reference_choice["benchmark_name"],
        company_name=company.get("company_name") or clean_ticker(company.get("ticker", "")),
        trade_channel=broker_profile["trade_channel"],
        shares=Decimal("1.0000"),
        average_cost_per_share=latest_price,
        current_price_per_share=latest_price,
        annual_dividend_income=quantize_decimal(annual_dividend_income, "0.01") or ZERO,
        annual_maintenance_cost=ZERO,
        latest_price_date=None,
        notes="Radar IBEX automatico",
    )


def find_ibex_universe_company(query: str, workbook_snapshot: dict | None = None) -> tuple[dict | None, dict]:
    workbook_snapshot = workbook_snapshot or load_ibex_reference_workbook_snapshot()
    normalized_query = normalize_company_lookup(query)
    if not normalized_query:
        return None, workbook_snapshot

    for company in build_ibex_universe_companies(workbook_snapshot):
        lookup_keys = build_security_lookup_keys(
            ticker=company.get("ticker", ""),
            company_name=company.get("company_name", ""),
            quote_symbol=company.get("quote_symbol", ""),
        )
        if normalized_query in lookup_keys:
            return company, workbook_snapshot
    return None, workbook_snapshot


def build_ibex_universe_card(
    company: dict,
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    reference_cache: dict | None = None,
    workbook_snapshot: dict | None = None,
    broker_profile: dict | None = None,
) -> dict:
    reference_cache = reference_cache if reference_cache is not None else {}
    workbook_snapshot = workbook_snapshot or load_ibex_reference_workbook_snapshot()
    broker_profile = broker_profile or resolve_analysis_broker_profile(positions)

    quote_symbol = company.get("quote_symbol", "")
    if not quote_symbol:
        raise ValueError(f"{company.get('company_name') or company.get('ticker')}: sin simbolo de cotizacion")

    stock_series = fetch_market_series(quote_symbol)
    reference_choice, workbook_playbook = resolve_ibex_reference_choice(company, workbook_snapshot)
    reference_series = fetch_reference_series_for_choice(
        reference_choice["reference_profile"],
        benchmark_symbol=reference_choice["benchmark_symbol"],
        benchmark_name=reference_choice["benchmark_name"],
    )
    benchmark_map = align_reference_points(stock_series.points, reference_series.points)
    position = build_virtual_ibex_position(company, reference_choice, broker_profile, stock_series.latest_price)
    position.latest_price_date = stock_series.latest_date
    position.last_synced_at = django_timezone.now()
    history = [
        EquityPriceHistory(
            position=position,
            price_date=point["date"],
            open_price=point.get("open"),
            high_price=point.get("high"),
            low_price=point.get("low"),
            close_price=point["close"],
            benchmark_close=benchmark_map.get(point["date"]),
        )
        for point in stock_series.points
    ]
    card = build_equity_history_card(
        position,
        history,
        reference_cache,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
        status_key="ibex",
        status_label="Radar IBEX",
        detail_anchor="",
        sector_label=company.get("sector", ""),
    )
    if workbook_playbook and workbook_playbook.get("available"):
        card["reference_playbook"] = build_workbook_reference_playbook(company, workbook_snapshot, card=card)
    card["ibex_company"] = company
    return card


def build_ibex_universe_analysis(
    tracked_history_cards: list[dict],
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    reference_cache: dict | None = None,
    company_limit: int | None = None,
) -> dict:
    reference_cache = reference_cache if reference_cache is not None else {}
    workbook_snapshot = load_ibex_reference_workbook_snapshot()
    companies = build_ibex_universe_companies(workbook_snapshot)
    tracked_keys = set()
    for card in tracked_history_cards:
        position = card["position"]
        tracked_keys.update(
            build_security_lookup_keys(
                ticker=position.ticker,
                company_name=position.company_name,
                quote_symbol=position.quote_symbol,
            )
        )

    broker_profile = resolve_analysis_broker_profile(positions)
    cards = []
    failures = []
    target_count = 0

    for company in companies:
        company_keys = build_security_lookup_keys(
            ticker=company.get("ticker", ""),
            company_name=company.get("company_name", ""),
            quote_symbol=company.get("quote_symbol", ""),
        )
        if tracked_keys & company_keys:
            continue
        if company_limit is not None and len(cards) >= company_limit:
            break
        quote_symbol = company.get("quote_symbol", "")
        if not quote_symbol:
            failures.append(f"{company.get('company_name') or company.get('ticker')}: sin simbolo de cotizacion")
            continue

        target_count += 1
        try:
            card = build_ibex_universe_card(
                company,
                positions,
                selected_start_date=selected_start_date,
                selected_end_date=selected_end_date,
                reference_cache=reference_cache,
                workbook_snapshot=workbook_snapshot,
                broker_profile=broker_profile,
            )
            cards.append(card)
        except Exception as exc:
            failures.append(f"{company.get('company_name') or company.get('ticker')}: {exc}")

    rows = build_equity_decision_rows(cards)
    buy_alert_count = sum(1 for card in cards if card.get("trade_alert", {}).get("label") == "Comprar")
    sell_alert_count = sum(1 for card in cards if card.get("trade_alert", {}).get("label") == "Vender")

    return {
        "cards": cards,
        "rows": rows,
        "summary": {
            "available": bool(cards),
            "workbook_loaded": workbook_snapshot.get("available", False),
            "source_label": Path(workbook_snapshot["path"]).name if workbook_snapshot.get("path") else "Catalogo IBEX",
            "analyzed_count": len(cards),
            "target_count": target_count,
            "buy_alert_count": buy_alert_count,
            "sell_alert_count": sell_alert_count,
            "watch_alert_count": max(len(cards) - buy_alert_count - sell_alert_count, 0),
            "failed_count": len(failures),
            "failures": failures[:8],
            "broker_assumption": broker_profile["broker"],
            "trade_channel_label": broker_profile["trade_channel_label"],
            "top_pick": rows[0] if rows else None,
        },
    }


def build_equity_analysis_dashboard(
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    include_ibex_universe: bool = False,
    ibex_company_limit: int | None = None,
) -> dict:
    reference_cache: dict = {}
    history_cards = build_equity_history_cards(
        positions,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
        reference_cache=reference_cache,
    )
    owned_positions = [position for position in positions if position.is_owned]
    watchlist_positions = [position for position in positions if not position.is_owned]
    current_value_total = sum((position.current_value for position in owned_positions), ZERO)
    invested_amount_total = sum((position.invested_amount for position in owned_positions), ZERO)
    annual_dividends_total = sum((position.annual_dividend_income for position in owned_positions), ZERO)
    net_dividends_total = sum((position.net_dividend_income for position in owned_positions), ZERO)
    annual_maintenance_total = sum((position.recurring_cost_used for position in owned_positions), ZERO)
    purchase_cost_total = sum((position.purchase_total_cost for position in owned_positions), ZERO)
    net_annual_income_total = sum((position.net_annual_income for position in owned_positions), ZERO)
    unrealized_gain_total = sum((position.unrealized_gain_after_costs for position in owned_positions), ZERO)
    reference_guide = build_equity_reference_guide(history_cards)
    decision_rows = build_equity_decision_rows(history_cards)
    ibex_universe = {
        "cards": [],
        "rows": [],
        "summary": {
            "available": False,
            "analyzed_count": 0,
            "buy_alert_count": 0,
            "sell_alert_count": 0,
            "watch_alert_count": 0,
            "failed_count": 0,
            "failures": [],
            "broker_assumption": "",
            "trade_channel_label": "",
            "top_pick": None,
        },
    }
    if include_ibex_universe:
        ibex_universe = build_ibex_universe_analysis(
            history_cards,
            positions,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
            reference_cache=reference_cache,
            company_limit=ibex_company_limit,
        )

    weighted_periods = []
    for label in ("1Y", "3Y", "5Y", "10Y"):
        weighted_stock = ZERO
        weighted_benchmark = ZERO
        weight_total = ZERO
        for card in history_cards:
            if not card["position"].is_owned:
                continue
            snapshot = next((item for item in card.get("period_snapshots", []) if item["label"] == label), None)
            if not snapshot or not snapshot["available"] or snapshot["stock_return_pct"] is None:
                continue
            weight = card["position"].current_value
            weighted_stock += snapshot["stock_return_pct"] * weight
            if snapshot["benchmark_return_pct"] is not None:
                weighted_benchmark += snapshot["benchmark_return_pct"] * weight
            weight_total += weight
        weighted_periods.append(
            {
                "label": label,
                "stock_return_pct": (weighted_stock / weight_total) if weight_total else None,
                "benchmark_return_pct": (weighted_benchmark / weight_total) if weight_total else None,
            }
        )

    latest_sync_at = max((position.last_synced_at for position in positions if position.last_synced_at), default=None)
    latest_price_date = max((position.latest_price_date for position in positions if position.latest_price_date), default=None)
    selected_cards = [
        card
        for card in history_cards
        if card["position"].is_owned and card.get("selected_period", {}).get("available")
    ]
    weighted_selected_return = None
    if selected_cards:
        numerator = sum(
            (card["selected_period"]["stock_return_pct"] or ZERO) * card["position"].current_value
            for card in selected_cards
        )
        denominator = sum((card["position"].current_value for card in selected_cards), ZERO)
        if denominator:
            weighted_selected_return = numerator / denominator

    if selected_start_date and selected_end_date:
        selected_period_label = f"{selected_start_date:%Y-%m-%d} a {selected_end_date:%Y-%m-%d}"
    elif selected_start_date:
        selected_period_label = f"Desde {selected_start_date:%Y-%m-%d}"
    elif selected_end_date:
        selected_period_label = f"Hasta {selected_end_date:%Y-%m-%d}"
    else:
        selected_period_label = "Ultimos 90 dias"

    weighted_projected_return_12m = None
    weighted_safety_score = None
    owned_projection_cards = [
        card
        for card in history_cards
        if card["position"].is_owned and card.get("projection", {}).get("available")
    ]
    if owned_projection_cards:
        projection_weight_total = sum((card["position"].current_value for card in owned_projection_cards), ZERO)
        if projection_weight_total:
            weighted_projected_return_12m = sum(
                (card["projection"].get("base_return_pct") or ZERO) * card["position"].current_value
                for card in owned_projection_cards
            ) / projection_weight_total
            weighted_safety_score = sum(
                (card["projection"].get("safety_score") or ZERO) * card["position"].current_value
                for card in owned_projection_cards
            ) / projection_weight_total

    overview = {
        "positions_count": len(positions),
        "owned_positions_count": len(owned_positions),
        "watchlist_positions_count": len(watchlist_positions),
        "invested_amount": invested_amount_total,
        "current_value": current_value_total,
        "annual_dividends_total": annual_dividends_total,
        "net_dividends_total": net_dividends_total,
        "annual_maintenance_total": annual_maintenance_total,
        "purchase_cost_total": purchase_cost_total,
        "net_annual_income_total": net_annual_income_total,
        "unrealized_gain_total": unrealized_gain_total,
        "unrealized_return_pct": (
            (unrealized_gain_total / (invested_amount_total + purchase_cost_total)) * ONE_HUNDRED
            if invested_amount_total + purchase_cost_total
            else None
        ),
        "latest_sync_at": latest_sync_at,
        "latest_price_date": latest_price_date,
        "weighted_selected_return": weighted_selected_return,
        "weighted_periods": weighted_periods,
        "weighted_projected_return_12m": weighted_projected_return_12m,
        "weighted_safety_score": weighted_safety_score,
        "selected_period_label": selected_period_label,
        "watchlist_latest_price_count": sum(1 for position in watchlist_positions if position.current_price_per_share),
        "best_decision": decision_rows[0] if decision_rows else ibex_universe["summary"].get("top_pick"),
    }

    return {
        "overview": overview,
        "history_cards": history_cards,
        "owned_positions": owned_positions,
        "watchlist_positions": watchlist_positions,
        "owned_history_cards": [card for card in history_cards if card["position"].is_owned],
        "watchlist_history_cards": [card for card in history_cards if not card["position"].is_owned],
        "decision_rows": decision_rows,
        "ibex_universe_cards": ibex_universe["cards"],
        "ibex_universe_rows": ibex_universe["rows"],
        "ibex_universe_summary": ibex_universe["summary"],
        "optimizer_cards": [*history_cards, *ibex_universe["cards"]],
        "reference_guide_rows": reference_guide["rows"],
        "tracked_reference_rows": reference_guide["tracked_rows"],
        "reference_guide_summary": reference_guide["summary"],
    }


def projection_confidence_multiplier(confidence_label: str) -> Decimal:
    if confidence_label == "Alta":
        return Decimal("1.00")
    if confidence_label == "Media":
        return Decimal("0.85")
    return Decimal("0.70")


def projection_reliability_score(label: str) -> Decimal:
    score_map = {
        "Alta": Decimal("82.00"),
        "Media": Decimal("64.00"),
        "Baja": Decimal("42.00"),
        "Sin historico suficiente": Decimal("45.00"),
    }
    return score_map.get(label or "Baja", Decimal("42.00"))


def build_equity_optimizer_candidate(card: dict) -> dict | None:
    projection = card.get("projection") or {}
    if not projection.get("available") or projection.get("projected_price") is None:
        return None

    base_return_pct = projection.get("base_return_pct")
    if base_return_pct is None:
        return None

    confidence_label = projection.get("confidence_label", "Baja")
    reliability = card.get("projection_reliability") or {}
    reliability_label = reliability.get("label") or confidence_label
    reliability_score = reliability.get("score") or projection_reliability_score(reliability_label)
    safety_score = projection.get("safety_score") or Decimal("60.00")
    years_covered = projection.get("years_covered") or ZERO
    positive_year_ratio_pct = projection.get("positive_year_ratio_pct") or Decimal("50.00")
    annualized_volatility_pct = projection.get("annualized_volatility_pct") or Decimal("18.00")
    low_return_pct = projection.get("low_return_pct")
    current_drawdown_pct = projection.get("current_drawdown_pct") or ZERO
    max_drawdown_pct = projection.get("max_drawdown_pct") or ZERO
    net_income_yield_pct = projection.get("net_income_yield_pct") or ZERO
    gross_dividend_yield_pct = projection.get("gross_dividend_yield_pct") or ZERO
    transaction_drag_pct = projection.get("transaction_drag_pct") or ZERO
    trade_alert = card.get("trade_alert") or {}
    trade_alert_label = trade_alert.get("label") or "Vigilar"
    trade_signal_adjustment = {
        "Comprar": Decimal("1.75"),
        "Vigilar": ZERO,
        "Vender": Decimal("-4.50"),
    }.get(trade_alert_label, ZERO)

    downside_return_pct = ZERO
    if low_return_pct is not None and low_return_pct < ZERO:
        downside_return_pct = abs(low_return_pct)

    history_depth_multiplier = clamp_decimal(
        (years_covered / Decimal(str(LONG_ANALYSIS_YEARS))) if years_covered else Decimal("0.55"),
        Decimal("0.55"),
        Decimal("1.00"),
    )
    consistency_multiplier = clamp_decimal(
        Decimal("0.75") + ((positive_year_ratio_pct - Decimal("50.00")) / Decimal("100")),
        Decimal("0.70"),
        Decimal("1.10"),
    )
    confidence_multiplier = projection_confidence_multiplier(confidence_label)
    quality_multiplier = (
        confidence_multiplier
        * (Decimal("0.55") + (safety_score / Decimal("200")))
        * (Decimal("0.55") + (reliability_score / Decimal("200")))
        * history_depth_multiplier
        * consistency_multiplier
    )

    risk_penalty_pct = (
        (downside_return_pct * Decimal("0.32"))
        + (annualized_volatility_pct * Decimal("0.08"))
        + (abs(current_drawdown_pct) * Decimal("0.05") if current_drawdown_pct < ZERO else ZERO)
        + (abs(max_drawdown_pct) * Decimal("0.02") if max_drawdown_pct < ZERO else ZERO)
    )
    income_support_bonus_pct = clamp_decimal(net_income_yield_pct * Decimal("0.30"), Decimal("-2.00"), Decimal("3.00"))
    cost_efficiency_penalty_pct = clamp_decimal(transaction_drag_pct * Decimal("0.40"), ZERO, Decimal("3.00"))
    risk_adjusted_return_pct = (base_return_pct * quality_multiplier) - risk_penalty_pct
    optimization_score = risk_adjusted_return_pct + income_support_bonus_pct - cost_efficiency_penalty_pct + trade_signal_adjustment

    return {
        "card": card,
        "position": card["position"],
        "projection": projection,
        "status_key": card.get("status_key") or ("owned" if card["position"].is_owned else "watchlist"),
        "status_label": card.get("status_label") or card["position"].get_position_kind_display(),
        "sector_label": card.get("sector_label") or resolve_equity_sector_label(
            company_name=card["position"].company_name,
            ticker=card["position"].ticker,
            quote_symbol=card["position"].quote_symbol,
        ) or "Sin sector",
        "reference_label": card["reference_label"],
        "confidence_label": confidence_label,
        "reliability_label": reliability_label,
        "reliability_score": reliability_score,
        "safety_score": safety_score,
        "base_return_pct": base_return_pct,
        "risk_adjusted_return_pct": risk_adjusted_return_pct,
        "optimization_score": optimization_score,
        "downside_return_pct": downside_return_pct,
        "annualized_volatility_pct": annualized_volatility_pct,
        "gross_dividend_yield_pct": gross_dividend_yield_pct,
        "net_income_yield_pct": net_income_yield_pct,
        "transaction_drag_pct": transaction_drag_pct,
        "years_covered": years_covered,
        "cycle_phase": projection.get("cycle_phase") or "Sin ciclo",
        "max_drawdown_pct": max_drawdown_pct,
        "current_drawdown_pct": current_drawdown_pct,
        "trade_alert_label": trade_alert_label,
        "trade_alert_tone": trade_alert.get("tone", "watch"),
        "trade_alert_note": trade_alert.get("note", ""),
        "trade_signal_adjustment": trade_signal_adjustment,
    }


def filter_optimizer_candidates_by_sector(
    candidates: list[dict],
    max_sector_positions: int,
) -> tuple[list[dict], list[dict]]:
    if max_sector_positions <= 0:
        return candidates, []

    sector_counts: dict[str, int] = {}
    accepted = []
    excluded = []
    for candidate in candidates:
        sector_label = candidate.get("sector_label") or "Sin sector"
        current_count = sector_counts.get(sector_label, 0)
        if current_count >= max_sector_positions:
            excluded.append(candidate)
            continue
        sector_counts[sector_label] = current_count + 1
        accepted.append(candidate)
    return accepted, excluded


def distribute_optimizer_amounts(
    candidates: list[dict],
    total_investment: Decimal,
    company_cap_amount: Decimal,
) -> tuple[list[Decimal], Decimal]:
    if not candidates:
        return [], total_investment

    epsilon = Decimal("0.01")
    allocations = [ZERO for _ in candidates]
    open_indexes = list(range(len(candidates)))
    remaining_amount = total_investment

    while remaining_amount > epsilon and open_indexes:
        score_total = sum((candidates[index]["optimization_score"] for index in open_indexes), ZERO)
        if score_total <= ZERO:
            break

        distributed_amount = ZERO
        next_open_indexes = []
        for index in open_indexes:
            capacity = company_cap_amount - allocations[index]
            if capacity <= epsilon:
                continue
            proportional_amount = remaining_amount * (candidates[index]["optimization_score"] / score_total)
            allocated_amount = min(capacity, proportional_amount)
            if allocated_amount <= ZERO:
                continue
            allocations[index] += allocated_amount
            distributed_amount += allocated_amount
            if company_cap_amount - allocations[index] > epsilon:
                next_open_indexes.append(index)

        if distributed_amount <= epsilon:
            break
        remaining_amount -= distributed_amount
        open_indexes = next_open_indexes

    quantized_allocations = [quantize_decimal(amount, "0.01") or ZERO for amount in allocations]
    allocated_total = sum(quantized_allocations, ZERO)
    remaining_amount = quantize_decimal(total_investment - allocated_total, "0.01") or ZERO

    if remaining_amount > ZERO:
        for index, amount in enumerate(quantized_allocations):
            capacity = company_cap_amount - amount
            top_up = min(capacity, remaining_amount)
            if top_up <= ZERO:
                continue
            quantized_allocations[index] = amount + top_up
            remaining_amount -= top_up
            if remaining_amount <= ZERO:
                break

    return quantized_allocations, remaining_amount


def build_optimizer_allocation_scenario(candidate: dict, allocated_amount: Decimal) -> dict:
    position = candidate["position"]
    projection = candidate["projection"]
    if allocated_amount <= ZERO:
        return {
            "gross_dividend_income": ZERO,
            "net_dividend_income": ZERO,
            "annual_cost_used": ZERO,
            "roundtrip_total_cost": ZERO,
            "transaction_drag_pct": ZERO,
            "net_income_yield_pct": ZERO,
            "net_projected_return_pct": ZERO,
            "low_return_pct": ZERO,
            "high_return_pct": ZERO,
            "projected_gain_amount": ZERO,
            "downside_amount": ZERO,
        }

    gross_dividend_yield_pct = projection.get("gross_dividend_yield_pct") or ZERO
    gross_dividend_income = (allocated_amount * gross_dividend_yield_pct) / ONE_HUNDRED
    broker_costs = estimate_broker_costs(
        broker_name=position.broker,
        trade_channel=position.trade_channel,
        trade_amount=allocated_amount,
        valuation_amount=allocated_amount,
        annual_dividend_income=gross_dividend_income,
        quote_symbol=position.quote_symbol,
    )
    annual_cost_used, annual_cost_source = resolve_recurring_cost_used(
        position.annual_maintenance_cost,
        broker_costs.get("annual_recurring_cost", ZERO),
    )
    net_dividend_income = gross_dividend_income - broker_costs.get("annual_dividend_fee", ZERO)
    net_annual_income = net_dividend_income - annual_cost_used
    net_income_yield_pct = (net_annual_income / allocated_amount) * ONE_HUNDRED if allocated_amount else ZERO
    transaction_drag_pct = (
        (broker_costs.get("roundtrip_total_cost", ZERO) / allocated_amount) * ONE_HUNDRED
        if allocated_amount
        else ZERO
    )
    price_return_pct = projection.get("price_return_pct")
    if price_return_pct is None:
        fallback_base_return_pct = projection.get("base_return_pct") or ZERO
        fallback_net_income_yield_pct = projection.get("net_income_yield_pct") or ZERO
        fallback_transaction_drag_pct = projection.get("transaction_drag_pct") or ZERO
        price_return_pct = fallback_base_return_pct - fallback_net_income_yield_pct + fallback_transaction_drag_pct
    price_low_return_pct = projection.get("price_low_return_pct")
    if price_low_return_pct is None:
        price_low_return_pct = projection.get("low_return_pct")
    if price_low_return_pct is None:
        price_low_return_pct = price_return_pct
    price_high_return_pct = projection.get("price_high_return_pct")
    if price_high_return_pct is None:
        price_high_return_pct = projection.get("high_return_pct")
    if price_high_return_pct is None:
        price_high_return_pct = price_return_pct
    net_projected_return_pct = clamp_decimal(
        price_return_pct + net_income_yield_pct - transaction_drag_pct,
        Decimal("-45.00"),
        Decimal("45.00"),
    )
    low_return_pct = clamp_decimal(
        price_low_return_pct + net_income_yield_pct - transaction_drag_pct,
        Decimal("-80.00"),
        Decimal("120.00"),
    )
    high_return_pct = clamp_decimal(
        price_high_return_pct + net_income_yield_pct - transaction_drag_pct,
        Decimal("-80.00"),
        Decimal("140.00"),
    )
    projected_gain_amount = (allocated_amount * net_projected_return_pct) / ONE_HUNDRED
    downside_amount = (allocated_amount * low_return_pct) / ONE_HUNDRED

    return {
        "gross_dividend_income": quantize_decimal(gross_dividend_income, "0.01") or ZERO,
        "net_dividend_income": quantize_decimal(net_dividend_income, "0.01") or ZERO,
        "annual_cost_used": quantize_decimal(annual_cost_used, "0.01") or ZERO,
        "annual_cost_source": annual_cost_source,
        "roundtrip_total_cost": quantize_decimal(broker_costs.get("roundtrip_total_cost", ZERO), "0.01") or ZERO,
        "purchase_total_cost": quantize_decimal(broker_costs.get("purchase_total_cost", ZERO), "0.01") or ZERO,
        "annual_dividend_fee": quantize_decimal(broker_costs.get("annual_dividend_fee", ZERO), "0.01") or ZERO,
        "transaction_drag_pct": quantize_decimal(transaction_drag_pct) or ZERO,
        "net_income_yield_pct": quantize_decimal(net_income_yield_pct) or ZERO,
        "net_projected_return_pct": quantize_decimal(net_projected_return_pct) or ZERO,
        "low_return_pct": quantize_decimal(low_return_pct) or ZERO,
        "high_return_pct": quantize_decimal(high_return_pct) or ZERO,
        "projected_gain_amount": quantize_decimal(projected_gain_amount, "0.01") or ZERO,
        "downside_amount": quantize_decimal(downside_amount, "0.01") or ZERO,
    }


def review_optimizer_ticket_efficiency(candidate: dict, allocated_amount: Decimal, scenario: dict) -> dict:
    if allocated_amount <= ZERO:
        return {
            "keep": False,
            "reason": "sin_asignacion",
            "entry_drag_pct": None,
            "roundtrip_drag_pct": None,
            "gain_to_roundtrip_multiple": None,
        }

    purchase_total_cost = scenario.get("purchase_total_cost", ZERO)
    roundtrip_total_cost = scenario.get("roundtrip_total_cost", ZERO)
    projected_gain_amount = scenario.get("projected_gain_amount", ZERO)
    entry_drag_pct = (
        (purchase_total_cost / allocated_amount) * ONE_HUNDRED
        if allocated_amount and purchase_total_cost > ZERO
        else ZERO
    )
    roundtrip_drag_pct = (
        (roundtrip_total_cost / allocated_amount) * ONE_HUNDRED
        if allocated_amount and roundtrip_total_cost > ZERO
        else ZERO
    )
    gain_to_roundtrip_multiple = (
        (projected_gain_amount / roundtrip_total_cost)
        if roundtrip_total_cost > ZERO and projected_gain_amount is not None
        else None
    )

    if purchase_total_cost <= ZERO and roundtrip_total_cost <= ZERO:
        return {
            "keep": True,
            "reason": "",
            "entry_drag_pct": entry_drag_pct,
            "roundtrip_drag_pct": roundtrip_drag_pct,
            "gain_to_roundtrip_multiple": gain_to_roundtrip_multiple,
        }

    if entry_drag_pct > OPTIMIZER_MAX_ENTRY_DRAG_PCT:
        return {
            "keep": False,
            "reason": "entry_drag",
            "entry_drag_pct": entry_drag_pct,
            "roundtrip_drag_pct": roundtrip_drag_pct,
            "gain_to_roundtrip_multiple": gain_to_roundtrip_multiple,
        }

    if roundtrip_drag_pct > OPTIMIZER_MAX_ROUNDTRIP_DRAG_PCT:
        return {
            "keep": False,
            "reason": "roundtrip_drag",
            "entry_drag_pct": entry_drag_pct,
            "roundtrip_drag_pct": roundtrip_drag_pct,
            "gain_to_roundtrip_multiple": gain_to_roundtrip_multiple,
        }

    if (
        gain_to_roundtrip_multiple is not None
        and projected_gain_amount > ZERO
        and gain_to_roundtrip_multiple < OPTIMIZER_MIN_GAIN_TO_ROUNDTRIP_MULTIPLE
    ):
        return {
            "keep": False,
            "reason": "gain_vs_cost",
            "entry_drag_pct": entry_drag_pct,
            "roundtrip_drag_pct": roundtrip_drag_pct,
            "gain_to_roundtrip_multiple": gain_to_roundtrip_multiple,
        }

    return {
        "keep": True,
        "reason": "",
        "entry_drag_pct": entry_drag_pct,
        "roundtrip_drag_pct": roundtrip_drag_pct,
        "gain_to_roundtrip_multiple": gain_to_roundtrip_multiple,
    }


def build_equity_allocation_plan(
    history_cards: list[dict],
    total_investment: Decimal,
    max_company_pct: Decimal,
    max_sector_positions: int = 0,
) -> dict:
    if total_investment <= 0 or max_company_pct <= 0:
        return {
            "available": False,
            "reason": "Parametros insuficientes para calcular la propuesta.",
        }

    company_cap_amount = (total_investment * max_company_pct) / Decimal("100")
    candidates = [candidate for candidate in (build_equity_optimizer_candidate(card) for card in history_cards) if candidate]

    if not candidates:
        return {
            "available": False,
            "reason": "Todavia no hay suficientes proyecciones para proponer una distribucion a 12M.",
        }

    positive_candidates = [
        item
        for item in candidates
        if item["optimization_score"] > ZERO
        and item["base_return_pct"] > ZERO
        and item["trade_alert_label"] != "Vender"
    ]
    if not positive_candidates:
        return {
            "available": False,
            "reason": "Ahora mismo ninguna accion supera el filtro de retorno neto y riesgo para una optimizacion equilibrada a 12M.",
        }

    ranked_candidates = sorted(
        positive_candidates,
        key=lambda item: (
            item["optimization_score"],
            item["risk_adjusted_return_pct"],
            item["base_return_pct"],
            item["safety_score"],
        ),
        reverse=True,
    )
    sector_excluded_candidates = []
    if max_sector_positions > 0:
        ranked_candidates, sector_excluded_candidates = filter_optimizer_candidates_by_sector(
            ranked_candidates,
            max_sector_positions,
        )

    if not ranked_candidates:
        return {
            "available": False,
            "reason": "Con el limite por sector indicado no quedan suficientes candidatos validos para construir la propuesta.",
        }

    ticket_filtered_count = 0
    ticket_filter_reasons = {"entry_drag": 0, "roundtrip_drag": 0, "gain_vs_cost": 0}
    iteration_candidates = ranked_candidates
    remaining_amount = total_investment
    allocated_amounts = []
    kept_scenarios = []
    max_iterations = len(ranked_candidates) + 1

    for _ in range(max_iterations):
        allocated_amounts, remaining_amount = distribute_optimizer_amounts(
            iteration_candidates,
            total_investment,
            company_cap_amount,
        )
        rejected_reviews = []
        kept_scenarios = []
        for index, (candidate, allocated_amount) in enumerate(zip(iteration_candidates, allocated_amounts)):
            if allocated_amount <= ZERO:
                kept_scenarios.append(None)
                continue
            scenario = build_optimizer_allocation_scenario(candidate, allocated_amount)
            review = review_optimizer_ticket_efficiency(candidate, allocated_amount, scenario)
            scenario["entry_drag_pct"] = quantize_decimal(review.get("entry_drag_pct")) or ZERO
            scenario["roundtrip_drag_pct"] = quantize_decimal(review.get("roundtrip_drag_pct")) or ZERO
            scenario["gain_to_roundtrip_multiple"] = quantize_decimal(review.get("gain_to_roundtrip_multiple")) if review.get("gain_to_roundtrip_multiple") is not None else None
            if not review["keep"]:
                rejected_reviews.append(
                    {
                        "index": index,
                        "reason": review["reason"],
                        "entry_drag_pct": review.get("entry_drag_pct") or ZERO,
                        "roundtrip_drag_pct": review.get("roundtrip_drag_pct") or ZERO,
                        "gain_to_roundtrip_multiple": review.get("gain_to_roundtrip_multiple"),
                        "allocated_amount": allocated_amount,
                    }
                )
                kept_scenarios.append(None)
                continue
            kept_scenarios.append(scenario)

        if not rejected_reviews:
            break
        rejected_reviews.sort(
            key=lambda item: (
                item["roundtrip_drag_pct"],
                item["entry_drag_pct"],
                -(item["gain_to_roundtrip_multiple"] if item["gain_to_roundtrip_multiple"] is not None else Decimal("999")),
                -item["allocated_amount"],
            ),
            reverse=True,
        )
        rejected_index = rejected_reviews[0]["index"]
        rejected_reason = rejected_reviews[0]["reason"]
        ticket_filtered_count += 1
        if rejected_reason in ticket_filter_reasons:
            ticket_filter_reasons[rejected_reason] += 1
        iteration_candidates = [
            candidate
            for index, candidate in enumerate(iteration_candidates)
            if index != rejected_index
        ]
        if not iteration_candidates:
            break

    ranked_candidates = iteration_candidates
    allocations = []
    for rank, (candidate, allocated_amount, scenario) in enumerate(zip(ranked_candidates, allocated_amounts, kept_scenarios), start=1):
        if allocated_amount <= ZERO:
            continue
        if scenario is None:
            scenario = build_optimizer_allocation_scenario(candidate, allocated_amount)
        allocations.append(
            {
                "rank": rank,
                "position": candidate["position"],
                "status_key": candidate["status_key"],
                "status_label": candidate["status_label"],
                "sector_label": candidate["sector_label"],
                "reference_label": candidate["reference_label"],
                "allocated_amount": allocated_amount,
                "allocated_weight_pct": (allocated_amount / total_investment) * Decimal("100"),
                "base_return_pct": candidate["base_return_pct"],
                "adjusted_return_pct": candidate["risk_adjusted_return_pct"],
                "net_projected_return_pct": scenario["net_projected_return_pct"],
                "low_return_pct": scenario["low_return_pct"],
                "high_return_pct": scenario["high_return_pct"],
                "projected_gain_amount": scenario["projected_gain_amount"],
                "downside_amount": scenario["downside_amount"],
                "expected_net_dividend_income": scenario["net_dividend_income"],
                "expected_gross_dividend_income": scenario["gross_dividend_income"],
                "annual_cost_used": scenario["annual_cost_used"],
                "roundtrip_total_cost": scenario["roundtrip_total_cost"],
                "purchase_total_cost": scenario["purchase_total_cost"],
                "entry_drag_pct": scenario.get("entry_drag_pct", ZERO),
                "roundtrip_drag_pct": scenario.get("roundtrip_drag_pct", ZERO),
                "gain_to_roundtrip_multiple": scenario.get("gain_to_roundtrip_multiple"),
                "transaction_drag_pct": scenario["transaction_drag_pct"],
                "net_income_yield_pct": scenario["net_income_yield_pct"],
                "confidence_label": candidate["confidence_label"],
                "reliability_label": candidate["reliability_label"],
                "reliability_score": candidate["reliability_score"],
                "safety_score": candidate["safety_score"],
                "optimization_score": candidate["optimization_score"],
                "trade_alert_label": candidate["trade_alert_label"],
                "trade_alert_tone": candidate["trade_alert_tone"],
                "annualized_volatility_pct": candidate["annualized_volatility_pct"],
                "years_covered": candidate["years_covered"],
                "cycle_phase": candidate["cycle_phase"],
                "max_drawdown_pct": candidate["max_drawdown_pct"],
                "current_drawdown_pct": candidate["current_drawdown_pct"],
                "projected_price": candidate["projection"].get("projected_price"),
            }
        )

    allocated_amount_total = sum((item["allocated_amount"] for item in allocations), ZERO)
    projected_gain_total = sum((item["projected_gain_amount"] for item in allocations), ZERO)
    weighted_return_pct = (
        (projected_gain_total / allocated_amount_total) * Decimal("100")
        if allocated_amount_total
        else None
    )
    weighted_low_return_pct = (
        (sum((item["downside_amount"] for item in allocations), ZERO) / allocated_amount_total) * Decimal("100")
        if allocated_amount_total
        else None
    )
    weighted_safety_score = (
        sum((item["safety_score"] * item["allocated_amount"] for item in allocations), ZERO) / allocated_amount_total
        if allocated_amount_total
        else None
    )
    weighted_reliability_score = (
        sum((item["reliability_score"] * item["allocated_amount"] for item in allocations), ZERO) / allocated_amount_total
        if allocated_amount_total
        else None
    )
    weighted_volatility_pct = (
        sum((item["annualized_volatility_pct"] * item["allocated_amount"] for item in allocations), ZERO) / allocated_amount_total
        if allocated_amount_total
        else None
    )
    net_dividend_income_total = sum((item["expected_net_dividend_income"] for item in allocations), ZERO)
    annual_cost_total = sum((item["annual_cost_used"] for item in allocations), ZERO)
    roundtrip_cost_total = sum((item["roundtrip_total_cost"] for item in allocations), ZERO)
    owned_allocations_count = sum(1 for item in allocations if item["position"].is_owned)
    ibex_allocations_count = sum(1 for item in allocations if item["status_key"] == "ibex")
    watchlist_allocations_count = sum(
        1
        for item in allocations
        if not item["position"].is_owned and item["status_key"] != "ibex"
    )
    sectors_used = sorted({item["sector_label"] for item in allocations if item.get("sector_label")})

    if remaining_amount > ZERO:
        reserve_reason = (
            "Queda caja en reserva porque el filtro de riesgo/rentabilidad solo deja pasar las ideas con retorno neto positivo, "
            "riesgo asumible y suficiente calidad historica."
        )
    else:
        reserve_reason = ""
    if ticket_filtered_count:
        ticket_filter_note = (
            "Se han descartado "
            f"{ticket_filtered_count} propuesta(s) porque el coste fijo+variable por operacion se comia demasiado retorno esperado."
        )
    else:
        ticket_filter_note = ""
    if max_sector_positions > 0:
        sector_limit_note = (
            f"Se aplica un maximo de {max_sector_positions} empresa(s) por sector."
        )
        if sector_excluded_candidates:
            sector_limit_note += f" {len(sector_excluded_candidates)} candidata(s) han quedado fuera por diversificacion sectorial."
    else:
        sector_limit_note = ""
    reason = ""
    if not allocations:
        reason = (
            ticket_filter_note
            or sector_limit_note
            or "Con las tarifas actuales del broker, las compras que caben por peso maximo no compensan el coste fijo+variable por operacion."
        )

    top_pick = allocations[0] if allocations else None
    return {
        "available": bool(allocations),
        "reason": reason,
        "allocations": allocations,
        "total_investment": total_investment,
        "max_company_pct": max_company_pct,
        "company_cap_amount": company_cap_amount,
        "allocated_amount_total": allocated_amount_total,
        "cash_reserve_amount": remaining_amount,
        "projected_gain_total": projected_gain_total,
        "weighted_return_pct": weighted_return_pct,
        "weighted_low_return_pct": weighted_low_return_pct,
        "weighted_safety_score": weighted_safety_score,
        "weighted_reliability_score": weighted_reliability_score,
        "weighted_volatility_pct": weighted_volatility_pct,
        "net_dividend_income_total": net_dividend_income_total,
        "annual_cost_total": annual_cost_total,
        "roundtrip_cost_total": roundtrip_cost_total,
        "owned_allocations_count": owned_allocations_count,
        "watchlist_allocations_count": watchlist_allocations_count,
        "ibex_allocations_count": ibex_allocations_count,
        "max_sector_positions": max_sector_positions,
        "sectors_used": sectors_used,
        "sectors_used_count": len(sectors_used),
        "sector_filtered_count": len(sector_excluded_candidates),
        "sector_filter_note": sector_limit_note,
        "ticket_filtered_count": ticket_filtered_count,
        "ticket_filter_reasons": ticket_filter_reasons,
        "ticket_filter_note": ticket_filter_note,
        "reserve_reason": reserve_reason,
        "top_pick": top_pick,
        "methodology_note": (
            "La optimizacion prioriza retorno neto esperado a 12 meses, dividendos netos, costes de compra/venta y mantenimiento, "
            "seguridad, fiabilidad del modelo, alertas de tendencia, riesgo historico, eficiencia real del ticket de compra "
            "y, si lo marcas, diversificacion maxima por sector."
        ),
    }
