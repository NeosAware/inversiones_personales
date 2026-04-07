from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone as django_timezone

from banking.services import load_rows_from_workbook
from portfolio.ownership import AssetOwnershipCategory

from .models import EquityPosition, EquityPriceHistory


ZERO = Decimal("0.00")
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


def fetch_market_series(symbol: str, range_key: str = "5y", interval: str = "1d") -> MarketSeries:
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


def fetch_ecb_reference_series(series_key: str, series_name: str, last_n_observations: int = 120) -> MarketSeries:
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
    start_date = end_date - timedelta(days=365 * 5)
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
    stock_values,
    reference_values,
    width: int = 640,
    height: int = 220,
    padding: int = 18,
) -> dict:
    stock_filtered = [Decimal(str(value)) for value in stock_values if value is not None]
    reference_filtered = [Decimal(str(value)) for value in reference_values if value is not None]
    if len(stock_filtered) < 2:
        return {
            "stock_line": "",
            "reference_line": "",
            "stock_min_label": "-",
            "stock_max_label": "-",
            "reference_min_label": "-",
            "reference_max_label": "-",
        }

    stock_min = min(stock_filtered)
    stock_max = max(stock_filtered)
    if stock_min == stock_max:
        stock_max += Decimal("1")

    if reference_filtered:
        reference_min = min(reference_filtered)
        reference_max = max(reference_filtered)
        if reference_min == reference_max:
            reference_max += Decimal("1")
    else:
        reference_min = reference_max = None

    def scale_series(values, series_min: Decimal | None, series_max: Decimal | None) -> str:
        if series_min is None or series_max is None:
            return ""
        span_x = width - 2 * padding
        span_y = height - 2 * padding
        total_points = len(values) - 1 or 1
        points = []
        for index, raw_value in enumerate(values):
            if raw_value is None:
                continue
            value = Decimal(str(raw_value))
            x = padding + (span_x * index / total_points)
            normalized = (value - series_min) / (series_max - series_min)
            y = height - padding - (normalized * span_y)
            points.append(f"{x:.1f},{y:.1f}")
        return " ".join(points)

    return {
        "stock_line": scale_series(stock_values, stock_min, stock_max),
        "reference_line": scale_series(reference_values, reference_min, reference_max),
        "stock_min_label": format_axis_value(stock_min),
        "stock_max_label": format_axis_value(stock_max),
        "reference_min_label": format_axis_value(reference_min),
        "reference_max_label": format_axis_value(reference_max),
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
    if frequency == "quarterly":
        quarter = ((value.month - 1) // 3) + 1
        return f"{value.year}-Q{quarter}"
    return value.strftime("%Y-%m")


def collapse_history_to_frequency(history, frequency: str) -> list:
    buckets = {}
    for point in history:
        buckets[bucket_label_for_date(point.price_date, frequency)] = point
    return [buckets[label] for label in sorted(buckets.keys())]


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

    stock_returns = []
    reference_returns = []
    for previous, current in zip(collapsed, collapsed[1:]):
        stock_return = percentage_change(current.get("stock_close"), previous.get("stock_close"))
        reference_return = percentage_change(current.get("reference_close"), previous.get("reference_close"))
        if stock_return is None or reference_return is None:
            continue
        stock_returns.append(stock_return)
        reference_returns.append(reference_return)

    coefficient = pearson_correlation(stock_returns[-12:], reference_returns[-12:])
    return {
        "frequency": frequency,
        "coefficient": coefficient,
        "label": describe_correlation(coefficient),
        "observations_count": len(stock_returns),
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


def build_projection_confidence(
    coefficient: Decimal | None,
    observations_count: int,
    monthly_returns_count: int,
) -> dict:
    score = 0
    absolute_coefficient = abs(coefficient) if coefficient is not None else None
    if absolute_coefficient is not None:
        if absolute_coefficient >= Decimal("0.25"):
            score += 1
        if absolute_coefficient >= Decimal("0.50"):
            score += 1
    if observations_count >= 4:
        score += 1
    if observations_count >= 8:
        score += 1
    if monthly_returns_count >= 4:
        score += 1

    if score >= 4:
        return {
            "label": "Alta",
            "note": "La relacion reciente entre accion y referencia tiene bastante base para una lectura orientativa.",
        }
    if score >= 2:
        return {
            "label": "Media",
            "note": "La lectura es util, pero conviene verla como una guia y no como una estimacion cerrada.",
        }
    return {
        "label": "Baja",
        "note": "Hay poca base historica o una relacion debil con la referencia, asi que la propuesta es muy tentativa.",
    }


def project_price_from_return(current_price: Decimal | None, return_pct: Decimal | None) -> Decimal | None:
    if current_price in {None, ZERO} or return_pct is None:
        return None
    multiplier = Decimal("1") + (return_pct / Decimal("100"))
    if multiplier <= 0:
        multiplier = Decimal("0.01")
    return current_price * multiplier


def build_projection_path(current_price: Decimal | None, annual_return_pct: Decimal | None) -> list[dict]:
    if current_price in {None, ZERO} or annual_return_pct is None:
        return []
    annual_multiplier = max(0.01, 1 + (float(annual_return_pct) / 100))
    path = []
    for months, label in ((3, "3M"), (6, "6M"), (9, "9M"), (12, "12M")):
        projected_multiplier = annual_multiplier ** (months / 12)
        projected_price = Decimal(str(round(float(current_price) * projected_multiplier, 4)))
        path.append(
            {
                "label": label,
                "projected_price": projected_price,
            }
        )
    return path


def build_one_year_projection(history, position: EquityPosition, correlation: dict, six_month_snapshot: dict) -> dict:
    if not history or not six_month_snapshot.get("available") or six_month_snapshot.get("stock_return_pct") is None:
        return {"available": False}

    latest_price = history[-1].close_price if history[-1].close_price else position.current_price_per_share
    stock_6m_return_pct = six_month_snapshot["stock_return_pct"]
    reference_6m_return_pct = six_month_snapshot.get("benchmark_return_pct")
    coefficient = correlation.get("coefficient")
    observations_count = correlation.get("observations_count", 0)

    stock_signal = build_projection_signal(stock_6m_return_pct, 6)
    if stock_signal is None:
        return {"available": False}

    base_return_pct = stock_signal
    reference_signal = build_projection_signal(reference_6m_return_pct, 6) if reference_6m_return_pct is not None else None
    if coefficient is not None and reference_signal is not None:
        reference_weight = abs(coefficient) * Decimal("0.30")
        momentum_weight = Decimal("1") - reference_weight
        base_return_pct = (stock_signal * momentum_weight) + (reference_signal * coefficient * Decimal("0.30"))

    evidence_factor = Decimal("0.70")
    if observations_count >= 8:
        evidence_factor = Decimal("1.00")
    elif observations_count >= 5:
        evidence_factor = Decimal("0.90")
    elif observations_count >= 3:
        evidence_factor = Decimal("0.80")
    base_return_pct *= evidence_factor
    base_return_pct = clamp_decimal(base_return_pct, Decimal("-60.00"), Decimal("90.00"))

    monthly_returns = build_recent_monthly_stock_returns(history, months_back=6)
    monthly_volatility_pct = standard_deviation_decimal(monthly_returns)
    annualized_volatility_pct = (
        monthly_volatility_pct * Decimal(str(round(math.sqrt(12), 4)))
        if monthly_volatility_pct is not None
        else None
    )
    confidence = build_projection_confidence(coefficient, observations_count, len(monthly_returns))
    band_pct = annualized_volatility_pct * Decimal("0.70") if annualized_volatility_pct is not None else Decimal("16.00")
    if confidence["label"] == "Alta":
        band_pct -= Decimal("2.00")
    elif confidence["label"] == "Baja":
        band_pct += Decimal("4.00")
    band_pct = clamp_decimal(band_pct, Decimal("8.00"), Decimal("30.00"))

    low_return_pct = clamp_decimal(base_return_pct - band_pct, Decimal("-80.00"), Decimal("120.00"))
    high_return_pct = clamp_decimal(base_return_pct + band_pct, Decimal("-80.00"), Decimal("140.00"))

    if coefficient is None or reference_6m_return_pct is None:
        explanation = (
            f"La propuesta a 12 meses se apoya sobre todo en el tono del ultimo semestre "
            f"({stock_6m_return_pct:.2f} %) porque la relacion con {position.analysis_reference_label} "
            f"todavia no tiene suficiente base."
        )
    else:
        reference_direction = "acompanando" if coefficient >= 0 else "en sentido inverso a"
        explanation = (
            f"Se prolonga de forma prudente el tono de 6M de la accion ({stock_6m_return_pct:.2f} %) y se ajusta con "
            f"{position.analysis_reference_label} ({reference_6m_return_pct:.2f} % en 6M), "
            f"{reference_direction} la referencia con una correlacion de {coefficient:.2f}."
        )

    return {
        "available": True,
        "base_return_pct": base_return_pct,
        "low_return_pct": low_return_pct,
        "high_return_pct": high_return_pct,
        "projected_price": project_price_from_return(latest_price, base_return_pct),
        "low_price": project_price_from_return(latest_price, low_return_pct),
        "high_price": project_price_from_return(latest_price, high_return_pct),
        "quarterly_path": build_projection_path(latest_price, base_return_pct),
        "confidence_label": confidence["label"],
        "confidence_note": confidence["note"],
        "stock_6m_return_pct": stock_6m_return_pct,
        "reference_6m_return_pct": reference_6m_return_pct,
        "coefficient": coefficient,
        "reference_label": position.analysis_reference_label,
        "explanation": explanation,
    }


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


def build_equity_history_cards(
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
) -> list[dict]:
    cards = []
    reference_cache: dict = {}
    for position in positions:
        history = list(position.price_history.order_by("price_date"))
        if not history:
            cards.append(
                {
                    "position": position,
                    "has_history": False,
                    "projection": {"available": False},
                    "suggested_references": [],
                }
            )
            continue

        first_price = history[0].close_price
        first_benchmark = next((point.benchmark_close for point in history if point.benchmark_close is not None), None)
        stock_series = [point.close_price for point in history]
        benchmark_series = [point.benchmark_close for point in history]
        dual_axis_chart = build_dual_axis_chart(stock_series, benchmark_series)

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
                "3M",
                start_date=latest_point.price_date - timedelta(days=90),
                end_date=latest_point.price_date,
            )

        period_snapshots = [
            build_period_snapshot(history, "1M", start_date=latest_point.price_date - timedelta(days=30), end_date=latest_point.price_date),
            build_period_snapshot(history, "3M", start_date=latest_point.price_date - timedelta(days=90), end_date=latest_point.price_date),
            build_period_snapshot(history, "6M", start_date=latest_point.price_date - timedelta(days=182), end_date=latest_point.price_date),
            build_period_snapshot(history, "1Y", start_date=latest_point.price_date - timedelta(days=365), end_date=latest_point.price_date),
        ]
        correlation = build_reference_correlation(history, position)
        six_month_snapshot = next((snapshot for snapshot in period_snapshots if snapshot["label"] == "6M"), {"available": False})
        projection = build_one_year_projection(history, position, correlation, six_month_snapshot)
        maintenance_drag_pct = (
            (position.annual_maintenance_cost / position.invested_amount) * Decimal("100")
            if position.invested_amount
            else ZERO
        )

        cards.append(
            {
                "position": position,
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
                "dual_axis_chart": dual_axis_chart,
                "period_snapshots": period_snapshots,
                "selected_period": selected_period,
                "net_unrealized_gain": position.unrealized_gain_after_costs,
                "net_unrealized_return_pct": position.unrealized_return_pct,
                "net_annual_income": position.net_annual_income,
                "maintenance_drag_pct": maintenance_drag_pct,
                "price_vs_cost_pct": percentage_change(position.current_price_per_share, position.average_cost_per_share),
                "correlation": correlation,
                "projection": projection,
                "suggested_references": build_suggested_reference_cards(history, position, reference_cache),
            }
        )
    return cards


def build_equity_analysis_dashboard(
    positions,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
) -> dict:
    history_cards = build_equity_history_cards(
        positions,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
    )
    owned_positions = [position for position in positions if position.is_owned]
    watchlist_positions = [position for position in positions if not position.is_owned]
    current_value_total = sum((position.current_value for position in owned_positions), ZERO)
    invested_amount_total = sum((position.invested_amount for position in owned_positions), ZERO)
    annual_dividends_total = sum((position.annual_dividend_income for position in owned_positions), ZERO)
    annual_maintenance_total = sum((position.annual_maintenance_cost for position in owned_positions), ZERO)
    net_annual_income_total = sum((position.net_annual_income for position in owned_positions), ZERO)
    unrealized_gain_total = sum((position.unrealized_gain_after_costs for position in owned_positions), ZERO)

    weighted_periods = []
    for label in ("1M", "3M", "6M", "1Y"):
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

    overview = {
        "positions_count": len(positions),
        "owned_positions_count": len(owned_positions),
        "watchlist_positions_count": len(watchlist_positions),
        "invested_amount": invested_amount_total,
        "current_value": current_value_total,
        "annual_dividends_total": annual_dividends_total,
        "annual_maintenance_total": annual_maintenance_total,
        "net_annual_income_total": net_annual_income_total,
        "unrealized_gain_total": unrealized_gain_total,
        "unrealized_return_pct": percentage_change(current_value_total - annual_maintenance_total, invested_amount_total),
        "latest_sync_at": latest_sync_at,
        "latest_price_date": latest_price_date,
        "weighted_selected_return": weighted_selected_return,
        "weighted_periods": weighted_periods,
        "selected_period_label": selected_period_label,
        "watchlist_latest_price_count": sum(1 for position in watchlist_positions if position.current_price_per_share),
    }

    return {
        "overview": overview,
        "history_cards": history_cards,
        "owned_positions": owned_positions,
        "watchlist_positions": watchlist_positions,
        "owned_history_cards": [card for card in history_cards if card["position"].is_owned],
        "watchlist_history_cards": [card for card in history_cards if not card["position"].is_owned],
    }
