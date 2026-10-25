"""
Real Exchange Rate Forecast App
===============================

A Streamlit app for an international finance class. It downloads monthly CPI
and exchange-rate data from the IMF SDMX 3.0 API, builds the bilateral nominal
and real exchange rates between two countries, and shows the real exchange rate
as an index relative to its sample average (RER*).

Run locally with:
    streamlit run app.py
"""

from __future__ import annotations

import io
import re
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"          # IMF Statistics Department owns the CPI and ER dataflows
CPI_FLOW = "CPI"            # Consumer Price Index dataflow
ER_FLOW = "ER"              # Exchange Rates dataflow
FLOW_VERSION = "+"          # "+" = latest stable version of the dataflow
START_PERIOD = "1976-01"    # earliest month we request
CACHE_TTL = 24 * 60 * 60    # cache API results for 24 hours (seconds)

EURO_AREA = "G163"
USA = "USA"

# A browser-like User-Agent; some API gateways reject the default python agent.
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0"
JSON_ACCEPT = "application/json"

REQUEST_TIMEOUT = 45        # seconds per HTTP request
MAX_RETRIES = 3             # attempts per request
RETRY_STATUS = {429, 500, 502, 503, 504}

# Fallback country list (used only if the IMF codelist request fails).
FALLBACK_COUNTRIES: dict[str, str] = {
    "ARG": "Argentina",
    "AUS": "Australia",
    "AUT": "Austria",
    "BEL": "Belgium",
    "BRA": "Brazil",
    "CAN": "Canada",
    "CHE": "Switzerland",
    "CHL": "Chile",
    "CHN": "China",
    "COL": "Colombia",
    "CZE": "Czech Republic",
    "DEU": "Germany",
    "DNK": "Denmark",
    "EGY": "Egypt",
    "ESP": "Spain",
    "FIN": "Finland",
    "FRA": "France",
    "G163": "Euro area",
    "GBR": "United Kingdom",
    "GRC": "Greece",
    "HUN": "Hungary",
    "IDN": "Indonesia",
    "IND": "India",
    "IRL": "Ireland",
    "ISL": "Iceland",
    "ISR": "Israel",
    "ITA": "Italy",
    "JPN": "Japan",
    "KOR": "Korea",
    "MEX": "Mexico",
    "MYS": "Malaysia",
    "NGA": "Nigeria",
    "NLD": "Netherlands",
    "NOR": "Norway",
    "NZL": "New Zealand",
    "PAK": "Pakistan",
    "PER": "Peru",
    "PHL": "Philippines",
    "POL": "Poland",
    "PRT": "Portugal",
    "RUS": "Russian Federation",
    "SAU": "Saudi Arabia",
    "SGP": "Singapore",
    "SWE": "Sweden",
    "THA": "Thailand",
    "TUR": "Türkiye",
    "USA": "United States",
    "ZAF": "South Africa",
}

# Currency codes used to label axes. Countries not listed are labelled
# generically as "<ISO3> currency".
CURRENCIES: dict[str, str] = {
    "ARG": "ARS", "AUS": "AUD", "BRA": "BRL", "CAN": "CAD", "CHE": "CHF",
    "CHL": "CLP", "CHN": "CNY", "COL": "COP", "CZE": "CZK", "DNK": "DKK",
    "EGY": "EGP", "G163": "EUR", "GBR": "GBP", "HKG": "HKD", "HUN": "HUF",
    "IDN": "IDR", "IND": "INR", "ISL": "ISK", "ISR": "ILS", "JPN": "JPY",
    "KOR": "KRW", "MEX": "MXN", "MYS": "MYR", "NGA": "NGN", "NOR": "NOK",
    "NZL": "NZD", "PAK": "PKR", "PER": "PEN", "PHL": "PHP", "POL": "PLN",
    "RUS": "RUB", "SAU": "SAR", "SGP": "SGD", "SWE": "SEK", "THA": "THB",
    "TUR": "TRY", "USA": "USD", "ZAF": "ZAR",
}

COLOR_REAL = "#1f77b4"      # blue
COLOR_NOMINAL = "#d62728"   # red
COLOR_REF = "#7f7f7f"       # grey


# ---------------------------------------------------------------------------
# Fetching (network) helpers
# ---------------------------------------------------------------------------

def http_get(url: str, params: dict | None, accept: str) -> requests.Response | None:
    """GET a URL with a timeout and simple retry/backoff on transient errors.

    Returns the Response for any final status code, or raises
    requests.RequestException if every attempt failed at the network level.
    A 404 is returned (not raised) so callers can treat it as "no data".
    """
    headers = {**HEADERS_BASE, "Accept": accept}
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise requests.RequestException(f"Network error after {MAX_RETRIES} attempts: {last_exc}")


def build_data_url(flow: str, key: str) -> str:
    """Build an SDMX 3.0 data URL.

    Pattern: {BASE}/data/dataflow/{agency}/{flow}/{version}/{key}
    Example: .../data/dataflow/IMF.STA/CPI/+/JPN.CPI._T.IX.M
    """
    return f"{BASE_URL}/data/dataflow/{AGENCY}/{flow}/{FLOW_VERSION}/{key}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_periods(labels: pd.Series) -> pd.Series:
    """Convert IMF period labels ("2020-M01", "2020M01", "2020-01") to
    month-start Timestamps. Unparseable labels become NaT."""
    parts = labels.astype(str).str.strip().str.extract(r"^(\d{4})-?M?(\d{1,2})$")
    text = parts[0] + "-" + parts[1].str.zfill(2)
    return pd.to_datetime(text, format="%Y-%m", errors="coerce")


def parse_sdmx_csv(text: str) -> pd.Series:
    """Parse an SDMX-CSV response into a monthly float Series indexed by date.

    Non-numeric or missing values are dropped. When the API finds no matching
    data it may return a header plus an empty row, so an empty Series is a
    normal result.
    """
    if not text or not text.strip():
        return pd.Series(dtype=float)
    df = pd.read_csv(io.StringIO(text), dtype=str)
    if "TIME_PERIOD" not in df.columns or "OBS_VALUE" not in df.columns:
        return pd.Series(dtype=float)
    dates = parse_periods(df["TIME_PERIOD"])
    values = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    ok = dates.notna() & values.notna() & np.isfinite(values)
    series = pd.Series(values[ok].to_numpy(), index=pd.DatetimeIndex(dates[ok]), dtype=float)
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


# ---------------------------------------------------------------------------
# Cached data access
# ---------------------------------------------------------------------------

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_imf_series(flow: str, key: str, start: str = START_PERIOD) -> tuple[pd.Series, str | None]:
    """Download one monthly IMF series as a pandas Series.

    Returns (series, error_message). On any failure the series is empty and
    error_message explains why; the function never raises. Warnings are shown
    by the caller (not here) so that cached results don't replay stale UI.
    """
    url = build_data_url(flow, key)
    # SDMX 3.0 time filter: c[TIME_PERIOD]=ge:1976-01
    params = {"c[TIME_PERIOD]": f"ge:{start}"}
    try:
        resp = http_get(url, params, CSV_ACCEPT)
    except requests.RequestException as exc:
        return pd.Series(dtype=float), str(exc)
    if resp.status_code == 404:
        return pd.Series(dtype=float), "no data found (HTTP 404)"
    if resp.status_code != 200:
        return pd.Series(dtype=float), f"IMF API returned HTTP {resp.status_code}"
    try:
        series = parse_sdmx_csv(resp.text)
    except Exception as exc:  # malformed CSV, unexpected layout, etc.
        return pd.Series(dtype=float), f"could not parse response ({exc})"
    if series.empty:
        return series, "no observations returned"
    return series, None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_country_list() -> tuple[dict[str, str], bool]:
    """Return ({code: name}, from_api).

    Reads the IMF country codelist (CL_COUNTRY, the reference-area codelist used
    by the CPI and ER dataflows). Keeps three-letter ISO codes plus the Euro area
    (G163) and drops the other regional aggregates. Falls back to a hardcoded
    list if the request fails.
    """
    url = f"{BASE_URL}/structure/codelist/IMF/CL_COUNTRY/+"
    try:
        resp = http_get(url, None, JSON_ACCEPT)
        resp.raise_for_status()
        codes = resp.json()["data"]["codelists"][0]["codes"]
        countries = {
            c["id"]: c.get("name", c["id"])
            for c in codes
            if re.fullmatch(r"[A-Z]{3}", c["id"]) or c["id"] == EURO_AREA
        }
        if len(countries) < 20:
            raise ValueError("codelist unexpectedly short")
        countries[EURO_AREA] = "Euro area"
        return countries, True
    except Exception:
        return dict(FALLBACK_COUNTRIES), False


def monthly_usd_constant(start: str = START_PERIOD) -> pd.Series:
    """Exchange rate of the US dollar against itself: 1.0 in every month."""
    end = pd.Timestamp.today().to_period("M").to_timestamp()
    idx = pd.date_range(start=pd.Timestamp(start), end=end, freq="MS")
    return pd.Series(1.0, index=idx)


def get_exchange_rate(iso: str, name: str) -> pd.Series:
    """Local-currency units per USD, period average (key {ISO3}.XDC_USD.PA_RT.M)."""
    if iso == USA:
        return monthly_usd_constant()
    series, err = fetch_imf_series(ER_FLOW, f"{iso}.XDC_USD.PA_RT.M")
    if err:
        st.warning(f"Exchange rate for **{name} ({iso})** unavailable "
                   f"[{ER_FLOW}: {iso}.XDC_USD.PA_RT.M]: {err}.")
    return series


def get_cpi(iso: str, name: str) -> pd.Series:
    """Monthly all-items CPI index (key {ISO3}.CPI._T.IX.M).

    The IMF stores the Euro area's price index as HICP rather than CPI, so if
    the CPI key returns nothing we try {ISO3}.HICP._T.IX.M as a second option.
    """
    key = f"{iso}.CPI._T.IX.M"
    series, err = fetch_imf_series(CPI_FLOW, key)
    if series.empty:
        alt_key = f"{iso}.HICP._T.IX.M"
        alt, alt_err = fetch_imf_series(CPI_FLOW, alt_key)
        if not alt.empty:
            return alt
    if err:
        st.warning(f"CPI for **{name} ({iso})** unavailable [{CPI_FLOW}: {key}]: {err}.")
    return series


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def build_dataset(e1: pd.Series, e2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    """Merge the four raw series and compute S(t) and RER(t).

    Inner join: a month is "valid" only if all four values exist. No filling
    or interpolation is done.

    E1, E2 are local currency per USD. Converting via the dollar:
        S = (C2 currency / USD) / (C1 currency / USD) = E2 / E1
    so S is units of Country 2 currency per 1 unit of Country 1 currency.
    Example: C1 = USA (E1 = 1), C2 = Japan (E2 = yen per USD) -> S = yen per dollar.

    RER = S * P1 / P2: the number of Country 2 baskets one Country 1 basket buys.
    """
    df = pd.concat({"E1": e1, "E2": e2, "P1": p1, "P2": p2}, axis=1, join="inner")
    df = df.dropna()
    df = df[(df > 0).all(axis=1)]  # rates and price indices must be positive
    df["S"] = df["E2"] / df["E1"]
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    df.index.name = "Date"
    return df.sort_index()


def apply_sample(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Restrict to the chosen sample and compute RER* and the Real Index.

    RER*       = mean of RER over valid months in the sample
    Real Index = 100 * RER / RER*
    """
    sample = df.loc[(df.index >= start) & (df.index <= end)].copy()
    rer_star = sample["RER"].mean()
    sample["RER_star"] = rer_star
    sample["Real_Index"] = 100.0 * sample["RER"] / rer_star
    return sample


def coverage_table(raw: dict[str, pd.Series]) -> pd.DataFrame:
    """First and last available month and observation count for each raw series."""
    rows = []
    for label, s in raw.items():
        rows.append({
            "Series": label,
            "First month": s.index.min().strftime("%Y-%m") if not s.empty else "—",
            "Last month": s.index.max().strftime("%Y-%m") if not s.empty else "—",
            "Observations": int(s.size),
        })
    return pd.DataFrame(rows)


def gap_months(raw: dict[str, pd.Series], valid_index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Months inside the common window (latest first month .. earliest last month)
    that were dropped because at least one series was missing."""
    if any(s.empty for s in raw.values()):
        return []
    lo = max(s.index.min() for s in raw.values())
    hi = min(s.index.max() for s in raw.values())
    if lo > hi:
        return []
    window = pd.date_range(lo, hi, freq="MS")
    return list(window.difference(valid_index))


def to_download_frame(sample: pd.DataFrame) -> pd.DataFrame:
    """Format the sample for display and CSV export."""
    out = pd.DataFrame({
        "Date": sample.index.strftime("%Y-%m"),
        "E1_LCU_per_USD": sample["E1"].to_numpy(),
        "E2_LCU_per_USD": sample["E2"].to_numpy(),
        "S_C2_per_C1": sample["S"].to_numpy(),
        "P1_CPI": sample["P1"].to_numpy(),
        "P2_CPI": sample["P2"].to_numpy(),
        "RER": sample["RER"].to_numpy(),
        "RER_star": sample["RER_star"].to_numpy(),
        "Real_Index": sample["Real_Index"].to_numpy(),
    })
    return out


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def for_plotting(sample: pd.DataFrame) -> pd.DataFrame:
    """Reindex to every month in the sample so missing months appear as breaks
    in the line instead of being silently bridged."""
    full = pd.date_range(sample.index.min(), sample.index.max(), freq="MS")
    return sample.reindex(full)


def currency_label(iso: str) -> str:
    return CURRENCIES.get(iso, f"{iso} currency")


def plot_real(sample: pd.DataFrame, title: str) -> go.Figure:
    """Line chart of the Real Index with a dashed reference line at 100."""
    data = for_plotting(sample)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=data.index, y=data["Real_Index"], mode="lines", name="Real Index",
        line=dict(color=COLOR_REAL, width=2), connectgaps=False,
        hovertemplate="%{x|%Y-%m}<br>Real Index: %{y:.2f}<extra></extra>",
    ))
    fig.add_hline(y=100, line_dash="dash", line_color=COLOR_REF,
                  annotation_text="RER* (sample average)", annotation_position="top left")
    fig.update_layout(title=title, xaxis_title="Month",
                      yaxis_title="Real Index (RER* = 100)", hovermode="x unified")
    return fig


def plot_nominal(sample: pd.DataFrame, title: str, y_label: str) -> go.Figure:
    """Line chart of nominal S in actual currency units (never indexed)."""
    data = for_plotting(sample)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=data.index, y=data["S"], mode="lines", name="Nominal S",
        line=dict(color=COLOR_NOMINAL, width=2), connectgaps=False,
        hovertemplate="%{x|%Y-%m}<br>S: %{y:,.4f}<extra></extra>",
    ))
    fig.update_layout(title=title, xaxis_title="Month", yaxis_title=y_label,
                      hovermode="x unified")
    return fig


def plot_both(sample: pd.DataFrame, title: str, y_label: str) -> go.Figure:
    """Real Index on the left axis, nominal S on the right axis."""
    data = for_plotting(sample)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(
        x=data.index, y=data["Real_Index"], mode="lines", name="Real Index (left)",
        line=dict(color=COLOR_REAL, width=2), connectgaps=False,
        hovertemplate="Real Index: %{y:.2f}<extra></extra>",
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=data.index, y=data["S"], mode="lines", name="Nominal S (right)",
        line=dict(color=COLOR_NOMINAL, width=2), connectgaps=False,
        hovertemplate="S: %{y:,.4f}<extra></extra>",
    ), secondary_y=True)
    fig.add_hline(y=100, line_dash="dash", line_color=COLOR_REF,
                  annotation_text="RER* (sample average)", annotation_position="top left",
                  secondary_y=False)
    fig.update_layout(title=title, xaxis_title="Month", hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    fig.update_yaxes(title_text="Real Index (RER* = 100)", color=COLOR_REAL, secondary_y=False)
    fig.update_yaxes(title_text=y_label, color=COLOR_NOMINAL, secondary_y=True)
    return fig


# ---------------------------------------------------------------------------
# User interface
# ---------------------------------------------------------------------------

def choose_countries(countries: dict[str, str]) -> tuple[str, str]:
    """Two selectboxes; returns (iso1, iso2)."""
    options = sorted(countries, key=lambda c: countries[c].lower())
    fmt = lambda c: f"{countries[c]} ({c})"
    default1 = options.index(USA) if USA in options else 0
    default2 = options.index(EURO_AREA) if EURO_AREA in options else min(1, len(options) - 1)
    col1, col2 = st.columns(2)
    iso1 = col1.selectbox("Country 1", options, index=default1, format_func=fmt)
    iso2 = col2.selectbox("Country 2", options, index=default2, format_func=fmt)
    return iso1, iso2


def show_methodology(name1: str, name2: str, cur1: str, cur2: str) -> None:
    with st.expander("Methodology"):
        st.markdown(f"""
**Inputs (IMF, monthly)**
- E1(t), E2(t): local currency per US dollar, period average (`{{ISO3}}.XDC_USD.PA_RT.M`).
  For the United States E = 1 by definition.
- P1(t), P2(t): all-items CPI index (`{{ISO3}}.CPI._T.IX.M`; the Euro area uses HICP).

**Nominal exchange rate** — units of {cur2} per 1 {cur1}:

  S(t) = E2(t) / E1(t)

Both rates are quoted per USD, so dividing cancels the dollar.

**Real exchange rate:** RER(t) = S(t) × P1(t) / P2(t).
It measures how many {name2} consumption baskets one {name1} basket buys.

**Sample average and index:** RER\\* = mean of RER over the valid months in the
chosen sample; Real Index(t) = 100 × RER(t) / RER\\*.

**Interpretation.** Because S is {cur2} per {cur1}, a *higher* S or Real Index means
{name1}'s currency is *stronger*. A Real Index **above 100** means {name1}'s currency
is **real-appreciated** (overvalued) relative to its sample average: {name1} goods are
relatively expensive compared with {name2} goods. **Below 100** means it is
real-depreciated (undervalued). Relative PPP suggests the index tends to drift back
toward 100 over long horizons — that is the basis for treating RER\\* as a forecast anchor.

Only months where all four series exist are used. Nothing is forward-filled or
interpolated.
""")


def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Forecast App", page_icon="💱", layout="wide")
    st.title("Real Exchange Rate Forecast App")
    st.caption("Bilateral nominal and real exchange rates from IMF monthly CPI and "
               "exchange-rate data (SDMX 3.0 API).")

    # 1. Country selection ---------------------------------------------------
    with st.spinner("Loading IMF country list…"):
        countries, from_api = fetch_country_list()
    if not from_api:
        st.info("Could not load the IMF country list; using a built-in list of major economies.")

    iso1, iso2 = choose_countries(countries)
    if iso1 == iso2:
        st.warning("Please choose two different countries.")
        st.stop()

    name1, name2 = countries[iso1], countries[iso2]
    cur1, cur2 = currency_label(iso1), currency_label(iso2)

    if EURO_AREA in (iso1, iso2):
        st.info("Note: Euro area (G163) data begin around 1999 (the euro's launch). Any "
                "earlier history in IMF sources is shorter or synthetic, so the sample is "
                "usually limited to 1999 onward.")

    # 2. Data retrieval ------------------------------------------------------
    with st.spinner("Downloading data from the IMF…"):
        e1 = get_exchange_rate(iso1, name1)
        e2 = get_exchange_rate(iso2, name2)
        p1 = get_cpi(iso1, name1)
        p2 = get_cpi(iso2, name2)

    raw = {
        f"E1 – {name1} LCU per USD": e1,
        f"E2 – {name2} LCU per USD": e2,
        f"P1 – {name1} CPI": p1,
        f"P2 – {name2} CPI": p2,
    }

    # 3. Calculations --------------------------------------------------------
    try:
        data = build_dataset(e1, e2, p1, p2)
    except Exception as exc:
        st.error(f"Could not combine the series: {exc}")
        st.stop()

    if len(data) < 2:
        st.error(f"Fewer than 2 months have all four series for {name1} and {name2}, "
                 "so no chart can be drawn. Try a different country pair.")
        with st.expander("Data coverage", expanded=True):
            st.dataframe(coverage_table(raw), hide_index=True, width="stretch")
        st.stop()

    # Sample period selector (bounded by the first and last valid months)
    months = list(data.index)
    start, end = st.select_slider(
        "Sample period",
        options=months,
        value=(months[0], months[-1]),
        format_func=lambda d: d.strftime("%Y-%m"),
    )
    sample = apply_sample(data, start, end)
    if len(sample) < 2:
        st.warning("The selected sample contains fewer than 2 valid months. Widen the range.")
        st.stop()

    rer_star = float(sample["RER_star"].iloc[0])
    latest = sample.iloc[-1]
    deviation = latest["Real_Index"] - 100.0
    status = "overvalued" if deviation > 0 else "undervalued" if deviation < 0 else "at average"

    # 4. Display -------------------------------------------------------------
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("RER*", f"{rer_star:,.4g}")
    m2.metric("Valid months", f"{len(sample):,}")
    m3.metric("Sample", f"{sample.index[0]:%Y-%m} → {sample.index[-1]:%Y-%m}")
    m4.metric(f"Real Index ({sample.index[-1]:%Y-%m})", f"{latest['Real_Index']:.1f}")
    m5.metric("Deviation from 100", f"{deviation:+.1f}%", status,
              delta_color="off")
    st.caption(f"Direction convention: S is {cur2} per {cur1}, so an index above 100 means "
               f"the {name1} currency is real-appreciated (overvalued) relative to its sample "
               f"average against {name2}; below 100 means undervalued.")

    view = st.radio("Chart", ["Real", "Nominal", "Both"], horizontal=True)
    y_label = f"Units of {cur2} per 1 {cur1}"
    pair = f"{name1} vs {name2}"
    if view == "Real":
        fig = plot_real(sample, f"Real Index — {pair}")
    elif view == "Nominal":
        fig = plot_nominal(sample, f"Nominal exchange rate S — {cur2} per {cur1}", y_label)
    else:
        fig = plot_both(sample, f"Real Index and nominal S — {pair}", y_label)
    st.plotly_chart(fig, width="stretch")

    with st.expander("Data coverage"):
        st.dataframe(coverage_table(raw), hide_index=True, width="stretch")
        gaps = gap_months(raw, data.index)
        if gaps:
            shown = ", ".join(d.strftime("%Y-%m") for d in gaps[:60])
            more = f" … and {len(gaps) - 60} more" if len(gaps) > 60 else ""
            st.write(f"**{len(gaps)} month(s) dropped** inside the common window because "
                     f"at least one series was missing: {shown}{more}")
        else:
            st.write("No months were dropped inside the common window; the valid range is "
                     "set by the shortest series.")
        st.write(f"Valid months overall: {len(data):,} "
                 f"({data.index[0]:%Y-%m} → {data.index[-1]:%Y-%m}).")

    show_methodology(name1, name2, cur1, cur2)

    # 5. CSV download --------------------------------------------------------
    table = to_download_frame(sample)
    filename = f"rer_{iso1}_{iso2}_{sample.index[0]:%Y-%m}_{sample.index[-1]:%Y-%m}.csv"
    st.download_button("Download sample as CSV", table.to_csv(index=False).encode("utf-8"),
                       file_name=filename, mime="text/csv")
    with st.expander("Data table"):
        st.dataframe(table, hide_index=True, width="stretch")

    st.caption("Source: International Monetary Fund, SDMX 3.0 API (dataflows IMF.STA:CPI "
               "and IMF.STA:ER).")


if __name__ == "__main__":
    main()
