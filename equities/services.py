from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .models import (
    EquityClosedPosition,
    EquityPosition,
    EquityPriceHistory,
    EquityPurchaseForecastBaseline,
    EquityTicketSnapshot,
)


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
COMPARABLE_MONTH_DAYS = Decimal("30.4375")
OPTIMIZER_MAX_ENTRY_DRAG_PCT = Decimal("1.00")
OPTIMIZER_MAX_ROUNDTRIP_DRAG_PCT = Decimal("2.00")
OPTIMIZER_MIN_GAIN_TO_ROUNDTRIP_MULTIPLE = Decimal("1.80")

logger = logging.getLogger(__name__)
DEFAULT_EQUITY_ANALYSIS_NOTIONAL = Decimal("10000.00")
DEFAULT_BENCHMARK_SYMBOL = "^IBEX"
DEFAULT_BENCHMARK_NAME = "IBEX 35"
DEFAULT_MARKET_REQUEST_TIMEOUT_SECONDS = 12
DEFAULT_MARKET_DATA_CACHE_MINUTES = 60
DEFAULT_IBEX_UNIVERSE_MAX_WORKERS = 8
SCHEDULED_REVIEW_WEEKDAY_LABELS = {
    1: "lunes",
    2: "martes",
    3: "miercoles",
    4: "jueves",
    5: "viernes",
    6: "sabado",
    7: "domingo",
}
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


def clone_market_series(series: MarketSeries | None) -> MarketSeries | None:
    if series is None:
        return None
    return MarketSeries(
        symbol=series.symbol,
        name=series.name,
        latest_price=series.latest_price,
        latest_date=series.latest_date,
        points=[dict(point) for point in series.points],
    )


def build_market_data_cache_bucket(now: datetime | None = None) -> int:
    cache_minutes = max(
        getattr(settings, "EQUITIES_MARKET_DATA_CACHE_MINUTES", DEFAULT_MARKET_DATA_CACHE_MINUTES) or DEFAULT_MARKET_DATA_CACHE_MINUTES,
        1,
    )
    current = now or django_timezone.now()
    return int(current.timestamp() // (cache_minutes * 60))


def market_request_timeout_seconds() -> int:
    return max(
        getattr(settings, "EQUITIES_MARKET_REQUEST_TIMEOUT_SECONDS", DEFAULT_MARKET_REQUEST_TIMEOUT_SECONDS)
        or DEFAULT_MARKET_REQUEST_TIMEOUT_SECONDS,
        3,
    )


def ibex_universe_max_workers() -> int:
    return max(
        getattr(settings, "EQUITIES_IBEX_UNIVERSE_MAX_WORKERS", DEFAULT_IBEX_UNIVERSE_MAX_WORKERS)
        or DEFAULT_IBEX_UNIVERSE_MAX_WORKERS,
        1,
    )


def clear_market_data_caches() -> None:
    _fetch_market_series_cached.cache_clear()
    _fetch_reference_series_for_choice_cached.cache_clear()
    _fetch_equity_fundamentals_cached.cache_clear()


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


def get_equity_optimizer_sector_choices() -> list[tuple[str, str]]:
    sectors = sorted(
        {
            str(entry.get("sector_label") or "").strip()
            for entry in get_equity_company_catalog()
            if str(entry.get("sector_label") or "").strip()
        }
    )
    return [(sector, sector) for sector in sectors]


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


@lru_cache(maxsize=512)
def _fetch_market_series_cached(
    symbol: str,
    range_key: str = DEFAULT_MARKET_RANGE_KEY,
    interval: str = "1d",
    cache_bucket: int = 0,
) -> MarketSeries:
    params = urlencode({"range": range_key, "interval": interval, "includePrePost": "false"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=market_request_timeout_seconds()) as response:
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


def fetch_market_series(symbol: str, range_key: str = DEFAULT_MARKET_RANGE_KEY, interval: str = "1d") -> MarketSeries:
    normalized_symbol = clean_symbol(symbol)
    return clone_market_series(
        _fetch_market_series_cached(
            normalized_symbol,
            range_key,
            interval,
            build_market_data_cache_bucket(),
        )
    )


def should_fetch_equity_fundamentals() -> bool:
    return bool(getattr(settings, "EQUITIES_FETCH_FUNDAMENTALS", True))


def build_fundamentals_period_bounds(now: datetime | None = None) -> tuple[int, int]:
    current = now or django_timezone.now()
    period2 = int(current.timestamp())
    period1 = int((current - timedelta(days=365 * 5)).timestamp())
    return period1, period2


def parse_yahoo_timeseries_metric_rows(rows: list[dict] | None) -> list[dict]:
    parsed_rows = []
    for row in rows or []:
        as_of_date_raw = row.get("asOfDate")
        reported_value = row.get("reportedValue", {})
        raw_value = reported_value.get("raw")
        if not as_of_date_raw or raw_value in {None, ""}:
            continue
        try:
            as_of_date = date.fromisoformat(as_of_date_raw)
            value = Decimal(str(raw_value))
        except Exception:
            continue
        parsed_rows.append(
            {
                "as_of_date": as_of_date,
                "period_type": row.get("periodType", ""),
                "currency_code": row.get("currencyCode", ""),
                "value": value,
            }
        )
    parsed_rows.sort(key=lambda item: item["as_of_date"])
    return parsed_rows


def clone_equity_fundamentals_snapshot(snapshot: dict) -> dict:
    return {
        **snapshot,
        "net_income_rows": [dict(row) for row in snapshot.get("net_income_rows", [])],
        "market_cap_rows": [dict(row) for row in snapshot.get("market_cap_rows", [])],
    }


@lru_cache(maxsize=256)
def _fetch_equity_fundamentals_cached(symbol: str, cache_bucket: int = 0) -> dict:
    period1, period2 = build_fundamentals_period_bounds()
    requested_types = ",".join(
        (
            "annualNetIncome",
            "trailingMarketCap",
            "annualMarketCap",
            "quarterlyMarketCap",
        )
    )
    url = (
        "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
        f"{symbol}?type={requested_types}&period1={period1}&period2={period2}"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=market_request_timeout_seconds()) as response:
        payload = json.load(response)

    timeseries = payload.get("timeseries", {})
    if timeseries.get("error"):
        description = timeseries["error"].get("description") or f"No se han podido cargar los fundamentales de {symbol}."
        raise MarketDataError(description)

    parsed_metrics = {}
    for item in timeseries.get("result", []):
        metric_keys = [key for key in item.keys() if key not in {"meta", "timestamp"}]
        for metric_key in metric_keys:
            parsed_metrics[metric_key] = parse_yahoo_timeseries_metric_rows(item.get(metric_key))

    net_income_rows = parsed_metrics.get("annualNetIncome", [])
    market_cap_rows = (
        parsed_metrics.get("trailingMarketCap")
        or parsed_metrics.get("quarterlyMarketCap")
        or parsed_metrics.get("annualMarketCap")
        or []
    )
    if not net_income_rows and not market_cap_rows:
        raise MarketDataError(f"No se han recibido fundamentales suficientes para {symbol}.")

    latest_market_cap = market_cap_rows[-1] if market_cap_rows else {}
    currency_code = (
        latest_market_cap.get("currency_code")
        or (net_income_rows[-1].get("currency_code") if net_income_rows else "")
        or ""
    )
    return {
        "symbol": symbol,
        "available": True,
        "currency_code": currency_code,
        "net_income_rows": net_income_rows[-3:],
        "market_cap_rows": market_cap_rows[-3:],
        "market_cap": latest_market_cap.get("value"),
        "market_cap_as_of_date": latest_market_cap.get("as_of_date"),
    }


def fetch_equity_fundamentals(symbol: str) -> dict:
    normalized_symbol = clean_symbol(symbol)
    return clone_equity_fundamentals_snapshot(
        _fetch_equity_fundamentals_cached(
            normalized_symbol,
            build_market_data_cache_bucket(),
        )
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


@lru_cache(maxsize=256)
def _fetch_reference_series_for_choice_cached(
    reference_profile: str,
    benchmark_symbol: str = "",
    benchmark_name: str = "",
    cache_bucket: int = 0,
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
        return _fetch_market_series_cached(
            clean_symbol(benchmark_symbol),
            DEFAULT_MARKET_RANGE_KEY,
            "1d",
            cache_bucket,
        )
    return None


def fetch_reference_series_for_choice(
    reference_profile: str,
    benchmark_symbol: str = "",
    benchmark_name: str = "",
) -> MarketSeries | None:
    return clone_market_series(
        _fetch_reference_series_for_choice_cached(
            reference_profile,
            benchmark_symbol,
            benchmark_name,
            build_market_data_cache_bucket(),
        )
    )


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


def build_time_axis_markers(
    point_dates: list[date],
    width: int = 640,
    height: int = 220,
    padding: int = 18,
) -> list[dict]:
    filtered_dates = sorted({point_date for point_date in point_dates if point_date is not None})
    if len(filtered_dates) < 2:
        return []

    min_date = filtered_dates[0]
    max_date = filtered_dates[-1]
    total_days = max((max_date - min_date).days, 1)
    span_x = width - (padding * 2)

    def to_x(point_date: date) -> float:
        return padding + (span_x * ((point_date - min_date).days / total_days))

    markers = []
    if min_date.year != max_date.year:
        for year in range(min_date.year, max_date.year + 1):
            marker_date = date(year, 1, 1)
            if marker_date < min_date or marker_date > max_date:
                continue
            markers.append(
                {
                    "x": f"{to_x(marker_date):.1f}",
                    "label": str(year),
                    "date": marker_date.isoformat(),
                    "y1": str(height - padding),
                    "y2": str(height - padding + 6),
                    "text_y": str(height - 2),
                }
            )

    if len(markers) < 2:
        fallback_dates = [min_date]
        if min_date.year != max_date.year:
            fallback_dates.append(max_date)
        else:
            midpoint = min_date + timedelta(days=total_days // 2)
            fallback_dates.extend([midpoint, max_date])

        seen_labels = set()
        markers = []
        for marker_date in fallback_dates:
            label = str(marker_date.year) if min_date.year != max_date.year else marker_date.strftime("%Y-%m")
            if label in seen_labels:
                continue
            seen_labels.add(label)
            markers.append(
                {
                    "x": f"{to_x(marker_date):.1f}",
                    "label": label,
                    "date": marker_date.isoformat(),
                    "y1": str(height - padding),
                    "y2": str(height - padding + 6),
                    "text_y": str(height - 2),
                }
            )

    return markers


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
            "x_markers": [],
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
        "x_markers": build_time_axis_markers(all_dates, width=width, height=height, padding=padding),
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
        "x_markers": chart.get("x_markers", []),
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
        "x_markers": chart.get("x_markers", []),
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
        start_date=latest_date - timedelta(days=365),
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
        "x_markers": chart.get("x_markers", []),
        "start_label": recent_history[0].price_date.isoformat(),
        "end_label": latest_date.isoformat(),
        "projection_end_label": end_projection_step.get("projected_date").isoformat() if end_projection_step and end_projection_step.get("projected_date") else "",
        "points_count": len(stock_series),
        "history_window_label": "Ultimo ano",
    }


def build_five_year_cycle_projection(
    history,
    position: EquityPosition,
    correlation: dict,
    cycle_metrics: dict | None = None,
) -> dict:
    if len(history) < 2:
        return {"available": False}

    latest_date = history[-1].price_date
    analysis_history = filter_history_window(
        history,
        start_date=latest_date - timedelta(days=LONG_ANALYSIS_DAYS),
        end_date=latest_date,
    )
    monthly_history = collapse_history_to_frequency(analysis_history, "monthly")
    if len(monthly_history) < 24:
        return {"available": False}

    cycle_metrics = cycle_metrics or build_cycle_metrics(analysis_history)
    latest_price = monthly_history[-1].close_price
    three_year_snapshot = build_period_snapshot(
        analysis_history,
        "3Y",
        start_date=latest_date - timedelta(days=365 * 3),
        end_date=latest_date,
    )
    five_year_snapshot = build_period_snapshot(
        analysis_history,
        "5Y",
        start_date=latest_date - timedelta(days=365 * 5),
        end_date=latest_date,
    )
    coefficient = correlation.get("coefficient")
    annual_return_components = []

    cagr_pct = cycle_metrics.get("cagr_pct")
    if cagr_pct is not None:
        annual_return_components.append(cagr_pct * Decimal("0.50"))
    if five_year_snapshot.get("available") and five_year_snapshot.get("stock_return_pct") is not None:
        five_year_signal = annualize_return_pct(five_year_snapshot["stock_return_pct"], 60)
        if five_year_signal is not None:
            annual_return_components.append(five_year_signal * Decimal("0.30"))
    if three_year_snapshot.get("available") and three_year_snapshot.get("stock_return_pct") is not None:
        three_year_signal = annualize_return_pct(three_year_snapshot["stock_return_pct"], 36)
        if three_year_signal is not None:
            annual_return_components.append(three_year_signal * Decimal("0.20"))
    reference_five_year_return_pct = five_year_snapshot.get("benchmark_return_pct") if five_year_snapshot.get("available") else None
    if coefficient is not None and reference_five_year_return_pct is not None:
        reference_five_year_signal = annualize_return_pct(reference_five_year_return_pct, 60)
        if reference_five_year_signal is not None:
            annual_return_components.append(reference_five_year_signal * coefficient * Decimal("0.08"))

    current_drawdown_pct = cycle_metrics.get("current_drawdown_pct")
    if current_drawdown_pct is not None:
        if current_drawdown_pct <= Decimal("-20.00"):
            annual_return_components.append(Decimal("3.00"))
        elif current_drawdown_pct <= Decimal("-8.00"):
            annual_return_components.append(Decimal("1.50"))
        elif current_drawdown_pct >= Decimal("-3.00"):
            annual_return_components.append(Decimal("-1.20"))

    annual_return_components.append(
        {
            "Correccion": Decimal("2.25"),
            "Recuperacion": Decimal("1.25"),
            "Expansion": Decimal("0.25"),
            "Transicion": Decimal("-0.50"),
        }.get(cycle_metrics.get("cycle_phase") or "Transicion", ZERO)
    )

    positive_year_ratio_pct = cycle_metrics.get("positive_year_ratio_pct")
    if positive_year_ratio_pct is not None:
        annual_return_components.append(
            clamp_decimal((positive_year_ratio_pct - Decimal("50.00")) * Decimal("0.04"), Decimal("-0.80"), Decimal("0.80"))
        )

    if not annual_return_components:
        return {"available": False}

    annual_return_pct = sum(annual_return_components, ZERO)
    years_covered = cycle_metrics.get("years_covered", ZERO)
    if years_covered < Decimal("5.00"):
        annual_return_pct *= Decimal("0.85")
    elif years_covered >= Decimal("8.00"):
        annual_return_pct *= Decimal("0.96")
    annual_return_pct = clamp_decimal(annual_return_pct, Decimal("-10.00"), Decimal("16.00"))

    half_year_returns = []
    for index in range(6, len(monthly_history), 6):
        half_year_return = percentage_change(monthly_history[index].close_price, monthly_history[index - 6].close_price)
        if half_year_return is not None:
            half_year_returns.append(half_year_return)
    positive_half_year_returns = [value for value in half_year_returns if value > ZERO]
    negative_half_year_returns = [value for value in half_year_returns if value < ZERO]
    base_half_year_return = Decimal(
        str(round(((max(0.01, 1 + (float(annual_return_pct) / 100)) ** 0.5) - 1) * 100, 4))
    )
    upside_return = median_decimal(positive_half_year_returns)
    if upside_return is None:
        upside_return = clamp_decimal(base_half_year_return * Decimal("1.40"), Decimal("2.50"), Decimal("10.00"))
    downside_return = median_decimal(negative_half_year_returns)
    downside_floor = -clamp_decimal(
        max(
            abs(cycle_metrics.get("max_drawdown_pct") or ZERO) * Decimal("0.22"),
            (cycle_metrics.get("annualized_volatility_pct") or Decimal("16.00")) * Decimal("0.32"),
            Decimal("2.50"),
        ),
        Decimal("2.50"),
        Decimal("10.00"),
    )
    if downside_return is None:
        downside_return = downside_floor
    else:
        downside_return = min(downside_return, downside_floor)
    mild_up = average_decimal([base_half_year_return, upside_return]) or base_half_year_return
    mild_down = average_decimal([base_half_year_return, downside_return]) or downside_return
    flat_return = base_half_year_return * Decimal("0.35")
    recovery_return = average_decimal([upside_return, mild_up]) or mild_up
    correction_return = average_decimal([downside_return, mild_down]) or mild_down
    phase_sequence = {
        "Expansion": [
            recovery_return,
            correction_return,
            mild_up,
            downside_return,
            recovery_return,
            mild_down,
            upside_return,
            correction_return,
            mild_up,
            flat_return,
        ],
        "Recuperacion": [
            upside_return,
            mild_up,
            correction_return,
            recovery_return,
            mild_down,
            upside_return,
            correction_return,
            mild_up,
            flat_return,
            mild_down,
        ],
        "Correccion": [
            downside_return,
            mild_up,
            correction_return,
            recovery_return,
            mild_down,
            upside_return,
            downside_return,
            mild_up,
            correction_return,
            recovery_return,
        ],
        "Transicion": [
            correction_return,
            mild_up,
            flat_return,
            downside_return,
            recovery_return,
            mild_down,
            upside_return,
            correction_return,
            mild_up,
            flat_return,
        ],
    }
    step_return_pcts = list(phase_sequence.get(cycle_metrics.get("cycle_phase") or "Transicion", phase_sequence["Transicion"]))
    schedule_average = average_decimal(step_return_pcts) or ZERO
    schedule_shift = base_half_year_return - schedule_average
    step_return_pcts = [
        clamp_decimal(value + schedule_shift, Decimal("-12.00"), Decimal("12.00"))
        for value in step_return_pcts
    ]
    if all(value >= ZERO for value in step_return_pcts):
        step_return_pcts[3] = min(downside_return, Decimal("-1.50"))
    if all(value <= ZERO for value in step_return_pcts):
        step_return_pcts[1] = max(upside_return, Decimal("1.50"))

    path = build_cycle_projection_path(
        latest_price,
        annual_return_pct,
        annualized_volatility_pct=cycle_metrics.get("annualized_volatility_pct"),
        current_drawdown_pct=current_drawdown_pct,
        cycle_phase=cycle_metrics.get("cycle_phase") or "Transicion",
        anchor_date=latest_date,
        years=5,
        step_months=6,
        step_return_pcts=step_return_pcts,
    )
    if not path:
        return {"available": False}

    projected_price = path[-1]["projected_price"]
    five_year_return_pct = percentage_change(projected_price, latest_price)
    analysis_years_used = min(years_covered, Decimal(str(LONG_ANALYSIS_YEARS))) if years_covered else ZERO
    if analysis_years_used >= Decimal(str(LONG_ANALYSIS_YEARS)) - Decimal("0.15"):
        analysis_years_used = Decimal(str(LONG_ANALYSIS_YEARS))
    explanation = (
        f"Esta vista 5A usa los ultimos {analysis_years_used:.1f} anos para leer el ciclo de {position.company_name}. "
        f"Combina CAGR del ciclo, ritmo a 3-5 anos, drawdown actual y fase {cycle_metrics.get('cycle_phase', 'sin ciclo').lower()} "
        "para dibujar una senda larga de orientacion con tramos de correccion y rebote inspirados en las ventanas bajistas y alcistas del historico, separada de la decision a 12 meses."
    )
    return {
        "available": True,
        "annual_return_pct": quantize_decimal(annual_return_pct),
        "projected_price": projected_price,
        "five_year_return_pct": quantize_decimal(five_year_return_pct),
        "path": path,
        "cycle_phase": cycle_metrics.get("cycle_phase"),
        "analysis_years_used": analysis_years_used,
        "history_window_label": "Ultimos 5 anos",
        "model_window_label": f"{analysis_years_used:.1f} anos de historico",
        "step_return_pcts": [quantize_decimal(value) or ZERO for value in step_return_pcts],
        "explanation": explanation,
    }


def build_cycle_projection_5y_chart(history, cycle_projection: dict) -> dict:
    if len(history) < 2 or not cycle_projection.get("available"):
        return {"available": False}

    latest_date = history[-1].price_date
    recent_history = filter_history_window(
        history,
        start_date=latest_date - timedelta(days=365 * 5),
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
        for step in cycle_projection.get("path", [])
        if step.get("projected_date") and step.get("projected_price") is not None
    )
    chart = build_dual_axis_chart(stock_series, [], projection_points=projection_series)
    end_projection_step = cycle_projection.get("path", [])[-1] if cycle_projection.get("path") else None
    return {
        "available": bool(chart.get("stock_line") and chart.get("projection_line")),
        "stock_line": chart.get("stock_line", ""),
        "projection_line": chart.get("projection_line", ""),
        "stock_min_label": chart.get("stock_min_label", "-"),
        "stock_max_label": chart.get("stock_max_label", "-"),
        "x_markers": chart.get("x_markers", []),
        "start_label": recent_history[0].price_date.isoformat(),
        "end_label": latest_date.isoformat(),
        "projection_end_label": end_projection_step.get("projected_date").isoformat() if end_projection_step and end_projection_step.get("projected_date") else "",
        "points_count": len(stock_series),
        "history_window_label": cycle_projection.get("history_window_label", "Ultimos 5 anos"),
        "model_window_label": cycle_projection.get("model_window_label", ""),
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
    benchmark_series: list[dict] | None = None,
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
    benchmark_points = normalize_series(benchmark_series or [])
    if not actual_points or not expected_points:
        return {
            "available": False,
            "actual_line": "",
            "expected_line": "",
            "benchmark_line": "",
            "actual_points": [],
            "expected_points": [],
            "benchmark_points": [],
            "min_label": "-",
            "max_label": "-",
            "start_label": "",
            "latest_label": "",
            "projection_end_label": "",
            "points_count": 0,
        }

    all_values = (
        [point["value"] for point in actual_points]
        + [point["value"] for point in expected_points]
        + [point["value"] for point in benchmark_points]
    )
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
    benchmark_line, benchmark_point_rows = build_series(benchmark_points, "benchmark")
    return {
        "available": True,
        "actual_line": actual_line,
        "expected_line": expected_line,
        "benchmark_line": benchmark_line,
        "actual_points": actual_point_rows,
        "expected_points": expected_point_rows,
        "benchmark_points": benchmark_point_rows,
        "min_label": f"{format_axis_value(series_min)} EUR",
        "max_label": f"{format_axis_value(series_max)} EUR",
        "start_label": min_date.isoformat(),
        "latest_label": actual_points[-1]["date"].isoformat(),
        "projection_end_label": expected_points[-1]["date"].isoformat(),
        "points_count": len(actual_points),
        "x_markers": build_time_axis_markers(
            [point["date"] for point in actual_points] + [point["date"] for point in expected_points],
            width=width,
            height=height,
            padding=padding,
        ),
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


def filter_ticket_tracking_snapshots(
    snapshots: list[EquityTicketSnapshot],
    tracking_anchor_date: date | None = None,
) -> list[EquityTicketSnapshot]:
    if not snapshots or tracking_anchor_date is None:
        return snapshots
    filtered = [snapshot for snapshot in snapshots if snapshot.snapshot_date >= tracking_anchor_date]
    return filtered or snapshots


def resolve_ticket_tracking_anchor_date(grouped_snapshots: dict[int, list[EquityTicketSnapshot]]) -> date | None:
    earliest_dates = [snapshots[0].snapshot_date for snapshots in grouped_snapshots.values() if snapshots]
    return max(earliest_dates) if earliest_dates else None


def build_normalized_reference_tracking_series(
    reference_points: list[dict],
    tracking_dates: list[date],
    baseline_value: Decimal,
) -> list[dict]:
    normalized_points = [
        {
            "date": point.get("date"),
            "close": Decimal(str(point.get("close"))),
        }
        for point in reference_points
        if point.get("date") is not None and point.get("close") is not None
    ]
    requested_dates = sorted({point_date for point_date in tracking_dates if point_date is not None})
    if not normalized_points or not requested_dates:
        return []

    normalized_points.sort(key=lambda item: item["date"])
    point_index = 0
    last_close = None
    baseline_reference_close = None
    series = []

    for point_date in requested_dates:
        while point_index < len(normalized_points) and normalized_points[point_index]["date"] <= point_date:
            last_close = normalized_points[point_index]["close"]
            point_index += 1

        if baseline_reference_close is None:
            baseline_reference_close = last_close
            if baseline_reference_close is None:
                future_point = next(
                    (point for point in normalized_points if point["date"] >= point_date),
                    None,
                )
                if future_point is None:
                    continue
                baseline_reference_close = future_point["close"]
                last_close = future_point["close"]

        current_close = last_close or baseline_reference_close
        if current_close is None or baseline_reference_close in {None, ZERO}:
            continue

        normalized_value = quantize_decimal(
            baseline_value * (current_close / baseline_reference_close),
            "0.01",
        ) or ZERO
        series.append({"date": point_date, "value": normalized_value})

    return series


def build_tracking_benchmark_context(
    tracking_dates: list[date],
    baseline_value: Decimal,
) -> dict:
    if not tracking_dates:
        return {
            "available": False,
            "label": DEFAULT_BENCHMARK_NAME,
            "series": [],
            "latest_value": None,
            "actual_change_pct": None,
        }

    try:
        benchmark_series = fetch_reference_series_for_choice(
            EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol=DEFAULT_BENCHMARK_SYMBOL,
            benchmark_name=DEFAULT_BENCHMARK_NAME,
        )
    except Exception:
        benchmark_series = None

    if benchmark_series is None:
        return {
            "available": False,
            "label": DEFAULT_BENCHMARK_NAME,
            "series": [],
            "latest_value": None,
            "actual_change_pct": None,
        }

    series = build_normalized_reference_tracking_series(
        benchmark_series.points,
        tracking_dates,
        baseline_value,
    )
    latest_value = series[-1]["value"] if series else None
    return {
        "available": bool(series),
        "label": benchmark_series.name or DEFAULT_BENCHMARK_NAME,
        "series": series,
        "latest_value": latest_value,
        "actual_change_pct": percentage_change(latest_value, series[0]["value"]) if series else None,
    }


def add_calendar_years(value: date | None, years: int) -> date | None:
    if value is None:
        return None
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + years)


def build_purchase_baseline_yearly_rows(
    purchase_baseline: EquityPurchaseForecastBaseline | None,
) -> list[dict]:
    if purchase_baseline is None or purchase_baseline.baseline_date is None:
        return []

    rows = []
    previous_price = purchase_baseline.baseline_price
    previous_cumulative_return = ZERO

    for year_number in range(1, 6):
        projected_price = getattr(purchase_baseline, f"projected_price_{year_number}y", None)
        cumulative_return_pct = getattr(
            purchase_baseline,
            f"projected_return_pct_{year_number}y",
            None,
        )
        if projected_price is None and cumulative_return_pct is None:
            continue

        margin_pct = None
        if projected_price not in {None, ZERO} and previous_price not in {None, ZERO}:
            if year_number == 1 and cumulative_return_pct is not None:
                margin_pct = cumulative_return_pct
            else:
                margin_pct = percentage_change(projected_price, previous_price)
        elif cumulative_return_pct is not None:
            if year_number == 1:
                margin_pct = cumulative_return_pct
            else:
                margin_pct = quantize_decimal(cumulative_return_pct - previous_cumulative_return)

        rows.append(
            {
                "year_number": year_number,
                "projected_date": add_calendar_years(purchase_baseline.baseline_date, year_number),
                "reentry_date": add_calendar_years(purchase_baseline.baseline_date, year_number - 1),
                "projected_price": projected_price,
                "cumulative_return_pct": cumulative_return_pct,
                "margin_pct": quantize_decimal(margin_pct),
            }
        )
        if projected_price is not None:
            previous_price = projected_price
        if cumulative_return_pct is not None:
            previous_cumulative_return = cumulative_return_pct

    return rows


def build_purchase_forecast_trade_plan(
    purchase_baseline: EquityPurchaseForecastBaseline | None,
) -> dict:
    yearly_rows = build_purchase_baseline_yearly_rows(purchase_baseline)
    if not yearly_rows:
        return {"available": False}

    best_row = max(
        yearly_rows,
        key=lambda item: (
            item["cumulative_return_pct"]
            if item["cumulative_return_pct"] is not None
            else Decimal("-9999"),
            -item["year_number"],
        ),
    )
    sale_row = None
    drawdown_row = None
    for row in yearly_rows:
        if row["year_number"] <= 1:
            continue
        margin_pct = row.get("margin_pct")
        previous_row = next(
            (item for item in yearly_rows if item["year_number"] == row["year_number"] - 1),
            None,
        )
        previous_return_pct = previous_row.get("cumulative_return_pct") if previous_row else None
        if (
            margin_pct is not None
            and margin_pct <= -OPTIMIZER_MAX_ROUNDTRIP_DRAG_PCT
            and previous_row is not None
            and previous_return_pct is not None
            and previous_return_pct >= OPTIMIZER_MAX_ENTRY_DRAG_PCT
        ):
            sale_row = previous_row
            drawdown_row = row
            break

    if sale_row is None:
        sale_window_label = (
            f"Cierre A\u00d1O {best_row['year_number']}"
            if best_row.get("year_number")
            else "Mantener"
        )
        return {
            "available": True,
            "mode": "hold",
            "sale_year_number": best_row["year_number"],
            "sale_date": best_row["projected_date"],
            "sale_date_label": "",
            "sale_window_label": sale_window_label,
            "reentry_year_number": None,
            "reentry_date": None,
            "reentry_date_label": "",
            "reentry_window_label": "",
            "summary": (
                "La foto de compra no muestra un retroceso anual lo bastante fuerte como para justificar "
                f"una salida tactica. Mantener hasta {sale_window_label.lower()} sigue siendo la lectura base. "
                "La recomendacion se expresa en ventanas anuales, no en dias exactos."
            )
            if best_row.get("year_number")
            else (
                "La foto de compra no muestra un retroceso anual fuerte y la lectura base sigue siendo mantener. "
                "La recomendacion se expresa en ventanas anuales, no en dias exactos."
            ),
            "yearly_rows": yearly_rows,
            "drawdown_year_number": None,
            "drawdown_margin_pct": None,
        }

    reentry_row = next(
        (
            item
            for item in yearly_rows
            if item["year_number"] > drawdown_row["year_number"]
            and item.get("margin_pct") is not None
            and item["margin_pct"] >= OPTIMIZER_MAX_ENTRY_DRAG_PCT
        ),
        None,
    )
    sale_window_label = f"Cierre A\u00d1O {sale_row['year_number']}"
    reentry_date = reentry_row.get("reentry_date") if reentry_row else None
    reentry_window_label = f"Inicio A\u00d1O {reentry_row['year_number']}" if reentry_row else ""
    if reentry_row and reentry_window_label:
        summary = (
            f"Se propone salir en {sale_window_label.lower()} antes de una caida prevista "
            f"del {drawdown_row['margin_pct']:.1f} %. Si la tesis sigue viva, la reentrada sugerida "
            f"se revisa en {reentry_window_label.lower()}. La recomendacion se expresa en ventanas anuales, "
            "no en dias exactos."
        )
    else:
        summary = (
            f"Se propone salir en {sale_window_label.lower()} antes de una caida prevista "
            f"del {drawdown_row['margin_pct']:.1f} %. Despues conviene revisar de nuevo el radar antes de volver. "
            "La recomendacion se expresa en ventanas anuales, no en dias exactos."
        )

    return {
        "available": True,
        "mode": "sale_reentry" if reentry_row else "sale_review",
        "sale_year_number": sale_row["year_number"],
        "sale_date": sale_row["projected_date"],
        "sale_date_label": "",
        "sale_window_label": sale_window_label,
        "reentry_year_number": reentry_row["year_number"] if reentry_row else None,
        "reentry_date": reentry_date,
        "reentry_date_label": "",
        "reentry_window_label": reentry_window_label,
        "summary": summary,
        "yearly_rows": yearly_rows,
        "drawdown_year_number": drawdown_row["year_number"] if drawdown_row else None,
        "drawdown_margin_pct": drawdown_row["margin_pct"] if drawdown_row else None,
    }


def build_purchase_trade_rotation_guidance(
    trade_plan: dict,
    position: EquityPosition,
    optimizer_cards: list[dict] | None,
    *,
    comparison_amount: Decimal = Decimal("10000"),
) -> dict:
    if not trade_plan.get("available") or not optimizer_cards:
        return {"available": False}

    comparison_amount = comparison_amount if comparison_amount > ZERO else Decimal("10000")
    candidates = [
        candidate
        for candidate in (
            build_equity_optimizer_candidate(card, OPTIMIZER_STRATEGY_12M_PRIMARY)
            for card in optimizer_cards or []
        )
        if candidate and candidate["position"].ticker != position.ticker
    ]
    ranked_candidates = rank_optimizer_candidates(
        filter_positive_optimizer_candidates(candidates, OPTIMIZER_STRATEGY_12M_PRIMARY)
    )

    selected_candidate = None
    selected_scenario = None
    for candidate in ranked_candidates:
        scenario = build_optimizer_allocation_scenario(candidate, comparison_amount)
        review = review_optimizer_ticket_efficiency(candidate, comparison_amount, scenario)
        if review.get("keep"):
            selected_candidate = candidate
            selected_scenario = scenario
            break

    if selected_candidate is None or selected_scenario is None:
        return {"available": False}

    yearly_rows = trade_plan.get("yearly_rows") or []
    sale_year_number = trade_plan.get("sale_year_number")
    sale_row = next((item for item in yearly_rows if item.get("year_number") == sale_year_number), None)
    future_rows = [
        item
        for item in yearly_rows
        if sale_year_number is not None
        and item.get("year_number", 0) > sale_year_number
        and item.get("projected_price") is not None
    ]
    best_future_row = max(
        future_rows,
        key=lambda item: (
            item.get("cumulative_return_pct")
            if item.get("cumulative_return_pct") is not None
            else Decimal("-9999"),
            -item.get("year_number", 0),
        ),
        default=None,
    )
    hold_remaining_return_pct = None
    if sale_row and best_future_row and sale_row.get("projected_price") not in {None, ZERO}:
        hold_remaining_return_pct = percentage_change(
            best_future_row.get("projected_price"),
            sale_row.get("projected_price"),
        )

    reentry_reference_row = next(
        (
            item
            for item in yearly_rows
            if item.get("year_number") == trade_plan.get("drawdown_year_number")
        ),
        None,
    )
    reentry_remaining_return_pct = None
    if reentry_reference_row and best_future_row and reentry_reference_row.get("projected_price") not in {None, ZERO}:
        reentry_remaining_return_pct = percentage_change(
            best_future_row.get("projected_price"),
            reentry_reference_row.get("projected_price"),
        )

    alternative_return_pct = selected_scenario.get("net_projected_return_pct")
    action = "mantener"
    summary = (
        f"La mejor alternativa actual del radar es {selected_candidate['position'].company_name} "
        f"({selected_candidate['position'].ticker}) con retorno neto 12M del {alternative_return_pct:.2f} %."
    )

    drawdown_margin_pct = trade_plan.get("drawdown_margin_pct")
    tactical_weak_period_pct = (
        drawdown_margin_pct.copy_abs()
        if isinstance(drawdown_margin_pct, Decimal) and drawdown_margin_pct < ZERO
        else ZERO
    )
    if (
        trade_plan.get("mode") in {"sale_reentry", "sale_review"}
        and alternative_return_pct is not None
        and alternative_return_pct >= tactical_weak_period_pct + OPTIMIZER_MAX_ROUNDTRIP_DRAG_PCT
    ):
        action = "rotar"
        summary = (
            f"En la foto actual compensa mas rotar hacia {selected_candidate['position'].company_name} "
            f"({selected_candidate['position'].ticker}), con retorno neto 12M del {alternative_return_pct:.2f} %, "
            "que aguantar el tramo debil previsto de esta posicion."
        )
    elif trade_plan.get("mode") == "sale_reentry":
        action = "esperar_reentrada"
        summary += " Hoy sigue pesando mas la salida tactica y esperar la reentrada sugerida."
    elif trade_plan.get("mode") == "sale_review":
        action = "revisar_en_venta"
        summary += " Hoy sigue pesando mas vender en la fecha sugerida y revisar entonces el radar."

    return {
        "available": True,
        "action": action,
        "summary": summary,
        "alternative_ticker": selected_candidate["position"].ticker,
        "alternative_company_name": selected_candidate["position"].company_name,
        "alternative_return_pct": alternative_return_pct,
        "alternative_reference_label": selected_candidate.get("reference_label") or "",
        "alternative_trade_alert_label": selected_candidate.get("trade_alert_label") or "",
        "alternative_reliability_label": selected_candidate.get("reliability_label") or "",
        "alternative_strategy_label": selected_candidate.get("strategy_label") or "",
        "hold_remaining_return_pct": quantize_decimal(hold_remaining_return_pct),
        "reentry_remaining_return_pct": quantize_decimal(reentry_remaining_return_pct),
    }


def build_equity_ticket_tracking_item(
    card: dict,
    snapshots: list[EquityTicketSnapshot],
    tracking_anchor_date: date | None = None,
    purchase_baseline: EquityPurchaseForecastBaseline | None = None,
    optimizer_cards: list[dict] | None = None,
) -> dict | None:
    relevant_snapshots = filter_ticket_tracking_snapshots(snapshots, tracking_anchor_date)
    if not relevant_snapshots:
        return None

    position = card["position"]
    baseline = relevant_snapshots[0]
    latest = relevant_snapshots[-1]
    previous = relevant_snapshots[-2] if len(relevant_snapshots) >= 2 else None
    expected_market_value_12m = baseline.projected_market_value_12m or baseline.current_value
    expected_total_value_12m = baseline.projected_total_value_12m or expected_market_value_12m
    actual_series = [{"date": snapshot.snapshot_date, "value": snapshot.current_value} for snapshot in relevant_snapshots]
    expected_series, current_expected_value, projected_end_date = build_ticket_expected_series(
        relevant_snapshots,
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
    try:
        trade_plan = build_purchase_forecast_trade_plan(purchase_baseline)
    except Exception:
        logger.exception(
            "No se pudo construir el plan tactico de compra para %s",
            position.ticker,
        )
        trade_plan = {"available": False}
    try:
        rotation_plan = build_purchase_trade_rotation_guidance(
            trade_plan,
            position,
            optimizer_cards,
        )
    except Exception:
        logger.exception(
            "No se pudo construir la sugerencia de rotacion para %s",
            position.ticker,
        )
        rotation_plan = {"available": False}
    return {
        "position": position,
        "card": card,
        "baseline_snapshot": baseline,
        "latest_snapshot": latest,
        "snapshot_count": len(relevant_snapshots),
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
        "trade_plan": trade_plan,
        "rotation_plan": rotation_plan,
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

    baseline_value = actual_series[0]["value"]
    benchmark = build_tracking_benchmark_context(
        [point["date"] for point in actual_series],
        baseline_value,
    )
    chart = build_value_tracking_chart(
        actual_series,
        expected_series,
        benchmark_series=benchmark["series"],
    )
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
        "benchmark": benchmark,
        "gap_value": gap_value,
        "gap_pct": percentage_change(latest_actual, current_expected_value) if current_expected_value is not None else None,
        "gap_tone": "good" if gap_value is not None and gap_value >= ZERO else "warn",
        "daily_change_pct": percentage_change(latest_actual, previous_actual) if previous_actual else None,
        "tracked_days": max((latest_date - actual_series[0]["date"]).days, 0),
    }


def build_equity_ticket_tracking_context(
    history_cards: list[dict],
    optimizer_cards: list[dict] | None = None,
) -> dict:
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
    purchase_baselines = {
        baseline.position_id: baseline
        for baseline in EquityPurchaseForecastBaseline.objects.filter(position_id__in=cards_by_id.keys())
    }
    grouped_snapshots: dict[int, list[EquityTicketSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped_snapshots[snapshot.position_id].append(snapshot)

    tracking_anchor_date = resolve_ticket_tracking_anchor_date(grouped_snapshots)
    ticket_items = []
    for card in owned_cards:
        position_id = card["position"].id
        if not position_id:
            continue
        item = build_equity_ticket_tracking_item(
            card,
            grouped_snapshots.get(position_id, []),
            tracking_anchor_date=tracking_anchor_date,
            purchase_baseline=purchase_baselines.get(position_id),
            optimizer_cards=optimizer_cards,
        )
        if item:
            ticket_items.append(item)

    ticket_items.sort(
        key=lambda item: (
            -(item["latest_snapshot"].current_value if item.get("latest_snapshot") else ZERO),
            item["position"].company_name,
        )
    )
    snapshot_days = sorted(
        {
            point["date"]
            for item in ticket_items
            for point in item.get("actual_series", [])
        }
    )
    return {
        "available": bool(ticket_items),
        "tickets": ticket_items,
        "tracked_ticket_count": len(ticket_items),
        "snapshot_days_count": len(snapshot_days),
        "anchor_date": tracking_anchor_date,
        "global": build_global_equity_ticket_tracking_item(ticket_items) if ticket_items else {"available": False},
    }


def serialize_equity_market_value_history(
    position: EquityPosition,
    closed_on: date | None = None,
    sale_price_per_share: Decimal | None = None,
) -> list[dict]:
    history = list(position.price_history.all().order_by("price_date"))
    serialized = []
    for point in history:
        if closed_on and point.price_date > closed_on:
            break
        serialized.append(
            {
                "date": point.price_date.isoformat(),
                "market_value": str(quantize_decimal(position.shares * point.close_price, "0.01") or ZERO),
            }
        )
    if closed_on and sale_price_per_share is not None:
        sale_market_value = quantize_decimal(position.shares * sale_price_per_share, "0.01") or ZERO
        if serialized and serialized[-1]["date"] == closed_on.isoformat():
            serialized[-1]["market_value"] = str(sale_market_value)
        else:
            serialized.append({"date": closed_on.isoformat(), "market_value": str(sale_market_value)})
    return serialized


def build_single_value_history_chart(series: list[dict]) -> dict:
    filtered = [
        {"date": point.get("date"), "value": Decimal(str(point.get("value")))}
        for point in series
        if point.get("date") is not None and point.get("value") is not None
    ]
    if len(filtered) < 2:
        return {"available": False}
    chart = build_dual_axis_chart(filtered, [])
    return {
        "available": bool(chart.get("stock_line")),
        "line": chart.get("stock_line", ""),
        "min_label": chart.get("stock_min_label", "-"),
        "max_label": chart.get("stock_max_label", "-"),
        "x_markers": chart.get("x_markers", []),
        "start_label": filtered[0]["date"].isoformat(),
        "end_label": filtered[-1]["date"].isoformat(),
        "points_count": len(filtered),
    }


def build_annual_result_chart(rows: list[dict], width: int = 640, height: int = 240, padding: int = 26) -> dict:
    if not rows:
        return {"available": False, "bars": []}

    values = [Decimal(str(row.get("net_result") or ZERO)) for row in rows]
    min_value = min([*values, ZERO])
    max_value = max([*values, ZERO])
    if min_value == max_value:
        max_value += Decimal("1")
        min_value -= Decimal("1")

    span_y = height - (padding * 2)
    span_x = width - (padding * 2)
    slot_width = span_x / max(len(rows), 1)
    bar_width = max(slot_width * 0.48, 18)

    def to_y(value: Decimal) -> float:
        normalized = (value - min_value) / (max_value - min_value)
        return float(height - padding - (normalized * span_y))

    zero_y = to_y(ZERO)
    bars = []
    for index, row in enumerate(rows):
        value = Decimal(str(row.get("net_result") or ZERO))
        center_x = padding + (slot_width * index) + (slot_width / 2)
        value_y = to_y(value)
        top_y = min(value_y, zero_y)
        bar_height = max(abs(zero_y - value_y), 1.5)
        bars.append(
            {
                "x": f"{center_x - (bar_width / 2):.1f}",
                "y": f"{top_y:.1f}",
                "width": f"{bar_width:.1f}",
                "height": f"{bar_height:.1f}",
                "year": row["year"],
                "value_label": f'{format_axis_value(value)} EUR',
                "tone": "good" if value >= ZERO else "warn",
                "text_x": f"{center_x:.1f}",
                "text_y": f"{height - 8:.1f}",
            }
        )

    return {
        "available": True,
        "bars": bars,
        "zero_y": f"{zero_y:.1f}",
        "min_label": f"{format_axis_value(min_value)} EUR",
        "max_label": f"{format_axis_value(max_value)} EUR",
        "years_count": len(rows),
    }


def collapse_date_value_series(series: list[dict], frequency: str = "monthly") -> list[dict]:
    buckets = {}
    for point in series:
        point_date = point.get("date")
        value = point.get("value")
        if point_date is None or value is None:
            continue
        label = bucket_label_for_date(point_date, frequency)
        buckets[label] = {"date": point_date, "value": Decimal(str(value))}
    return [buckets[label] for label in sorted(buckets.keys())]


def resolve_position_start_date(
    opened_on: date | None,
    fallback_date: date | None,
    default_date: date | None = None,
) -> tuple[date, str, bool]:
    if opened_on:
        return opened_on, "Fecha de compra indicada", False
    if fallback_date:
        return fallback_date, "Primer dato historico disponible", True
    if default_date:
        return default_date, "Fecha aproximada sin historico", True
    today = django_timezone.localdate()
    return today, "Fecha aproximada sin historico", True


def estimate_period_totals(
    annual_recurring_cost: Decimal,
    annual_net_dividend_income: Decimal,
    start_date: date,
    end_date: date,
) -> tuple[Decimal, Decimal, int]:
    elapsed_days = max((end_date - start_date).days, 0)
    year_fraction = Decimal(str(elapsed_days)) / Decimal("365")
    maintenance_total = quantize_decimal(annual_recurring_cost * year_fraction, "0.01") or ZERO
    dividend_total = quantize_decimal(annual_net_dividend_income * year_fraction, "0.01") or ZERO
    return maintenance_total, dividend_total, elapsed_days


def build_equity_sale_preview(
    position: EquityPosition,
    sale_price_per_share: Decimal | None = None,
    closed_on: date | None = None,
) -> dict:
    if not position.is_owned:
        return {"available": False}

    history = sorted(position.price_history.all(), key=lambda point: point.price_date)
    fallback_start_date = history[0].price_date if history else position.latest_price_date
    requested_closed_on = closed_on or django_timezone.localdate()
    start_date, start_label, is_estimated = resolve_position_start_date(
        position.opened_on,
        fallback_start_date,
        requested_closed_on,
    )
    effective_closed_on = max(requested_closed_on, start_date)
    sale_price = quantize_decimal(
        Decimal(str(sale_price_per_share if sale_price_per_share is not None else position.current_price_per_share or ZERO)),
        "0.0001",
    ) or ZERO
    gross_sale_value = quantize_decimal(position.shares * sale_price, "0.01") or ZERO
    sale_costs = estimate_broker_costs(
        broker_name=position.broker,
        trade_channel=position.trade_channel,
        trade_amount=gross_sale_value,
        valuation_amount=gross_sale_value,
        annual_dividend_income=position.annual_dividend_income,
        quote_symbol=position.quote_symbol,
    )
    sale_total_cost = quantize_decimal(sale_costs.get("sale_total_cost", ZERO), "0.01") or ZERO
    purchase_cost = quantize_decimal(position.purchase_total_cost, "0.01") or ZERO
    maintenance_total, dividend_total, holding_days = estimate_period_totals(
        position.recurring_cost_used,
        position.net_dividend_income,
        start_date,
        effective_closed_on,
    )
    invested_amount = quantize_decimal(position.invested_amount, "0.01") or ZERO
    committed_capital = quantize_decimal(invested_amount + purchase_cost, "0.01") or ZERO
    net_exit_value = quantize_decimal(gross_sale_value - sale_total_cost, "0.01") or ZERO
    total_costs = quantize_decimal(purchase_cost + sale_total_cost + maintenance_total, "0.01") or ZERO
    net_result = quantize_decimal(
        net_exit_value - invested_amount - purchase_cost - maintenance_total + dividend_total,
        "0.01",
    ) or ZERO
    cumulative_margin_pct = (
        quantize_decimal((net_result / committed_capital) * ONE_HUNDRED, "0.01")
        if committed_capital
        else ZERO
    ) or ZERO
    comparable_returns = build_comparable_return_metrics(cumulative_margin_pct, holding_days)
    annualized_margin_pct = quantize_decimal(comparable_returns["annual_equivalent_return_pct"], "0.01") or ZERO
    monthly_equivalent_return_pct = quantize_decimal(comparable_returns["monthly_equivalent_return_pct"], "0.01") or ZERO

    return {
        "available": True,
        "start_date": start_date,
        "start_date_label": start_date.isoformat(),
        "start_date_source_label": start_label,
        "start_date_is_estimated": is_estimated,
        "closed_on": effective_closed_on,
        "closed_on_label": effective_closed_on.isoformat(),
        "holding_days": holding_days,
        "shares": position.shares,
        "sale_price_per_share": sale_price,
        "invested_amount": invested_amount,
        "purchase_cost": purchase_cost,
        "sale_total_cost": sale_total_cost,
        "annual_recurring_cost": quantize_decimal(position.recurring_cost_used, "0.01") or ZERO,
        "annual_net_dividend_income": quantize_decimal(position.net_dividend_income, "0.01") or ZERO,
        "maintenance_total": maintenance_total,
        "dividend_total": dividend_total,
        "gross_sale_value": gross_sale_value,
        "net_exit_value": net_exit_value,
        "total_costs": total_costs,
        "net_result": net_result,
        "committed_capital": committed_capital,
        "cumulative_margin_pct": cumulative_margin_pct,
        "monthly_equivalent_return_pct": monthly_equivalent_return_pct,
        "annualized_margin_pct": annualized_margin_pct,
        "cost_profile_label": sale_costs.get("profile_label") or position.broker,
        "market_scope_label": sale_costs.get("market_scope_label") or "",
        "trade_channel_label": sale_costs.get("trade_channel_label") or position.get_trade_channel_display(),
    }


def build_active_equity_investment_ticket(position: EquityPosition) -> dict | None:
    history = sorted(position.price_history.all(), key=lambda point: point.price_date)
    fallback_start_date = history[0].price_date if history else position.latest_price_date
    default_end_date = position.latest_price_date or (history[-1].price_date if history else django_timezone.localdate())
    start_date, start_label, is_estimated = resolve_position_start_date(position.opened_on, fallback_start_date, default_end_date)
    end_date = max(default_end_date, start_date)
    purchase_cost = quantize_decimal(position.purchase_total_cost, "0.01") or ZERO
    sale_cost_estimate = quantize_decimal(position.sale_total_cost_estimate, "0.01") or ZERO
    maintenance_total, dividend_total, holding_days = estimate_period_totals(
        position.recurring_cost_used,
        position.net_dividend_income,
        start_date,
        end_date,
    )

    live_series = [{"date": start_date, "value": quantize_decimal(position.invested_amount - purchase_cost, "0.01") or ZERO}]
    monthly_history = collapse_date_value_series(
        [
            {
                "date": point.price_date,
                "value": quantize_decimal(
                    (position.shares * point.close_price)
                    - purchase_cost
                    - (quantize_decimal(position.recurring_cost_used * (Decimal(str(max((point.price_date - start_date).days, 0))) / Decimal("365")), "0.01") or ZERO)
                    + (quantize_decimal(position.net_dividend_income * (Decimal(str(max((point.price_date - start_date).days, 0))) / Decimal("365")), "0.01") or ZERO),
                    "0.01",
                )
                or ZERO,
            }
            for point in history
            if point.price_date >= start_date
        ]
    )
    live_series.extend(monthly_history)
    latest_live_value = quantize_decimal(position.current_value - purchase_cost - maintenance_total + dividend_total, "0.01") or ZERO
    if not live_series or live_series[-1]["date"] != end_date:
        live_series.append({"date": end_date, "value": latest_live_value})
    else:
        live_series[-1]["value"] = latest_live_value

    committed_capital = quantize_decimal(position.invested_amount + purchase_cost, "0.01") or ZERO
    net_exit_value = quantize_decimal(latest_live_value - sale_cost_estimate, "0.01") or ZERO
    net_result = quantize_decimal(net_exit_value - position.invested_amount, "0.01") or ZERO
    cumulative_margin_pct = ((net_result / committed_capital) * ONE_HUNDRED) if committed_capital else ZERO
    comparable_returns = build_comparable_return_metrics(cumulative_margin_pct, holding_days)
    annualized_margin_pct = comparable_returns["annual_equivalent_return_pct"] or ZERO
    monthly_equivalent_return_pct = comparable_returns["monthly_equivalent_return_pct"] or ZERO
    return {
        "status": "active",
        "status_label": "En cartera",
        "ticker": position.ticker,
        "company_name": position.company_name,
        "broker": position.broker,
        "position": position,
        "start_date": start_date,
        "end_date": end_date,
        "start_date_label": start_date.isoformat(),
        "end_date_label": end_date.isoformat(),
        "start_date_source_label": start_label,
        "start_date_is_estimated": is_estimated,
        "committed_capital": committed_capital,
        "purchase_cost": purchase_cost,
        "sale_cost": sale_cost_estimate,
        "maintenance_total": maintenance_total,
        "dividend_total": dividend_total,
        "costs_total": quantize_decimal(purchase_cost + sale_cost_estimate + maintenance_total, "0.01") or ZERO,
        "current_or_sale_value": quantize_decimal(position.current_value, "0.01") or ZERO,
        "net_live_value": latest_live_value,
        "net_exit_value": net_exit_value,
        "net_result": net_result,
        "cumulative_margin_pct": quantize_decimal(cumulative_margin_pct, "0.01") or ZERO,
        "monthly_equivalent_return_pct": quantize_decimal(monthly_equivalent_return_pct, "0.01") or ZERO,
        "annualized_margin_pct": quantize_decimal(annualized_margin_pct, "0.01") or ZERO,
        "holding_days": holding_days,
        "value_series": live_series,
        "value_chart": build_single_value_history_chart(live_series),
        "notes": "La rentabilidad actual descuenta compra y mantenimiento. La salida neta incluye tambien el coste estimado de venta si cerraras hoy.",
    }


def build_closed_equity_investment_ticket(position: EquityClosedPosition) -> dict:
    archived_series = collapse_date_value_series(
        [
            {
                "date": date.fromisoformat(str(point.get("date"))),
                "value": Decimal(str(point.get("market_value") or "0")),
            }
            for point in (position.archived_price_history or [])
            if point.get("date")
        ]
    )
    fallback_start_date = archived_series[0]["date"] if archived_series else position.closed_on
    start_date, start_label, is_estimated = resolve_position_start_date(position.opened_on, fallback_start_date, position.closed_on)
    end_date = position.closed_on
    total_days = max((end_date - start_date).days, 0)
    live_series = [{"date": start_date, "value": quantize_decimal(position.invested_amount - position.purchase_total_cost, "0.01") or ZERO}]
    for point in archived_series:
        if point["date"] < start_date:
            continue
        elapsed_ratio = Decimal(str(max((point["date"] - start_date).days, 0))) / Decimal(str(max(total_days, 1)))
        accrued_maintenance = quantize_decimal(position.maintenance_cost_total * elapsed_ratio, "0.01") or ZERO
        accrued_dividends = quantize_decimal(position.net_dividend_income_total * elapsed_ratio, "0.01") or ZERO
        live_series.append(
            {
                "date": point["date"],
                "value": quantize_decimal(point["value"] - position.purchase_total_cost - accrued_maintenance + accrued_dividends, "0.01") or ZERO,
            }
        )
    net_sale_value = quantize_decimal(position.net_sale_value - position.purchase_total_cost - position.maintenance_cost_total + position.net_dividend_income_total, "0.01") or ZERO
    if not live_series or live_series[-1]["date"] != end_date:
        live_series.append({"date": end_date, "value": net_sale_value})
    else:
        live_series[-1]["value"] = net_sale_value

    committed_capital = quantize_decimal(position.committed_capital, "0.01") or ZERO
    net_result = quantize_decimal(position.net_result, "0.01") or ZERO
    cumulative_margin_pct = quantize_decimal(position.cumulative_margin_pct, "0.01") or ZERO
    comparable_returns = build_comparable_return_metrics(cumulative_margin_pct, max((end_date - start_date).days, 0))
    annualized_margin_pct = quantize_decimal(comparable_returns["annual_equivalent_return_pct"], "0.01") or ZERO
    monthly_equivalent_return_pct = quantize_decimal(comparable_returns["monthly_equivalent_return_pct"], "0.01") or ZERO
    return {
        "status": "closed",
        "status_label": "Vendida",
        "ticker": position.ticker,
        "company_name": position.company_name,
        "broker": position.broker,
        "position": position,
        "start_date": start_date,
        "end_date": end_date,
        "start_date_label": start_date.isoformat(),
        "end_date_label": end_date.isoformat(),
        "start_date_source_label": start_label,
        "start_date_is_estimated": is_estimated,
        "committed_capital": committed_capital,
        "purchase_cost": quantize_decimal(position.purchase_total_cost, "0.01") or ZERO,
        "sale_cost": quantize_decimal(position.sale_total_cost, "0.01") or ZERO,
        "maintenance_total": quantize_decimal(position.maintenance_cost_total, "0.01") or ZERO,
        "dividend_total": quantize_decimal(position.net_dividend_income_total, "0.01") or ZERO,
        "costs_total": quantize_decimal(position.purchase_total_cost + position.sale_total_cost + position.maintenance_cost_total, "0.01") or ZERO,
        "current_or_sale_value": quantize_decimal(position.gross_sale_value, "0.01") or ZERO,
        "net_live_value": net_sale_value,
        "net_exit_value": net_sale_value,
        "net_result": net_result,
        "cumulative_margin_pct": cumulative_margin_pct,
        "monthly_equivalent_return_pct": monthly_equivalent_return_pct,
        "annualized_margin_pct": annualized_margin_pct,
        "holding_days": max((end_date - start_date).days, 0),
        "value_series": live_series,
        "value_chart": build_single_value_history_chart(live_series),
        "notes": "Resultado cerrado. La venta ya descuenta compra, mantenimiento y el coste real estimado de salida.",
    }


def build_equity_investment_journey_context(
    open_positions: list[EquityPosition],
    closed_positions: list[EquityClosedPosition],
) -> dict:
    active_tickets = [ticket for ticket in (build_active_equity_investment_ticket(position) for position in open_positions if position.is_owned) if ticket]
    closed_tickets = [build_closed_equity_investment_ticket(position) for position in closed_positions]
    all_tickets = [*active_tickets, *closed_tickets]
    if not all_tickets:
        return {
            "available": False,
            "active_tickets": [],
            "closed_tickets": [],
            "value_chart": {"available": False},
            "profit_chart": {"available": False},
        }

    date_points = sorted({point["date"] for ticket in all_tickets for point in ticket["value_series"]})
    aggregated_value_series = []
    aggregated_profit_series = []
    yearly_snapshot_buckets: dict[int, dict] = {}
    yearly_committed_points: dict[int, list[Decimal]] = defaultdict(list)

    for point_date in date_points:
        total_value = ZERO
        total_committed = ZERO
        for ticket in all_tickets:
            applicable_points = [point for point in ticket["value_series"] if point["date"] <= point_date]
            if not applicable_points or point_date < ticket["start_date"]:
                continue
            latest_point = applicable_points[-1]
            total_value += latest_point["value"]
            total_committed += ticket["committed_capital"]
        total_profit = total_value - total_committed
        aggregated_value_series.append({"date": point_date, "value": quantize_decimal(total_value, "0.01") or ZERO})
        aggregated_profit_series.append({"date": point_date, "value": quantize_decimal(total_profit, "0.01") or ZERO})
        yearly_snapshot_buckets[point_date.year] = {
            "date": point_date,
            "value": quantize_decimal(total_value, "0.01") or ZERO,
            "profit": quantize_decimal(total_profit, "0.01") or ZERO,
            "committed": quantize_decimal(total_committed, "0.01") or ZERO,
        }
        yearly_committed_points[point_date.year].append(quantize_decimal(total_committed, "0.01") or ZERO)

    total_committed_capital = sum((ticket["committed_capital"] for ticket in all_tickets), ZERO)
    total_net_result = sum((ticket["net_result"] for ticket in all_tickets), ZERO)
    total_realized_result = sum((ticket["net_result"] for ticket in closed_tickets), ZERO)
    total_live_result = sum((ticket["net_result"] for ticket in active_tickets), ZERO)
    total_costs = sum((ticket["costs_total"] for ticket in all_tickets), ZERO)
    total_dividends = sum((ticket["dividend_total"] for ticket in all_tickets), ZERO)
    earliest_start = min((ticket["start_date"] for ticket in all_tickets), default=django_timezone.localdate())
    latest_end = max((ticket["end_date"] for ticket in all_tickets), default=django_timezone.localdate())
    total_days = max((latest_end - earliest_start).days, 1)
    years_operating = Decimal(str(total_days)) / Decimal("365")
    cumulative_margin_pct = ((total_net_result / total_committed_capital) * ONE_HUNDRED) if total_committed_capital else ZERO
    comparable_returns = build_comparable_return_metrics(cumulative_margin_pct, total_days)
    monthly_equivalent_return_pct = comparable_returns["monthly_equivalent_return_pct"] or ZERO
    annualized_margin_pct = comparable_returns["annual_equivalent_return_pct"] or ZERO
    annual_rows = []
    previous_profit = ZERO
    for year in sorted(yearly_snapshot_buckets):
        snapshot = yearly_snapshot_buckets[year]
        year_profit = snapshot["profit"]
        annual_result = quantize_decimal(year_profit - previous_profit, "0.01") or ZERO
        average_committed_capital = average_decimal(yearly_committed_points.get(year, [])) or snapshot["committed"] or ZERO
        annual_margin_pct = (
            quantize_decimal((annual_result / average_committed_capital) * ONE_HUNDRED, "0.01")
            if average_committed_capital
            else ZERO
        )
        annual_rows.append(
            {
                "year": year,
                "year_end_value": snapshot["value"],
                "year_end_profit": year_profit,
                "average_committed_capital": quantize_decimal(average_committed_capital, "0.01") or ZERO,
                "net_result": annual_result,
                "margin_pct": annual_margin_pct,
            }
        )
        previous_profit = year_profit

    average_annual_result = (
        quantize_decimal(total_net_result / years_operating, "0.01")
        if years_operating
        else ZERO
    ) or ZERO
    cost_ratio_pct = (
        quantize_decimal((total_costs / total_committed_capital) * ONE_HUNDRED, "0.01")
        if total_committed_capital
        else ZERO
    ) or ZERO
    dividend_yield_pct = (
        quantize_decimal((total_dividends / total_committed_capital) * ONE_HUNDRED, "0.01")
        if total_committed_capital
        else ZERO
    ) or ZERO
    current_year_row = annual_rows[-1] if annual_rows else None
    previous_year_row = annual_rows[-2] if len(annual_rows) >= 2 else None
    best_year = max(annual_rows, key=lambda row: row["net_result"], default=None)
    worst_year = min(annual_rows, key=lambda row: row["net_result"], default=None)

    active_tickets.sort(key=lambda ticket: (-ticket["current_or_sale_value"], ticket["company_name"]))
    closed_tickets.sort(key=lambda ticket: (ticket["end_date"], ticket["company_name"]), reverse=True)
    return {
        "available": True,
        "active_tickets": active_tickets,
        "closed_tickets": closed_tickets,
        "tickets_count": len(all_tickets),
        "active_count": len(active_tickets),
        "closed_count": len(closed_tickets),
        "value_chart": build_single_value_history_chart(aggregated_value_series),
        "profit_chart": build_single_value_history_chart(aggregated_profit_series),
        "current_net_value": aggregated_value_series[-1]["value"] if aggregated_value_series else ZERO,
        "cumulative_net_result": quantize_decimal(total_net_result, "0.01") or ZERO,
        "cumulative_margin_pct": quantize_decimal(cumulative_margin_pct, "0.01") or ZERO,
        "monthly_equivalent_return_pct": quantize_decimal(monthly_equivalent_return_pct, "0.01") or ZERO,
        "annualized_margin_pct": quantize_decimal(annualized_margin_pct, "0.01") or ZERO,
        "years_operating": quantize_decimal(years_operating, "0.01") or ZERO,
        "average_annual_result": average_annual_result,
        "cost_ratio_pct": cost_ratio_pct,
        "dividend_yield_pct": dividend_yield_pct,
        "realized_net_result": quantize_decimal(total_realized_result, "0.01") or ZERO,
        "live_net_result": quantize_decimal(total_live_result, "0.01") or ZERO,
        "committed_capital_total": quantize_decimal(total_committed_capital, "0.01") or ZERO,
        "costs_total": quantize_decimal(total_costs, "0.01") or ZERO,
        "dividends_total": quantize_decimal(total_dividends, "0.01") or ZERO,
        "holding_start_label": earliest_start.isoformat() if earliest_start else "",
        "holding_end_label": latest_end.isoformat() if latest_end else "",
        "annual_rows": annual_rows,
        "annual_result_chart": build_annual_result_chart(annual_rows),
        "current_year_row": current_year_row,
        "previous_year_row": previous_year_row,
        "best_year": best_year,
        "worst_year": worst_year,
        "estimated_start_count": sum(1 for ticket in all_tickets if ticket["start_date_is_estimated"]),
    }


@transaction.atomic
def archive_equity_position_sale(
    position: EquityPosition,
    closed_on: date,
    sale_price_per_share: Decimal,
    notes: str = "",
) -> EquityClosedPosition:
    fallback_start_date = position.price_history.order_by("price_date").values_list("price_date", flat=True).first()
    start_date = position.opened_on or fallback_start_date or closed_on
    maintenance_total, dividend_total, _ = estimate_period_totals(
        position.recurring_cost_used,
        position.net_dividend_income,
        start_date,
        closed_on,
    )
    sale_trade_amount = quantize_decimal(position.shares * sale_price_per_share, "0.01") or ZERO
    sale_costs = estimate_broker_costs(
        broker_name=position.broker,
        trade_channel=position.trade_channel,
        trade_amount=sale_trade_amount,
        valuation_amount=sale_trade_amount,
        annual_dividend_income=position.annual_dividend_income,
        quote_symbol=position.quote_symbol,
    )
    archived = EquityClosedPosition.objects.create(
        ownership_category=position.ownership_category,
        broker=position.broker,
        ticker=position.ticker,
        quote_symbol=position.quote_symbol,
        company_name=position.company_name,
        trade_channel=position.trade_channel,
        benchmark_symbol=position.benchmark_symbol,
        benchmark_name=position.benchmark_name,
        opened_on=position.opened_on,
        closed_on=closed_on,
        shares=position.shares,
        average_cost_per_share=position.average_cost_per_share,
        sale_price_per_share=sale_price_per_share,
        purchase_total_cost=quantize_decimal(position.purchase_total_cost, "0.01") or ZERO,
        sale_total_cost=quantize_decimal(sale_costs.get("sale_total_cost", ZERO), "0.01") or ZERO,
        maintenance_cost_total=maintenance_total,
        net_dividend_income_total=dividend_total,
        archived_price_history=serialize_equity_market_value_history(position, closed_on, sale_price_per_share),
        notes="\n\n".join(filter(None, [position.notes, notes])),
    )
    position.delete()
    return archived


def average_decimal(values: list[Decimal]) -> Decimal | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return sum(filtered, ZERO) / Decimal(len(filtered))


def median_decimal(values: list[Decimal]) -> Decimal | None:
    filtered = sorted(value for value in values if value is not None)
    if not filtered:
        return None
    middle = len(filtered) // 2
    if len(filtered) % 2:
        return filtered[middle]
    return (filtered[middle - 1] + filtered[middle]) / Decimal("2")


def percentage_change(current: Decimal | None, reference: Decimal | None) -> Decimal | None:
    if current is None or reference in {None, ZERO}:
        return None
    return ((current / reference) - Decimal("1")) * Decimal("100")


def format_compact_currency_value(value: Decimal | None, currency_code: str = "") -> str:
    if value is None:
        return "-"

    absolute = abs(value)
    if absolute >= Decimal("1000000000"):
        scaled_value = value / Decimal("1000000000")
        label = f"{scaled_value:.1f} mM"
    elif absolute >= Decimal("1000000"):
        scaled_value = value / Decimal("1000000")
        label = f"{scaled_value:.1f} M"
    elif absolute >= Decimal("1000"):
        scaled_value = value / Decimal("1000")
        label = f"{scaled_value:.1f} k"
    else:
        label = f"{value:.0f}"
    return f"{label} {currency_code}".strip()


def describe_net_income_trend(net_income_rows: list[dict]) -> tuple[str, str]:
    if len(net_income_rows) < 2:
        return "Sin serie suficiente", "Todavia no hay tres cierres completos para leer la trayectoria del beneficio."

    values = [row["value"] for row in net_income_rows]
    latest_value = values[-1]
    previous_value = values[-2]
    oldest_value = values[0]

    if latest_value > ZERO and all(current > previous for previous, current in zip(values, values[1:])):
        return "Mejora", "Los beneficios netos encadenan varios ejercicios al alza."
    if latest_value <= ZERO:
        return "Presion", "El ultimo cierre entra en beneficio negativo y debilita la lectura del ciclo."
    if all(current < previous for previous, current in zip(values, values[1:])):
        return "Deterioro", "El beneficio neto viene enfriandose de forma continuada."
    if latest_value > previous_value and previous_value <= oldest_value:
        return "Recuperacion", "El beneficio habia flojeado, pero el ultimo ejercicio vuelve a mejorar."
    if latest_value < previous_value and previous_value >= oldest_value:
        return "Enfriamiento", "El ultimo ejercicio enfria una etapa de mejora previa."
    return "Mixto", "La serie de beneficios no marca una direccion limpia todavia."


def build_equity_fundamentals_summary(position: EquityPosition, force_live_fetch: bool = False) -> dict:
    if not force_live_fetch and not should_fetch_equity_fundamentals():
        return {
            "available": False,
            "note": "La carga de fundamentales en vivo esta desactivada.",
            "net_income_rows": [],
        }

    if not position.quote_symbol:
        return {
            "available": False,
            "note": "No hay simbolo de cotizacion para buscar los fundamentales.",
            "net_income_rows": [],
        }

    try:
        snapshot = fetch_equity_fundamentals(position.quote_symbol)
    except Exception as exc:
        return {
            "available": False,
            "note": f"No se han podido cargar los fundamentales recientes: {exc}",
            "net_income_rows": [],
        }

    net_income_rows = list(snapshot.get("net_income_rows", []))
    if not net_income_rows and snapshot.get("market_cap") is None:
        return {
            "available": False,
            "note": "La fuente externa no ha devuelto beneficio neto ni capitalizacion reciente.",
            "net_income_rows": [],
        }

    currency_code = snapshot.get("currency_code") or ""
    trend_label, trend_note = describe_net_income_trend(net_income_rows)
    net_income_delta_pct = None
    if len(net_income_rows) >= 2:
        net_income_delta_pct = percentage_change(net_income_rows[-1]["value"], net_income_rows[0]["value"])

    prepared_rows = []
    for row in reversed(net_income_rows):
        prepared_rows.append(
            {
                **row,
                "value_label": format_compact_currency_value(row.get("value"), row.get("currency_code") or currency_code),
                "year_label": str(row["as_of_date"].year),
            }
        )

    market_cap = snapshot.get("market_cap")
    market_cap_as_of_date = snapshot.get("market_cap_as_of_date")
    return {
        "available": True,
        "currency_code": currency_code,
        "net_income_rows": prepared_rows,
        "trend_label": trend_label,
        "trend_note": trend_note,
        "net_income_delta_pct": net_income_delta_pct,
        "market_cap": market_cap,
        "market_cap_label": format_compact_currency_value(market_cap, currency_code),
        "market_cap_as_of_date": market_cap_as_of_date,
        "market_cap_date_label": market_cap_as_of_date.isoformat() if market_cap_as_of_date else "",
        "note": "Beneficio neto anual y capitalizacion segun la serie fundamental mas reciente disponible.",
    }


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


def equity_analysis_notional() -> Decimal:
    configured = getattr(settings, "EQUITIES_ANALYSIS_NOTIONAL", DEFAULT_EQUITY_ANALYSIS_NOTIONAL)
    try:
        amount = Decimal(str(configured))
    except Exception:
        amount = DEFAULT_EQUITY_ANALYSIS_NOTIONAL
    return max(amount, Decimal("1000.00"))


def resolve_projection_analysis_value(position: EquityPosition, latest_price: Decimal | None = None) -> tuple[Decimal, str]:
    current_value = Decimal(position.current_value or 0)
    invested_amount = Decimal(position.invested_amount or 0)
    latest_price = Decimal(latest_price or 0)
    shares = Decimal(position.shares or 0)
    analysis_value = current_value or invested_amount
    if analysis_value <= ZERO and latest_price > ZERO and shares > ZERO:
        analysis_value = latest_price * shares
    if position.is_owned:
        return analysis_value, "actual"
    normalized_floor = equity_analysis_notional()
    if analysis_value <= ZERO or analysis_value < normalized_floor:
        return normalized_floor, "normalized_watchlist"
    return analysis_value, "watchlist_actual"


def build_projection_dividend_income(position: EquityPosition, analysis_value: Decimal) -> Decimal:
    analysis_value = Decimal(analysis_value or 0)
    if analysis_value <= ZERO:
        return ZERO
    current_value = Decimal(position.current_value or 0)
    invested_amount = Decimal(position.invested_amount or 0)
    reference_value = current_value or invested_amount
    annual_dividend_income = Decimal(position.annual_dividend_income or 0)
    if reference_value > ZERO and annual_dividend_income > ZERO:
        return quantize_decimal((annual_dividend_income / reference_value) * analysis_value, "0.01") or ZERO
    return annual_dividend_income


def build_analysis_broker_costs(position: EquityPosition, analysis_value: Decimal, annual_dividend_income: Decimal) -> dict:
    analysis_value = Decimal(analysis_value or 0)
    annual_dividend_income = Decimal(annual_dividend_income or 0)
    broker_costs = estimate_broker_costs(
        broker_name=position.broker,
        trade_channel=position.trade_channel,
        trade_amount=analysis_value,
        valuation_amount=analysis_value,
        annual_dividend_income=annual_dividend_income,
        quote_symbol=position.quote_symbol,
    )
    annual_cost_used, annual_cost_source = resolve_recurring_cost_used(
        position.annual_maintenance_cost,
        broker_costs.get("annual_recurring_cost", ZERO),
    )
    net_dividend_income = annual_dividend_income - broker_costs.get("annual_dividend_fee", ZERO)
    return {
        **broker_costs,
        "annual_cost_used": annual_cost_used,
        "annual_cost_source": annual_cost_source,
        "net_dividend_income": quantize_decimal(net_dividend_income, "0.01") or ZERO,
        "gross_dividend_income": quantize_decimal(annual_dividend_income, "0.01") or ZERO,
    }


def annualize_return_pct(return_pct: Decimal | None, months: int) -> Decimal | None:
    if return_pct is None or months <= 0:
        return None
    base = 1 + (float(return_pct) / 100)
    if base <= 0:
        return Decimal("-100.00")
    annualized = (base ** (12 / months) - 1) * 100
    return Decimal(str(round(annualized, 4)))


def calculate_equivalent_return_pct(
    cumulative_return_pct: Decimal | None,
    elapsed_days: int,
    target_days: Decimal,
) -> Decimal | None:
    if cumulative_return_pct is None or elapsed_days <= 0 or target_days <= 0:
        return None
    base = 1 + (float(cumulative_return_pct) / 100)
    if base <= 0:
        return Decimal("-100.00")
    exponent = float(target_days / Decimal(str(elapsed_days)))
    equivalent = (base ** exponent - 1) * 100
    return Decimal(str(round(equivalent, 4)))


def build_comparable_return_metrics(
    cumulative_return_pct: Decimal | None,
    holding_days: int,
) -> dict:
    return {
        "holding_days": holding_days,
        "monthly_equivalent_return_pct": calculate_equivalent_return_pct(
            cumulative_return_pct,
            holding_days,
            COMPARABLE_MONTH_DAYS,
        ),
        "annual_equivalent_return_pct": calculate_equivalent_return_pct(
            cumulative_return_pct,
            holding_days,
            Decimal("365"),
        ),
    }


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


def consecutive_tail_matches(values: list, predicate) -> int:
    streak = 0
    for value in reversed(values):
        if predicate(value):
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


def build_reference_coefficient_alert(
    history,
    position: EquityPosition,
    correlation: dict,
) -> dict:
    base_payload = {
        "available": False,
        "label": "Sin lectura",
        "tone": "neutral",
        "note": "Todavia no hay suficiente serie reciente para leer si el coeficiente de referencia se deteriora o se mantiene.",
        "trigger_label": "Sin ventana reciente suficiente",
        "long_term_coefficient": correlation.get("coefficient"),
        "recent_coefficient": correlation.get("recent_coefficient"),
        "coefficient_gap": None,
        "deterioration_streak": 0,
        "weak_streak": 0,
        "negative_streak": 0,
        "periods_label": "periodos",
        "window_size": 0,
        "windows_count": 0,
        "trend_slope": None,
    }
    if not history:
        return base_payload

    frequency = infer_reference_frequency(position)
    periods_label = "meses" if frequency == "monthly" else "trimestres"
    rows = [
        {
            "date": point.price_date,
            "stock_close": point.close_price,
            "reference_close": point.benchmark_close,
        }
        for point in history
        if point.benchmark_close is not None
    ]
    buckets = {}
    for row in rows:
        buckets[bucket_label_for_date(row["date"], frequency)] = row
    collapsed = [buckets[label] for label in sorted(buckets.keys())]
    stock_returns, reference_returns = build_return_series_from_collapsed_rows(
        collapsed,
        "stock_close",
        "reference_close",
    )
    recent_window = recent_correlation_window_size(frequency)
    if len(stock_returns) < recent_window + 2:
        return {
            **base_payload,
            "periods_label": periods_label,
            "window_size": recent_window,
        }

    rolling_rows = []
    for end_index in range(recent_window, len(stock_returns) + 1):
        rolling_coefficient = pearson_correlation(
            stock_returns[end_index - recent_window : end_index],
            reference_returns[end_index - recent_window : end_index],
        )
        if rolling_coefficient is None:
            continue
        rolling_rows.append(
            {
                "end_date": collapsed[end_index]["date"],
                "coefficient": rolling_coefficient,
            }
        )

    if len(rolling_rows) < 2:
        return {
            **base_payload,
            "periods_label": periods_label,
            "window_size": recent_window,
            "windows_count": len(rolling_rows),
        }

    baseline = correlation.get("coefficient")
    recent_coefficient = correlation.get("recent_coefficient") or rolling_rows[-1]["coefficient"]
    coefficient_gap = (
        baseline - recent_coefficient
        if baseline is not None and recent_coefficient is not None
        else None
    )
    coefficients = [row["coefficient"] for row in rolling_rows]
    weak_streak = consecutive_tail_matches(
        coefficients,
        lambda value: value is not None and value <= Decimal("0.20"),
    )
    negative_streak = consecutive_tail_matches(
        coefficients,
        lambda value: value is not None and value < ZERO,
    )
    deterioration_streak = consecutive_tail_matches(
        coefficients,
        lambda value: (
            value is not None
            and (
                value <= Decimal("0.20")
                or (
                    baseline is not None
                    and value <= baseline - Decimal("0.18")
                )
            )
        ),
    )
    trend_slope = decimal_linear_slope(coefficients[-4:])

    sell_signal = (
        (negative_streak >= 2)
        or (
            baseline is not None
            and baseline >= Decimal("0.35")
            and deterioration_streak >= 3
            and recent_coefficient is not None
            and recent_coefficient <= Decimal("0.20")
            and (coefficient_gap or ZERO) >= Decimal("0.20")
            and (trend_slope is None or trend_slope < ZERO)
        )
    )
    watch_signal = (
        not sell_signal
        and (
            weak_streak >= 2
            or deterioration_streak >= 2
            or (
                coefficient_gap is not None
                and coefficient_gap >= Decimal("0.12")
                and recent_coefficient is not None
                and recent_coefficient <= Decimal("0.30")
            )
            or correlation.get("stability_label") == "Cambiante"
        )
    )

    trigger_label = (
        f"Reciente {recent_coefficient:.2f} vs 10A {baseline:.2f}"
        if baseline is not None and recent_coefficient is not None
        else "Coeficiente reciente sin comparar"
    )
    if sell_signal:
        note = (
            f"El coeficiente frente a {position.analysis_reference_label} se debilita de forma continuada: "
            f"{deterioration_streak} {periods_label} seguidos con peor lectura y ultimo tramo en {recent_coefficient:.2f}. "
            f"Es una senal de venta si la posicion sigue en cartera."
        )
        return {
            **base_payload,
            "available": True,
            "label": "Vender",
            "tone": "sell",
            "note": note,
            "trigger_label": trigger_label,
            "long_term_coefficient": baseline,
            "recent_coefficient": recent_coefficient,
            "coefficient_gap": coefficient_gap,
            "deterioration_streak": deterioration_streak,
            "weak_streak": weak_streak,
            "negative_streak": negative_streak,
            "periods_label": periods_label,
            "window_size": recent_window,
            "windows_count": len(rolling_rows),
            "trend_slope": trend_slope,
        }

    if watch_signal:
        note = (
            f"El coeficiente frente a {position.analysis_reference_label} ha perdido consistencia en el tramo reciente. "
            f"Todavia no fuerza venta inmediata, pero conviene vigilar si el deterioro se prolonga."
        )
        return {
            **base_payload,
            "available": True,
            "label": "Vigilar",
            "tone": "watch",
            "note": note,
            "trigger_label": trigger_label,
            "long_term_coefficient": baseline,
            "recent_coefficient": recent_coefficient,
            "coefficient_gap": coefficient_gap,
            "deterioration_streak": deterioration_streak,
            "weak_streak": weak_streak,
            "negative_streak": negative_streak,
            "periods_label": periods_label,
            "window_size": recent_window,
            "windows_count": len(rolling_rows),
            "trend_slope": trend_slope,
        }

    note = (
        f"El coeficiente frente a {position.analysis_reference_label} sigue estable en el tramo reciente "
        f"y no deja una senal continuada de venta."
    )
    return {
        **base_payload,
        "available": True,
        "label": "Estable",
        "tone": "neutral",
        "note": note,
        "trigger_label": trigger_label,
        "long_term_coefficient": baseline,
        "recent_coefficient": recent_coefficient,
        "coefficient_gap": coefficient_gap,
        "deterioration_streak": deterioration_streak,
        "weak_streak": weak_streak,
        "negative_streak": negative_streak,
        "periods_label": periods_label,
        "window_size": recent_window,
        "windows_count": len(rolling_rows),
        "trend_slope": trend_slope,
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


def build_cycle_projection_path(
    current_price: Decimal | None,
    annual_return_pct: Decimal | None,
    annualized_volatility_pct: Decimal | None = None,
    current_drawdown_pct: Decimal | None = None,
    cycle_phase: str = "Transicion",
    anchor_date: date | None = None,
    years: int = 5,
    step_months: int = 6,
    step_return_pcts: list[Decimal] | None = None,
) -> list[dict]:
    if current_price in {None, ZERO} or annual_return_pct is None or years <= 0 or step_months <= 0:
        return []

    steps = int((years * 12) / step_months)
    annual_multiplier = max(0.01, 1 + (float(annual_return_pct) / 100))
    base_step_multiplier = annual_multiplier ** (step_months / 12)
    projected_price = Decimal(str(current_price))
    path = []

    for step in range(1, steps + 1):
        if step_return_pcts and step <= len(step_return_pcts):
            step_return_pct = step_return_pcts[step - 1]
            step_multiplier = max(0.80, 1 + (float(step_return_pct) / 100))
        else:
            amplitude_pct = clamp_decimal(
                (annualized_volatility_pct or Decimal("16.00")) * Decimal("0.18"),
                Decimal("1.50"),
                Decimal("5.50"),
            )
            phase_offset = {
                "Correccion": -math.pi / 2,
                "Recuperacion": -math.pi / 4,
                "Expansion": math.pi / 6,
                "Transicion": math.pi / 2,
            }.get(cycle_phase or "Transicion", math.pi / 2)
            cycle_length_steps = max(int(round((12 * 3) / step_months)), 4)
            angle_increment = (2 * math.pi) / cycle_length_steps
            cycle_wave = Decimal(str(round(math.sin(phase_offset + ((step - 1) * angle_increment)), 4)))
            cycle_adjustment_pct = cycle_wave * amplitude_pct * Decimal("0.40")
            if current_drawdown_pct is not None and current_drawdown_pct <= Decimal("-8.00") and step <= 2:
                rebound_bonus_pct = clamp_decimal(abs(current_drawdown_pct) * Decimal("0.08"), ZERO, Decimal("1.80"))
                cycle_adjustment_pct += rebound_bonus_pct / Decimal(str(step))
            elif current_drawdown_pct is not None and current_drawdown_pct >= Decimal("-3.00") and step == 1:
                cycle_adjustment_pct -= Decimal("0.80")
            step_multiplier = base_step_multiplier * max(0.85, 1 + (float(cycle_adjustment_pct) / 100))
        projected_price = Decimal(str(round(float(projected_price) * step_multiplier, 4)))
        months = step * step_months
        if months % 12 == 0:
            label = f"{months // 12}A"
        else:
            label = f"{months}M"
        path.append(
            {
                "label": label,
                "projected_date": anchor_date + timedelta(days=int(round((365 * months) / 12))) if anchor_date else None,
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
        "x_markers": build_time_axis_markers(
            [row["forecast_date"] for row in normalized_rows],
            width=width,
            height=height,
            padding=padding,
        ),
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
    recent_coefficient = correlation.get("recent_coefficient")
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
    if coefficient is not None and recent_coefficient is not None:
        price_return_components.append(
            clamp_decimal((recent_coefficient - coefficient) * Decimal("8.00"), Decimal("-2.50"), Decimal("2.00"))
        )

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

    analysis_value, analysis_value_source = resolve_projection_analysis_value(position, latest_price)
    annual_dividend_income = build_projection_dividend_income(position, analysis_value)
    broker_costs = build_analysis_broker_costs(position, analysis_value, annual_dividend_income)
    net_income_yield_pct = None
    transaction_drag_pct = None
    gross_dividend_yield_pct = None
    annual_cost_used = broker_costs.get("annual_cost_used", ZERO)
    net_dividend_income = broker_costs.get("net_dividend_income", ZERO)
    net_annual_income = net_dividend_income - annual_cost_used
    if analysis_value and analysis_value > 0:
        gross_dividend_yield_pct = (annual_dividend_income / analysis_value) * ONE_HUNDRED
        net_income_yield_pct = (net_annual_income / analysis_value) * ONE_HUNDRED
        transaction_drag_pct = (broker_costs.get("roundtrip_total_cost", ZERO) / analysis_value) * ONE_HUNDRED
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
    if coefficient is not None and recent_coefficient is not None:
        if recent_coefficient <= coefficient - Decimal("0.15"):
            explanation += (
                f" El coeficiente reciente cae a {recent_coefficient:.2f}, por debajo del 10A, "
                "asi que el modelo enfria la lectura de ciclo."
            )
        elif recent_coefficient >= coefficient + Decimal("0.15"):
            explanation += (
                f" El coeficiente reciente sube a {recent_coefficient:.2f}, por encima del 10A, "
                "y refuerza la lectura de ciclo actual."
            )
    if analysis_value_source == "normalized_watchlist":
        explanation += (
            f" Como es un valor en seguimiento, los costes y dividendos se normalizan sobre un ticket analitico de "
            f"{analysis_value:.0f} EUR para que una sola accion de muestra no distorsione la lectura."
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
        "recent_coefficient": recent_coefficient,
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
        "analysis_value_amount": quantize_decimal(analysis_value, "0.01") or ZERO,
        "analysis_value_source": analysis_value_source,
        "annual_dividend_income_used": annual_dividend_income,
        "annual_cost_used": annual_cost_used,
        "annual_cost_source": broker_costs.get("annual_cost_source", "broker"),
        "net_dividend_income": net_dividend_income,
        "broker_costs": broker_costs,
        "reference_label": position.analysis_reference_label,
        "explanation": explanation,
    }


def find_closest_history_point(history, target_date: date, tolerance_days: int = 20):
    candidates = [point for point in history if abs((point.price_date - target_date).days) <= tolerance_days]
    if not candidates:
        return None
    return min(candidates, key=lambda point: (abs((point.price_date - target_date).days), point.price_date))


def build_projection_backtest(
    history,
    position: EquityPosition,
    max_rows: int = 8,
    include_monthly_chart: bool = True,
) -> dict:
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
            "plain_explanation": "Todavia no hay suficiente historico mensual para comprobar como habria funcionado el modelo en el pasado.",
            "error_explanation": "Error medio: diferencia media entre lo que proyecto el modelo y lo que ocurrio realmente 12 meses despues.",
            "direction_explanation": "Acierto de direccion: porcentaje de veces que el modelo acerto si la accion subia o bajaba.",
            "range_explanation": "Dentro del rango: porcentaje de veces que el resultado real quedo dentro del escenario previsto.",
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
            "plain_explanation": "Todavia no hay suficiente historico mensual para comprobar como habria funcionado el modelo en el pasado.",
            "error_explanation": "Error medio: diferencia media entre lo que proyecto el modelo y lo que ocurrio realmente 12 meses despues.",
            "direction_explanation": "Acierto de direccion: porcentaje de veces que el modelo acerto si la accion subia o bajaba.",
            "range_explanation": "Dentro del rango: porcentaje de veces que el resultado real quedo dentro del escenario previsto.",
        }

    recent_rows = list(reversed(rows[-max_rows:]))
    comparisons_count = len(rows)
    monthly_chart = build_backtest_monthly_chart(rows) if include_monthly_chart else {"available": False}
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
        "plain_explanation": (
            "Cada punto mensual responde a una pregunta sencilla: si ese mes hubieramos lanzado la proyeccion a 12 meses, "
            "que habria dicho el modelo y que paso realmente un ano despues."
        ),
        "error_explanation": "Error medio: diferencia media entre la prediccion y el resultado real 12 meses despues.",
        "direction_explanation": "Acierto de direccion: veces que el modelo acerto si la accion terminaria subiendo o bajando.",
        "range_explanation": "Dentro del rango: veces que el resultado real quedo dentro del escenario previsto por el modelo.",
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

    if trade_score >= Decimal("3.20") and projected_return_pct < ZERO:
        note = (
            f"La accion encadena {positive_streak} {periods_label} mejorando frente a su referencia ajustada por coeficiente, "
            f"pero el retorno neto 12M sigue en negativo. Conviene vigilar antes de activar compra."
        )
        return {
            **base_payload,
            "score": quantize_decimal(trade_score) or ZERO,
            "note": note,
            "trend_label": trend_label,
            "trigger_label": f"{positive_streak} {periods_label} con mejora relativa, pero neto 12M negativo",
        }

    if trade_score <= Decimal("-3.20") and projected_return_pct > ZERO:
        note = (
            f"La accion encadena {negative_streak} {periods_label} perdiendo fuerza frente a su referencia, "
            f"pero la proyeccion neta 12M todavia es positiva. Conviene vigilar antes de activar venta."
        )
        return {
            **base_payload,
            "score": quantize_decimal(trade_score) or ZERO,
            "note": note,
            "trend_label": trend_label,
            "trigger_label": f"{negative_streak} {periods_label} con deterioro relativo, pero neto 12M positivo",
        }

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


def build_cycle_projection_yearly_margins(
    current_price: Decimal | None,
    cycle_projection: dict | None,
    *,
    first_year_projected_price: Decimal | None = None,
    first_year_return_pct: Decimal | None = None,
    max_years: int = 5,
) -> list[dict]:
    if current_price in {None, ZERO} or not cycle_projection or not cycle_projection.get("available"):
        return []

    path = cycle_projection.get("path") or []
    checkpoints_by_label = {
        str(step.get("label")): step
        for step in path
        if step.get("label") and step.get("projected_price") is not None
    }
    margins = []
    previous_price = current_price

    for year_number in range(1, max_years + 1):
        step = checkpoints_by_label.get(f"{year_number}A")
        if year_number == 1 and first_year_projected_price is not None:
            projected_price = first_year_projected_price
        elif step:
            projected_price = step.get("projected_price")
        else:
            projected_price = None
        if projected_price is None:
            continue
        if year_number == 1 and first_year_return_pct is not None:
            margin_pct = first_year_return_pct
        else:
            margin_pct = percentage_change(projected_price, previous_price)
        cumulative_return_pct = percentage_change(projected_price, current_price)
        margins.append(
            {
                "year_number": year_number,
                "label": f"AÑO {year_number}",
                "projected_price": projected_price,
                "margin_pct": quantize_decimal(margin_pct),
                "cumulative_return_pct": quantize_decimal(cumulative_return_pct),
            }
        )
        previous_price = projected_price

    return margins


def build_cycle_projection_return_profile(
    current_price: Decimal | None,
    cycle_projection: dict | None,
    *,
    first_year_projected_price: Decimal | None = None,
    first_year_return_pct: Decimal | None = None,
) -> dict:
    yearly_margins = build_cycle_projection_yearly_margins(
        current_price,
        cycle_projection,
        first_year_projected_price=first_year_projected_price,
        first_year_return_pct=first_year_return_pct,
    )
    five_year_return_pct = None
    five_year_annualized_return_pct = None
    if cycle_projection and cycle_projection.get("available"):
        five_year_return_pct = cycle_projection.get("five_year_return_pct")
        if five_year_return_pct is None:
            five_year_row = next((item for item in yearly_margins if item["year_number"] == 5), None)
            if five_year_row:
                five_year_return_pct = five_year_row.get("cumulative_return_pct")
        if five_year_return_pct is not None:
            five_year_annualized_return_pct = annualize_return_pct(five_year_return_pct, 60)
        if five_year_annualized_return_pct is None:
            five_year_annualized_return_pct = cycle_projection.get("annual_return_pct")

    return {
        "five_year_return_pct": quantize_decimal(five_year_return_pct),
        "five_year_annualized_return_pct": quantize_decimal(five_year_annualized_return_pct),
        "yearly_margins": yearly_margins,
    }


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
        coefficient_alert = card.get("coefficient_alert", {})
        cycle_projection = card.get("cycle_projection_5y") or {}
        cycle_return_profile = build_cycle_projection_return_profile(
            position.current_price_per_share,
            cycle_projection,
            first_year_projected_price=projection.get("projected_price"),
            first_year_return_pct=projected_return_pct,
        )
        rows.append(
            {
                "position": position,
                "status_key": card.get("status_key") or ("owned" if position.is_owned else "watchlist"),
                "status_label": card.get("status_label") or position.get_position_kind_display(),
                "status_note": card.get("status_note", ""),
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
                "cycle_return_5y_pct": cycle_return_profile["five_year_return_pct"],
                "cycle_return_annual_pct": cycle_return_profile["five_year_annualized_return_pct"],
                "cycle_yearly_margins": cycle_return_profile["yearly_margins"],
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
                "coefficient_alert_label": coefficient_alert.get("label", "Sin lectura"),
                "coefficient_alert_tone": coefficient_alert.get("tone", "neutral"),
                "coefficient_alert_trigger": coefficient_alert.get("trigger_label", ""),
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
    include_visuals: bool = True,
    include_reference_suggestions: bool = True,
    include_fundamentals: bool | None = None,
) -> dict:
    resolved_status_key = status_key or ("owned" if position.is_owned else "watchlist")
    resolved_status_label = status_label or position.get_position_kind_display()
    resolved_detail_anchor = detail_anchor if detail_anchor is not None else (
        (f"tracked-ticket-{position.id}" if position.is_owned else f"stock-{position.id}") if position.id else ""
    )
    resolved_sector_label = sector_label or resolve_equity_sector_label(
        company_name=position.company_name,
        ticker=position.ticker,
        quote_symbol=position.quote_symbol,
    )
    sale_preview = build_equity_sale_preview(position)
    resolved_include_fundamentals = should_fetch_equity_fundamentals() if include_fundamentals is None else include_fundamentals
    fundamentals = (
        build_equity_fundamentals_summary(position, force_live_fetch=bool(include_fundamentals))
        if resolved_include_fundamentals
        else {
            "available": False,
            "note": "La lectura fundamental se ha omitido en esta vista para priorizar velocidad.",
            "net_income_rows": [],
        }
    )

    if not history:
        return {
            "position": position,
            "status_key": resolved_status_key,
            "status_label": resolved_status_label,
            "detail_anchor": resolved_detail_anchor,
            "sector_label": resolved_sector_label,
            "has_history": False,
            "sale_preview": sale_preview,
            "projection": {"available": False},
            "cycle_projection_5y": {"available": False},
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
            "coefficient_alert": {
                "available": False,
                "label": "Sin lectura",
                "tone": "neutral",
                "note": "Sin historico suficiente para leer el coeficiente de referencia.",
                "trigger_label": "Sin historico suficiente",
            },
            "fundamentals": fundamentals,
            "reference_playbook": {"available": False, "candidates": []},
            "suggested_references": [],
            "historical_chart": {"available": False},
            "projection_12m_chart": {"available": False},
            "cycle_projection_5y_chart": {"available": False},
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
    cycle_projection_5y = build_five_year_cycle_projection(history, position, correlation, cycle_metrics=cycle_metrics)
    projection_backtest = build_projection_backtest(
        history,
        position,
        include_monthly_chart=include_visuals,
    )
    projection_reliability = (
        build_projection_reliability(projection, projection_backtest)
        if projection.get("available")
        else {"label": "Baja", "score": Decimal("40.00")}
    )
    relative_trend = build_relative_strength_trend(history, position, correlation.get("coefficient"))
    coefficient_alert = build_reference_coefficient_alert(history, position, correlation)
    trade_alert = build_trade_alert(
        position,
        projection,
        correlation,
        projection_reliability,
        relative_trend,
        six_month_snapshot,
        one_year_snapshot,
    )
    analysis_value_amount = projection.get("analysis_value_amount") or ZERO
    annual_cost_used = projection.get("annual_cost_used", position.recurring_cost_used) or ZERO
    maintenance_drag_pct = (
        (annual_cost_used / analysis_value_amount) * Decimal("100")
        if analysis_value_amount
        else (
            (position.recurring_cost_used / position.invested_amount) * Decimal("100")
            if position.invested_amount
            else ZERO
        )
    )
    broker_costs = {
        **(projection.get("broker_costs") or position.estimated_broker_costs),
        "annual_cost_used": annual_cost_used,
        "annual_cost_source": projection.get("annual_cost_source", position.recurring_cost_source),
        "net_dividend_income": projection.get("net_dividend_income", position.net_dividend_income),
        "analysis_value_amount": analysis_value_amount,
        "analysis_value_source": projection.get("analysis_value_source", "actual"),
        "annual_dividend_income_used": projection.get("annual_dividend_income_used", position.annual_dividend_income),
    }
    dual_axis_chart = {
        "stock_line": "",
        "reference_line": "",
        "projection_line": "",
        "stock_min_label": "-",
        "stock_max_label": "-",
        "reference_min_label": "-",
        "reference_max_label": "-",
        "x_markers": [],
    }
    historical_chart = {
        "available": False,
        "stock_line": "",
        "stock_min_label": "-",
        "stock_max_label": "-",
        "x_markers": [],
    }
    best_correlation_chart = {
        "available": False,
        "stock_line": "",
        "reference_line": "",
        "stock_min_label": "-",
        "stock_max_label": "-",
        "reference_min_label": "-",
        "reference_max_label": "-",
        "reference_name": "",
        "x_markers": [],
    }
    projection_12m_chart = {
        "available": False,
        "stock_line": "",
        "projection_line": "",
        "stock_min_label": "-",
        "stock_max_label": "-",
        "x_markers": [],
    }
    cycle_projection_5y_chart = {
        "available": False,
        "stock_line": "",
        "projection_line": "",
        "stock_min_label": "-",
        "stock_max_label": "-",
        "x_markers": [],
    }
    if include_visuals:
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
        historical_chart = build_stock_history_chart(history)
        projection_12m_chart = build_projection_12m_chart(history, projection)
        cycle_projection_5y_chart = build_cycle_projection_5y_chart(history, cycle_projection_5y)

    suggested_references = []
    if include_reference_suggestions:
        suggested_references = build_suggested_reference_cards(history, position, reference_cache)
        if include_visuals:
            best_correlation_chart = build_best_correlation_chart(history, suggested_references, reference_cache)

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
        "sale_preview": sale_preview,
        "projection": projection,
        "cycle_projection_5y": cycle_projection_5y,
        "projection_backtest": projection_backtest,
        "projection_reliability": projection_reliability,
        "relative_trend": relative_trend,
        "coefficient_alert": coefficient_alert,
        "trade_alert": trade_alert,
        "fundamentals": fundamentals,
        "suggested_references": suggested_references,
        "historical_chart": historical_chart,
        "best_correlation_chart": best_correlation_chart,
        "projection_12m_chart": projection_12m_chart,
        "cycle_projection_5y_chart": cycle_projection_5y_chart,
    }
    card["reference_playbook"] = (
        build_reference_playbook_from_card(card)
        if include_reference_suggestions
        else {
            "available": False,
            "source_label": "",
            "company_sector": resolved_sector_label or "",
            "current_reference_label": card.get("reference_label"),
            "current_candidate": None,
            "best_candidate": None,
            "candidates": [],
            "return_2025": None,
            "per_2025": None,
            "dividend_yield": None,
            "notes": position.notes,
        }
    )
    return card


def build_equity_history_cards(
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    reference_cache: dict | None = None,
    include_fundamentals: bool | None = None,
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
                include_fundamentals=include_fundamentals,
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
    include_visuals: bool = True,
    include_reference_suggestions: bool = True,
    include_fundamentals: bool | None = None,
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
        status_label="Solo radar",
        detail_anchor="",
        sector_label=company.get("sector", ""),
        include_visuals=include_visuals,
        include_reference_suggestions=include_reference_suggestions,
        include_fundamentals=include_fundamentals,
    )
    if workbook_playbook and workbook_playbook.get("available"):
        card["reference_playbook"] = build_workbook_reference_playbook(company, workbook_snapshot, card=card)
    card["ibex_company"] = company
    return card


def build_ibex_registered_summary(cards: list[dict]) -> dict:
    owned_count = sum(1 for card in cards if card["position"].is_owned)
    watchlist_count = sum(1 for card in cards if not card["position"].is_owned)
    if owned_count and watchlist_count:
        return {
            "status_key": "owned",
            "status_label": "Comprada + seguimiento",
            "status_note": f"{owned_count} compra(s) y {watchlist_count} seguimiento(s) guardados",
        }
    if owned_count:
        return {
            "status_key": "owned",
            "status_label": "Comprada",
            "status_note": f"{owned_count} posicion(es) guardadas" if owned_count > 1 else "Ya esta en cartera",
        }
    return {
        "status_key": "watchlist",
        "status_label": "En seguimiento",
        "status_note": f"{watchlist_count} seguimiento(s) guardados" if watchlist_count > 1 else "La tienes guardada",
    }


def merge_tracked_ibex_card(company: dict, matching_cards: list[dict]) -> dict:
    def card_rank(card: dict) -> tuple[int, int, int]:
        position = card["position"]
        return (
            0 if position.is_owned else 1,
            0 if position.current_price_per_share else 1,
            -(position.id or 0),
        )

    primary = dict(sorted(matching_cards, key=card_rank)[0])
    summary = build_ibex_registered_summary(matching_cards)
    primary["status_key"] = summary["status_key"]
    primary["status_label"] = summary["status_label"]
    primary["status_note"] = summary["status_note"]
    primary["sector_label"] = company.get("sector", "") or primary.get("sector_label") or "Sin sector"
    primary["ibex_company"] = company
    return primary


def build_optimizer_master_cards(history_cards: list[dict], ibex_cards: list[dict]) -> list[dict]:
    ibex_keys = set()
    for card in ibex_cards:
        position = card["position"]
        ibex_keys.update(
            build_security_lookup_keys(
                ticker=position.ticker,
                company_name=position.company_name,
                quote_symbol=position.quote_symbol,
            )
        )
    cards = list(ibex_cards)
    for card in history_cards:
        position = card["position"]
        position_keys = build_security_lookup_keys(
            ticker=position.ticker,
            company_name=position.company_name,
            quote_symbol=position.quote_symbol,
        )
        if position_keys & ibex_keys:
            continue
        cards.append(card)
    return cards


def build_ibex_universe_analysis(
    tracked_history_cards: list[dict],
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    reference_cache: dict | None = None,
    company_limit: int | None = None,
    progress_callback=None,
    include_visuals: bool = False,
    include_reference_suggestions: bool = False,
    include_fundamentals: bool = False,
) -> dict:
    reference_cache = reference_cache if reference_cache is not None else {}
    workbook_snapshot = load_ibex_reference_workbook_snapshot()
    companies = build_ibex_universe_companies(workbook_snapshot)
    tracked_card_map: dict[str, list[dict]] = defaultdict(list)
    for card in tracked_history_cards:
        position = card["position"]
        for key in build_security_lookup_keys(
            ticker=position.ticker,
            company_name=position.company_name,
            quote_symbol=position.quote_symbol,
        ):
            tracked_card_map[key].append(card)

    broker_profile = resolve_analysis_broker_profile(positions)
    tracked_cards = []
    candidate_companies = []
    failures = []

    for company in companies:
        company_keys = build_security_lookup_keys(
            ticker=company.get("ticker", ""),
            company_name=company.get("company_name", ""),
            quote_symbol=company.get("quote_symbol", ""),
        )
        matching_cards = []
        for key in company_keys:
            matching_cards.extend(tracked_card_map.get(key, []))
        unique_matching_cards = []
        seen_card_ids = set()
        for card in matching_cards:
            card_id = id(card)
            if card_id in seen_card_ids:
                continue
            seen_card_ids.add(card_id)
            unique_matching_cards.append(card)
        if unique_matching_cards:
            tracked_cards.append(merge_tracked_ibex_card(company, unique_matching_cards))
            continue
        if company_limit is not None and len(candidate_companies) >= company_limit:
            break
        quote_symbol = company.get("quote_symbol", "")
        if not quote_symbol:
            failures.append(f"{company.get('company_name') or company.get('ticker')}: sin simbolo de cotizacion")
            continue
        candidate_companies.append(company)

    cards = [None] * len(candidate_companies)
    if candidate_companies:
        max_workers = min(ibex_universe_max_workers(), len(candidate_companies))
        if max_workers <= 1:
            for index, company in enumerate(candidate_companies):
                try:
                    cards[index] = build_ibex_universe_card(
                        company,
                        positions,
                        selected_start_date=selected_start_date,
                        selected_end_date=selected_end_date,
                        reference_cache={},
                        workbook_snapshot=workbook_snapshot,
                        broker_profile=broker_profile,
                        include_visuals=include_visuals,
                        include_reference_suggestions=include_reference_suggestions,
                        include_fundamentals=include_fundamentals,
                    )
                except Exception as exc:
                    failures.append(f"{company.get('company_name') or company.get('ticker')}: {exc}")
                if progress_callback:
                    progress_callback(
                        completed_count=index + 1,
                        total_count=len(candidate_companies),
                        company=company,
                        completed_cards=len([card for card in cards if card is not None]),
                        failures_count=len(failures),
                    )
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ibex-analysis") as executor:
                future_map = {
                    executor.submit(
                        build_ibex_universe_card,
                        company,
                        positions,
                        selected_start_date=selected_start_date,
                        selected_end_date=selected_end_date,
                        reference_cache={},
                        workbook_snapshot=workbook_snapshot,
                        broker_profile=broker_profile,
                        include_visuals=include_visuals,
                        include_reference_suggestions=include_reference_suggestions,
                        include_fundamentals=include_fundamentals,
                    ): (index, company)
                    for index, company in enumerate(candidate_companies)
                }
                completed_count = 0
                for future in as_completed(future_map):
                    index, company = future_map[future]
                    try:
                        cards[index] = future.result()
                    except Exception as exc:
                        failures.append(f"{company.get('company_name') or company.get('ticker')}: {exc}")
                    completed_count += 1
                    if progress_callback:
                        progress_callback(
                            completed_count=completed_count,
                            total_count=len(candidate_companies),
                            company=company,
                            completed_cards=len([card for card in cards if card is not None]),
                            failures_count=len(failures),
                        )

    generated_cards = [card for card in cards if card is not None]
    cards = [*tracked_cards, *generated_cards]
    target_count = len(tracked_cards) + len(candidate_companies)

    rows = build_equity_decision_rows(cards)
    buy_alert_count = sum(1 for card in cards if card.get("trade_alert", {}).get("label") == "Comprar")
    sell_alert_count = sum(1 for card in cards if card.get("trade_alert", {}).get("label") == "Vender")
    registered_owned_count = sum(1 for card in tracked_cards if card.get("status_key") == "owned")
    registered_watchlist_count = sum(1 for card in tracked_cards if card.get("status_key") == "watchlist")

    return {
        "cards": cards,
        "tracked_cards": tracked_cards,
        "generated_cards": generated_cards,
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
            "registered_count": len(tracked_cards),
            "registered_owned_count": registered_owned_count,
            "registered_watchlist_count": registered_watchlist_count,
            "radar_only_count": max(len(cards) - len(tracked_cards), 0),
            "failed_count": len(failures),
            "failures": failures[:8],
            "broker_assumption": broker_profile["broker"],
            "trade_channel_label": broker_profile["trade_channel_label"],
            "top_pick": rows[0] if rows else None,
        },
    }


def build_owned_positions_comparable_summary(history_cards: list[dict]) -> dict:
    comparable_cards = [
        card
        for card in history_cards
        if card["position"].is_owned and card.get("sale_preview", {}).get("available")
    ]
    if not comparable_cards:
        return {
            "available": False,
            "positions_count": 0,
        }

    total_committed_capital = sum(
        (card["sale_preview"].get("committed_capital") or ZERO)
        for card in comparable_cards
    )
    weighted_total_return_pct = (
        sum(
            (
                (card["sale_preview"].get("cumulative_margin_pct") or ZERO)
                * (card["sale_preview"].get("committed_capital") or ZERO)
            )
            for card in comparable_cards
        )
        / total_committed_capital
        if total_committed_capital
        else None
    )

    comparable_weight = sum(
        (
            card["sale_preview"].get("committed_capital") or ZERO
        )
        for card in comparable_cards
        if card["sale_preview"].get("monthly_equivalent_return_pct") is not None
    )
    weighted_monthly_return_pct = (
        sum(
            (
                card["sale_preview"]["monthly_equivalent_return_pct"]
                * (card["sale_preview"].get("committed_capital") or ZERO)
            )
            for card in comparable_cards
            if card["sale_preview"].get("monthly_equivalent_return_pct") is not None
        )
        / comparable_weight
        if comparable_weight
        else None
    )
    weighted_annual_return_pct = (
        sum(
            (
                card["sale_preview"]["annualized_margin_pct"]
                * (card["sale_preview"].get("committed_capital") or ZERO)
            )
            for card in comparable_cards
            if card["sale_preview"].get("annualized_margin_pct") is not None
        )
        / comparable_weight
        if comparable_weight
        else None
    )

    best_card = max(
        comparable_cards,
        key=lambda card: card["sale_preview"].get("annualized_margin_pct") or Decimal("-9999"),
    )
    worst_card = min(
        comparable_cards,
        key=lambda card: card["sale_preview"].get("annualized_margin_pct") or Decimal("9999"),
    )
    return {
        "available": True,
        "positions_count": len(comparable_cards),
        "total_committed_capital": quantize_decimal(total_committed_capital, "0.01") or ZERO,
        "weighted_total_return_pct": quantize_decimal(weighted_total_return_pct, "0.01"),
        "weighted_monthly_return_pct": quantize_decimal(weighted_monthly_return_pct, "0.01"),
        "weighted_annual_return_pct": quantize_decimal(weighted_annual_return_pct, "0.01"),
        "best_ticker": best_card["position"].ticker,
        "best_annual_return_pct": quantize_decimal(best_card["sale_preview"].get("annualized_margin_pct"), "0.01"),
        "worst_ticker": worst_card["position"].ticker,
        "worst_annual_return_pct": quantize_decimal(worst_card["sale_preview"].get("annualized_margin_pct"), "0.01"),
        "method_label": "Rentabilidad equivalente compuesta sobre capital comprometido neto si cerraras hoy.",
    }


def build_selected_period_label(
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
) -> str:
    if selected_start_date and selected_end_date:
        return f"{selected_start_date:%Y-%m-%d} a {selected_end_date:%Y-%m-%d}"
    if selected_start_date:
        return f"Desde {selected_start_date:%Y-%m-%d}"
    if selected_end_date:
        return f"Hasta {selected_end_date:%Y-%m-%d}"
    return "Ultimos 90 dias"


def build_equity_analysis_overview(
    positions,
    history_cards: list[dict],
    decision_rows: list[dict],
    ibex_universe_summary: dict | None = None,
    *,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
) -> dict:
    ibex_universe_summary = ibex_universe_summary or {}
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
    comparable_summary = build_owned_positions_comparable_summary(history_cards)

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

    return {
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
        "selected_period_label": build_selected_period_label(selected_start_date, selected_end_date),
        "watchlist_latest_price_count": sum(1 for position in watchlist_positions if position.current_price_per_share),
        "best_decision": decision_rows[0] if decision_rows else ibex_universe_summary.get("top_pick"),
        "comparable_summary": comparable_summary,
    }


def build_equity_analysis_dashboard(
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
    include_ibex_universe: bool = False,
    ibex_company_limit: int | None = None,
    ibex_progress_callback=None,
    ibex_include_visuals: bool = False,
    ibex_include_reference_suggestions: bool = False,
    ibex_include_fundamentals: bool = False,
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
            progress_callback=ibex_progress_callback,
            include_visuals=ibex_include_visuals,
            include_reference_suggestions=ibex_include_reference_suggestions,
            include_fundamentals=ibex_include_fundamentals,
        )
    overview = build_equity_analysis_overview(
        positions,
        history_cards,
        decision_rows,
        ibex_universe["summary"],
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
    )

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
        "optimizer_cards": build_optimizer_master_cards(history_cards, ibex_universe["cards"]),
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


OPTIMIZER_STRATEGY_12M_PRIMARY = "12m_primary"
OPTIMIZER_STRATEGY_5Y_PRIMARY = "5y_primary"
OPTIMIZER_STRATEGIES = {
    OPTIMIZER_STRATEGY_12M_PRIMARY: {
        "label": "12M principal",
        "description": "Prioriza la prevision a 12 meses y usa el ciclo a 5 anos como contraste.",
        "primary_horizon_label": "12 meses",
        "secondary_horizon_label": "5 anos",
        "primary_weight": Decimal("0.78"),
        "secondary_weight": Decimal("0.22"),
    },
    OPTIMIZER_STRATEGY_5Y_PRIMARY: {
        "label": "5A principal",
        "description": "Prioriza el ciclo a 5 anos y usa la prevision a 12 meses como contraste de timing.",
        "primary_horizon_label": "5 anos",
        "secondary_horizon_label": "12 meses",
        "primary_weight": Decimal("0.65"),
        "secondary_weight": Decimal("0.35"),
    },
}


def get_optimizer_strategy_config(strategy_mode: str | None = None) -> dict:
    normalized_mode = str(strategy_mode or OPTIMIZER_STRATEGY_12M_PRIMARY).strip().lower()
    strategy = OPTIMIZER_STRATEGIES.get(normalized_mode)
    if strategy is None:
        normalized_mode = OPTIMIZER_STRATEGY_12M_PRIMARY
        strategy = OPTIMIZER_STRATEGIES[normalized_mode]
    return {"mode": normalized_mode, **strategy}


def build_equity_optimizer_candidate(card: dict, strategy_mode: str = OPTIMIZER_STRATEGY_12M_PRIMARY) -> dict | None:
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
    cycle_projection = card.get("cycle_projection_5y") or {}
    cycle_return_annual_pct = cycle_projection.get("annual_return_pct")
    cycle_return_5y_pct = cycle_projection.get("five_year_return_pct")
    trade_alert = card.get("trade_alert") or {}
    trade_alert_label = trade_alert.get("label") or "Vigilar"
    external_signal = card.get("external_signal") or {}
    external_signal_score = Decimal(str(external_signal.get("score", "0") or "0"))
    external_signal_label = external_signal.get("label") or "Sin prensa reciente"
    trade_signal_adjustment = {
        "Comprar": Decimal("1.75"),
        "Vigilar": ZERO,
        "Vender": Decimal("-4.50"),
    }.get(trade_alert_label, ZERO)
    external_signal_adjustment = clamp_decimal(
        external_signal_score * Decimal("0.35"),
        Decimal("-2.50"),
        Decimal("2.50"),
    )

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
    risk_adjusted_return_pct = (
        (base_return_pct * quality_multiplier)
        - risk_penalty_pct
        + clamp_decimal(external_signal_score * Decimal("0.12"), Decimal("-1.20"), Decimal("1.20"))
    )
    cycle_support_score = ZERO
    if cycle_projection.get("available") and cycle_return_annual_pct is not None:
        cycle_quality_multiplier = (
            (Decimal("0.70") + (safety_score / Decimal("250")))
            * (Decimal("0.70") + (reliability_score / Decimal("250")))
            * history_depth_multiplier
        )
        cycle_risk_penalty_pct = (
            (abs(current_drawdown_pct) * Decimal("0.03") if current_drawdown_pct < ZERO else ZERO)
            + (abs(max_drawdown_pct) * Decimal("0.015") if max_drawdown_pct < ZERO else ZERO)
        )
        cycle_support_score = clamp_decimal(
            (cycle_return_annual_pct * cycle_quality_multiplier) - cycle_risk_penalty_pct,
            Decimal("-8.00"),
            Decimal("10.00"),
        )
        if cycle_return_5y_pct is not None and cycle_return_5y_pct < ZERO:
            cycle_support_score += clamp_decimal(cycle_return_5y_pct * Decimal("0.05"), Decimal("-3.00"), ZERO)
    strategy = get_optimizer_strategy_config(strategy_mode)
    if strategy["mode"] == OPTIMIZER_STRATEGY_5Y_PRIMARY:
        primary_signal_pct = cycle_support_score
        secondary_signal_pct = risk_adjusted_return_pct
    else:
        primary_signal_pct = risk_adjusted_return_pct
        secondary_signal_pct = cycle_support_score
    blended_return_signal_pct = (
        (primary_signal_pct * strategy["primary_weight"])
        + (secondary_signal_pct * strategy["secondary_weight"])
    )
    optimization_score = (
        blended_return_signal_pct
        + income_support_bonus_pct
        - cost_efficiency_penalty_pct
        + trade_signal_adjustment
        + external_signal_adjustment
    )

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
        "strategy_mode": strategy["mode"],
        "strategy_label": strategy["label"],
        "strategy_primary_horizon_label": strategy["primary_horizon_label"],
        "strategy_secondary_horizon_label": strategy["secondary_horizon_label"],
        "reference_label": card["reference_label"],
        "confidence_label": confidence_label,
        "reliability_label": reliability_label,
        "reliability_score": reliability_score,
        "safety_score": safety_score,
        "base_return_pct": base_return_pct,
        "primary_signal_pct": primary_signal_pct,
        "secondary_signal_pct": secondary_signal_pct,
        "risk_adjusted_return_pct": risk_adjusted_return_pct,
        "blended_return_signal_pct": blended_return_signal_pct,
        "optimization_score": optimization_score,
        "downside_return_pct": downside_return_pct,
        "annualized_volatility_pct": annualized_volatility_pct,
        "gross_dividend_yield_pct": gross_dividend_yield_pct,
        "net_income_yield_pct": net_income_yield_pct,
        "transaction_drag_pct": transaction_drag_pct,
        "cycle_projection_available": bool(cycle_projection.get("available")),
        "cycle_return_annual_pct": cycle_return_annual_pct,
        "cycle_return_5y_pct": cycle_return_5y_pct,
        "cycle_support_score": cycle_support_score,
        "years_covered": years_covered,
        "cycle_phase": projection.get("cycle_phase") or "Sin ciclo",
        "max_drawdown_pct": max_drawdown_pct,
        "current_drawdown_pct": current_drawdown_pct,
        "trade_alert_label": trade_alert_label,
        "trade_alert_tone": trade_alert.get("tone", "watch"),
        "trade_alert_note": trade_alert.get("note", ""),
        "trade_signal_adjustment": trade_signal_adjustment,
        "external_signal_label": external_signal_label,
        "external_signal_score": external_signal_score,
        "external_signal_adjustment": external_signal_adjustment,
        "external_signal_note": external_signal.get("note", ""),
        "external_signal_items_count": external_signal.get("items_count", 0),
    }


def filter_positive_optimizer_candidates(
    candidates: list[dict],
    strategy_mode: str,
) -> list[dict]:
    strategy = get_optimizer_strategy_config(strategy_mode)
    if strategy["mode"] == OPTIMIZER_STRATEGY_5Y_PRIMARY:
        return [
            item
            for item in candidates
            if item["optimization_score"] > ZERO
            and item["cycle_support_score"] > ZERO
            and item["trade_alert_label"] != "Vender"
        ]
    return [
        item
        for item in candidates
        if item["optimization_score"] > ZERO
        and item["base_return_pct"] > ZERO
        and item["trade_alert_label"] != "Vender"
    ]


def rank_optimizer_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda item: (
            item["optimization_score"],
            item["primary_signal_pct"],
            item["secondary_signal_pct"],
            item["base_return_pct"],
            item["safety_score"],
        ),
        reverse=True,
    )


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


def scheduled_review_iso_weekdays() -> tuple[int, ...]:
    return tuple(
        weekday
        for weekday in getattr(settings, "EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS", (2, 4))
        if 1 <= int(weekday) <= 7
    )


def build_scheduled_review_weekdays_label(weekdays: tuple[int, ...] | list[int]) -> str:
    labels = [
        SCHEDULED_REVIEW_WEEKDAY_LABELS.get(int(weekday), str(weekday))
        for weekday in weekdays
        if 1 <= int(weekday) <= 7
    ]
    if not labels:
        return "sin dias configurados"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " y " + labels[-1]


def resolve_next_review_date(
    start_date: date,
    weekdays: tuple[int, ...] | list[int],
    *,
    include_today: bool = False,
) -> date | None:
    normalized_weekdays = tuple(sorted({int(weekday) for weekday in weekdays if 1 <= int(weekday) <= 7}))
    if not normalized_weekdays:
        return None
    current_date = start_date if include_today else (start_date + timedelta(days=1))
    for _ in range(14):
        if current_date.isoweekday() in normalized_weekdays:
            return current_date
        current_date += timedelta(days=1)
    return None


def build_round_review_dates(as_of: date, rounds_count: int) -> list[date]:
    if rounds_count <= 0:
        return []
    dates = [as_of]
    weekdays = scheduled_review_iso_weekdays()
    cursor = as_of
    while len(dates) < rounds_count:
        next_date = resolve_next_review_date(cursor, weekdays, include_today=False)
        if next_date is None:
            next_date = cursor + timedelta(days=7)
        dates.append(next_date)
        cursor = next_date
    return dates


def build_equity_round_investment_plan(
    owned_history_cards: list[dict],
    optimizer_cards: list[dict],
    target_total_capital: Decimal,
    max_round_amount: Decimal,
    max_company_pct: Decimal,
    *,
    strategy_mode: str = OPTIMIZER_STRATEGY_12M_PRIMARY,
    as_of: date | None = None,
) -> dict:
    strategy = get_optimizer_strategy_config(strategy_mode)
    if target_total_capital <= 0 or max_round_amount <= 0 or max_company_pct <= 0:
        return {
            "available": False,
            "reason": "Parametros insuficientes para calcular el plan por rondas.",
            "strategy_label": strategy["label"],
        }

    as_of = as_of or django_timezone.localdate()
    company_cap_amount = quantize_decimal((target_total_capital * max_company_pct) / Decimal("100"), "0.01") or ZERO
    current_allocations_by_ticker: dict[str, Decimal] = defaultdict(lambda: ZERO)
    current_allocations_count_by_ticker: dict[str, int] = defaultdict(int)
    for card in owned_history_cards:
        position = card["position"]
        ticker = position.ticker
        current_allocations_by_ticker[ticker] += quantize_decimal(position.current_value, "0.01") or ZERO
        current_allocations_count_by_ticker[ticker] += 1
    current_invested_total = sum(current_allocations_by_ticker.values(), ZERO)
    capital_to_deploy = quantize_decimal(target_total_capital - current_invested_total, "0.01") or ZERO
    if capital_to_deploy <= ZERO:
        return {
            "available": False,
            "reason": "La cartera ya alcanza o supera el paquete objetivo indicado. No hace falta abrir nuevas rondas.",
            "strategy_label": strategy["label"],
            "current_invested_total": current_invested_total,
            "target_total_capital": target_total_capital,
        }

    baseline_map = {
        baseline.position_id: baseline
        for baseline in EquityPurchaseForecastBaseline.objects.filter(
            position_id__in=[
                card["position"].id
                for card in optimizer_cards
                if getattr(card.get("position"), "id", None)
            ]
        )
    }
    candidate_pool = []
    for card in optimizer_cards:
        candidate = build_equity_optimizer_candidate(card, strategy["mode"])
        if candidate is None:
            continue
        position_id = getattr(candidate["position"], "id", None)
        if position_id:
            trade_plan = build_purchase_forecast_trade_plan(baseline_map.get(position_id))
            if trade_plan.get("mode") in {"sale_reentry", "sale_review"}:
                continue
        candidate_pool.append(candidate)

    positive_candidates = rank_optimizer_candidates(
        filter_positive_optimizer_candidates(candidate_pool, strategy["mode"])
    )
    if not positive_candidates:
        return {
            "available": False,
            "reason": "Ahora mismo no hay candidatas con retorno neto positivo suficiente para abrir rondas nuevas.",
            "strategy_label": strategy["label"],
        }

    planned_allocations_by_ticker: dict[str, Decimal] = defaultdict(lambda: ZERO, current_allocations_by_ticker)
    rounds = []
    remaining_capital = capital_to_deploy
    max_rounds = max(int(math.ceil(float(capital_to_deploy / max_round_amount))), 1)
    round_dates = build_round_review_dates(as_of, max_rounds)

    for round_index, round_date in enumerate(round_dates, start=1):
        if remaining_capital <= ZERO:
            break
        best_choice = None
        for candidate in positive_candidates:
            ticker = candidate["position"].ticker
            current_amount = planned_allocations_by_ticker.get(ticker, ZERO)
            available_capacity = quantize_decimal(company_cap_amount - current_amount, "0.01") or ZERO
            proposed_amount = min(max_round_amount, remaining_capital, available_capacity)
            if proposed_amount <= ZERO:
                continue
            scenario = build_optimizer_allocation_scenario(candidate, proposed_amount)
            review = review_optimizer_ticket_efficiency(candidate, proposed_amount, scenario)
            if not review.get("keep"):
                continue
            projected_gain_amount = scenario.get("projected_gain_amount", ZERO)
            choice = {
                "candidate": candidate,
                "amount": proposed_amount,
                "scenario": scenario,
                "projected_gain_amount": projected_gain_amount,
                "current_amount": current_amount,
                "current_weight_pct": quantize_decimal((current_amount / target_total_capital) * Decimal("100"))
                if target_total_capital
                else ZERO,
                "post_weight_pct": quantize_decimal(((current_amount + proposed_amount) / target_total_capital) * Decimal("100"))
                if target_total_capital
                else ZERO,
            }
            choice_key = (
                projected_gain_amount,
                scenario.get("net_projected_return_pct", ZERO),
                candidate["optimization_score"],
                Decimal("1") if current_amount == ZERO else ZERO,
            )
            if best_choice is None or choice_key > best_choice["choice_key"]:
                choice["choice_key"] = choice_key
                best_choice = choice

        if best_choice is None:
            break

        candidate = best_choice["candidate"]
        position = candidate["position"]
        amount = best_choice["amount"]
        planned_allocations_by_ticker[position.ticker] += amount
        remaining_capital = quantize_decimal(remaining_capital - amount, "0.01") or ZERO
        is_existing_holding = best_choice["current_amount"] > ZERO
        rounds.append(
            {
                "round_number": round_index,
                "round_date": round_date,
                "round_date_label": round_date.isoformat(),
                "ticker": position.ticker,
                "company_name": position.company_name,
                "action_label": "Ampliar" if is_existing_holding else "Abrir",
                "status_label": candidate.get("status_label") or "",
                "amount": amount,
                "current_weight_pct": best_choice["current_weight_pct"],
                "post_weight_pct": best_choice["post_weight_pct"],
                "net_projected_return_pct": best_choice["scenario"].get("net_projected_return_pct"),
                "cycle_return_5y_pct": candidate.get("cycle_return_5y_pct"),
                "reliability_label": candidate.get("reliability_label") or "",
                "reliability_score": candidate.get("reliability_score"),
                "trade_alert_label": candidate.get("trade_alert_label") or "",
                "reference_label": candidate.get("reference_label") or "",
                "optimization_score": candidate.get("optimization_score"),
                "note": (
                    f"{'Suma' if is_existing_holding else 'Abre'} posicion en {position.company_name} con "
                    f"retorno neto 12M del {best_choice['scenario'].get('net_projected_return_pct', ZERO):.2f} % "
                    f"y peso final del {best_choice['post_weight_pct'] or ZERO:.2f} % del paquete."
                ),
            }
        )

    current_overweights = [
        {
            "ticker": ticker,
            "amount": amount,
            "weight_pct": quantize_decimal((amount / target_total_capital) * Decimal("100")) if target_total_capital else ZERO,
        }
        for ticker, amount in current_allocations_by_ticker.items()
        if amount > company_cap_amount
    ]
    return {
        "available": bool(rounds),
        "reason": "" if rounds else "Con los limites actuales no sale ninguna ronda que compense en esta foto del radar.",
        "strategy_label": strategy["label"],
        "target_total_capital": target_total_capital,
        "current_invested_total": current_invested_total,
        "capital_to_deploy": capital_to_deploy,
        "remaining_capital": remaining_capital,
        "max_round_amount": max_round_amount,
        "max_company_pct": max_company_pct,
        "company_cap_amount": company_cap_amount,
        "rounds": rounds,
        "rounds_count": len(rounds),
        "new_tickets_count": sum(1 for item in rounds if item["action_label"] == "Abrir"),
        "top_up_rounds_count": sum(1 for item in rounds if item["action_label"] == "Ampliar"),
        "current_overweights": current_overweights,
        "current_overweights_count": len(current_overweights),
        "cadence_label": build_scheduled_review_weekdays_label(scheduled_review_iso_weekdays()),
    }


def build_equity_allocation_plan(
    history_cards: list[dict],
    total_investment: Decimal,
    max_company_pct: Decimal,
    max_total_positions: int = 0,
    max_sector_positions: int = 0,
    selected_sectors: list[str] | None = None,
    strategy_mode: str = OPTIMIZER_STRATEGY_12M_PRIMARY,
) -> dict:
    strategy = get_optimizer_strategy_config(strategy_mode)
    if total_investment <= 0 or max_company_pct <= 0:
        return {
            "available": False,
            "reason": "Parametros insuficientes para calcular la propuesta.",
            "strategy_mode": strategy["mode"],
            "strategy_label": strategy["label"],
        }

    company_cap_amount = (total_investment * max_company_pct) / Decimal("100")
    candidates = [candidate for candidate in (build_equity_optimizer_candidate(card, strategy["mode"]) for card in history_cards) if candidate]

    if not candidates:
        return {
            "available": False,
            "reason": f"Todavia no hay suficientes proyecciones para proponer una distribucion con foco en {strategy['primary_horizon_label']}.",
            "strategy_mode": strategy["mode"],
            "strategy_label": strategy["label"],
        }

    positive_candidates = filter_positive_optimizer_candidates(candidates, strategy["mode"])
    if not positive_candidates:
        return {
            "available": False,
            "reason": (
                "Ahora mismo ninguna accion supera el filtro de retorno neto y riesgo "
                f"para una optimizacion equilibrada con prioridad en {strategy['primary_horizon_label']}."
            ),
            "strategy_mode": strategy["mode"],
            "strategy_label": strategy["label"],
        }

    normalized_selected_sectors = []
    seen_selected_sectors = set()
    for sector in selected_sectors or []:
        normalized_sector = str(sector or "").strip()
        if not normalized_sector or normalized_sector in seen_selected_sectors:
            continue
        seen_selected_sectors.add(normalized_sector)
        normalized_selected_sectors.append(normalized_sector)
    selected_sector_set = set(normalized_selected_sectors)
    selected_sector_excluded_candidates = []
    if selected_sector_set:
        selected_sector_filtered_candidates = []
        for item in positive_candidates:
            sector_label = item.get("sector_label") or "Sin sector"
            if sector_label in selected_sector_set:
                selected_sector_filtered_candidates.append(item)
            else:
                selected_sector_excluded_candidates.append(item)
        positive_candidates = selected_sector_filtered_candidates

    if not positive_candidates and selected_sector_set:
        return {
            "available": False,
            "reason": (
                "Con los sectores elegidos no quedan candidatas validas para construir la propuesta. "
                "Prueba ampliando sectores o relajando otras restricciones."
            ),
            "strategy_mode": strategy["mode"],
            "strategy_label": strategy["label"],
            "selected_sectors": normalized_selected_sectors,
            "selected_sector_note": (
                "La optimizacion se ha limitado a estos sectores: "
                + ", ".join(normalized_selected_sectors)
                + "."
            ),
        }

    ranked_candidates = rank_optimizer_candidates(positive_candidates)
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
            "strategy_mode": strategy["mode"],
            "strategy_label": strategy["label"],
        }

    candidate_pool = ranked_candidates
    position_cap_overflow_count = 0
    if max_total_positions > 0:
        position_cap_overflow_count = max(len(candidate_pool) - max_total_positions, 0)
        iteration_candidates = candidate_pool[:max_total_positions]
        next_candidate_index = len(iteration_candidates)
    else:
        iteration_candidates = candidate_pool
        next_candidate_index = len(candidate_pool)

    ticket_filtered_count = 0
    ticket_filter_reasons = {"entry_drag": 0, "roundtrip_drag": 0, "gain_vs_cost": 0}
    remaining_amount = total_investment
    allocated_amounts = []
    kept_scenarios = []
    max_iterations = len(candidate_pool) + 1

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
        next_iteration_candidates = [
            candidate
            for index, candidate in enumerate(iteration_candidates)
            if index != rejected_index
        ]
        if max_total_positions > 0:
            while len(next_iteration_candidates) < max_total_positions and next_candidate_index < len(candidate_pool):
                next_iteration_candidates.append(candidate_pool[next_candidate_index])
                next_candidate_index += 1
        iteration_candidates = next_iteration_candidates
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
                "strategy_mode": candidate["strategy_mode"],
                "strategy_label": candidate["strategy_label"],
                "allocated_amount": allocated_amount,
                "allocated_weight_pct": (allocated_amount / total_investment) * Decimal("100"),
                "base_return_pct": candidate["base_return_pct"],
                "primary_signal_pct": candidate["primary_signal_pct"],
                "secondary_signal_pct": candidate["secondary_signal_pct"],
                "adjusted_return_pct": candidate["risk_adjusted_return_pct"],
                "blended_return_signal_pct": candidate["blended_return_signal_pct"],
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
                "external_signal_label": candidate["external_signal_label"],
                "external_signal_score": candidate["external_signal_score"],
                "external_signal_note": candidate["external_signal_note"],
                "external_signal_items_count": candidate["external_signal_items_count"],
                "annualized_volatility_pct": candidate["annualized_volatility_pct"],
                "years_covered": candidate["years_covered"],
                "cycle_phase": candidate["cycle_phase"],
                "cycle_projection_available": candidate["cycle_projection_available"],
                "cycle_return_annual_pct": candidate["cycle_return_annual_pct"],
                "cycle_return_5y_pct": candidate["cycle_return_5y_pct"],
                "cycle_yearly_margins": build_cycle_projection_yearly_margins(
                    candidate["position"].current_price_per_share,
                    candidate["card"].get("cycle_projection_5y") or {},
                    first_year_projected_price=candidate["projection"].get("projected_price"),
                    first_year_return_pct=candidate["base_return_pct"],
                ),
                "cycle_support_score": candidate["cycle_support_score"],
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
    cycle_allocated_amount_total = sum(
        (item["allocated_amount"] for item in allocations if item.get("cycle_return_annual_pct") is not None),
        ZERO,
    )
    weighted_cycle_return_annual_pct = (
        sum(
            (
                (item["cycle_return_annual_pct"] * item["allocated_amount"])
                for item in allocations
                if item.get("cycle_return_annual_pct") is not None
            ),
            ZERO,
        )
        / cycle_allocated_amount_total
        if cycle_allocated_amount_total
        else None
    )
    weighted_cycle_return_5y_pct = (
        sum(
            (
                (item["cycle_return_5y_pct"] * item["allocated_amount"])
                for item in allocations
                if item.get("cycle_return_5y_pct") is not None
            ),
            ZERO,
        )
        / cycle_allocated_amount_total
        if cycle_allocated_amount_total
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
    external_signal_used_count = sum(1 for item in allocations if item.get("external_signal_items_count"))

    if remaining_amount > ZERO:
        reserve_reason = (
            "Queda caja en reserva porque el filtro de riesgo/rentabilidad solo deja pasar las ideas con retorno neto positivo, "
            "riesgo asumible y suficiente calidad historica."
        )
        if max_total_positions > 0 and (company_cap_amount * Decimal(str(max_total_positions))) < total_investment:
            reserve_reason += (
                " Ademas, el maximo total de empresas combinado con el peso maximo por empresa impide asignar todo el capital sin saltarse tus propios limites."
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
    if selected_sector_set:
        selected_sector_note = "La optimizacion se limita a estos sectores: " + ", ".join(normalized_selected_sectors) + "."
        if selected_sector_excluded_candidates:
            selected_sector_note += (
                f" {len(selected_sector_excluded_candidates)} candidata(s) han quedado fuera por no pertenecer a los sectores permitidos."
            )
    else:
        selected_sector_note = ""
    if max_total_positions > 0:
        position_limit_note = f"Se aplica un maximo total de {max_total_positions} empresa(s) en la cartera propuesta."
        if position_cap_overflow_count:
            position_limit_note += (
                f" En el ranking inicial habia {position_cap_overflow_count} candidata(s) adicionales fuera del corte por capacidad."
            )
    else:
        position_limit_note = ""
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
            or position_limit_note
            or sector_limit_note
            or "Con las tarifas actuales del broker, las compras que caben por peso maximo no compensan el coste fijo+variable por operacion."
        )

    top_pick = allocations[0] if allocations else None
    return {
        "available": bool(allocations),
        "reason": reason,
        "strategy_mode": strategy["mode"],
        "strategy_label": strategy["label"],
        "strategy_description": strategy["description"],
        "primary_horizon_label": strategy["primary_horizon_label"],
        "secondary_horizon_label": strategy["secondary_horizon_label"],
        "allocations": allocations,
        "total_investment": total_investment,
        "max_company_pct": max_company_pct,
        "max_total_positions": max_total_positions,
        "company_cap_amount": company_cap_amount,
        "selected_sectors": normalized_selected_sectors,
        "selected_sector_note": selected_sector_note,
        "allocated_amount_total": allocated_amount_total,
        "cash_reserve_amount": remaining_amount,
        "projected_gain_total": projected_gain_total,
        "weighted_return_pct": weighted_return_pct,
        "weighted_low_return_pct": weighted_low_return_pct,
        "weighted_safety_score": weighted_safety_score,
        "weighted_reliability_score": weighted_reliability_score,
        "weighted_cycle_return_annual_pct": weighted_cycle_return_annual_pct,
        "weighted_cycle_return_5y_pct": weighted_cycle_return_5y_pct,
        "weighted_volatility_pct": weighted_volatility_pct,
        "net_dividend_income_total": net_dividend_income_total,
        "annual_cost_total": annual_cost_total,
        "roundtrip_cost_total": roundtrip_cost_total,
        "owned_allocations_count": owned_allocations_count,
        "watchlist_allocations_count": watchlist_allocations_count,
        "ibex_allocations_count": ibex_allocations_count,
        "max_sector_positions": max_sector_positions,
        "position_cap_filtered_count": position_cap_overflow_count,
        "position_cap_note": position_limit_note,
        "sectors_used": sectors_used,
        "sectors_used_count": len(sectors_used),
        "external_signal_used_count": external_signal_used_count,
        "sector_filtered_count": len(sector_excluded_candidates),
        "sector_filter_note": sector_limit_note,
        "ticket_filtered_count": ticket_filtered_count,
        "ticket_filter_reasons": ticket_filter_reasons,
        "ticket_filter_note": ticket_filter_note,
        "reserve_reason": reserve_reason,
        "top_pick": top_pick,
        "methodology_note": (
            f"Esta ejecucion usa la estrategia {strategy['label']}: "
            f"prioriza la lectura de {strategy['primary_horizon_label']} y deja {strategy['secondary_horizon_label']} como contraste secundario para reforzar o penalizar la jerarquia final. "
            "Sigue integrando dividendos netos, costes de compra/venta y mantenimiento, seguridad, fiabilidad del modelo, alertas de tendencia, senal externa reciente de prensa y riesgo historico. "
            "Las cifras monetarias del informe siguen expresadas a 12 meses para mantener comparables dividendos, costes, caja y peor escenario, aunque la jerarquia de seleccion pueda priorizar 5 anos. "
            "eficiencia real del ticket de compra y, si lo marcas, un maximo total de empresas y diversificacion maxima por sector. En la version robusta se "
            "analiza siempre todo el IBEX, no solo los valores que ya tienes en seguimiento."
        ),
    }
