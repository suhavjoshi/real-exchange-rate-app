# Real Exchange Rate Forecast App

A Streamlit web app for an international finance class. Pick any two countries and the
app downloads monthly consumer price indices and US-dollar exchange rates from the IMF.
It then builds the bilateral **nominal** and **real** exchange rates and shows the real
rate as an index around its sample average (RER\*). Under relative purchasing power
parity, the real exchange rate tends to revert to that average, which is why RER\* is
used here as a simple long-run forecast anchor.

## Features

- Choose Country 1 and Country 2 from the IMF country list (plus the Euro area, `G163`).
- Pick the sample with a month slider. RER\* and the Real Index recalculate instantly.
- Interactive Plotly charts: **Real**, **Nominal**, or **Both** (two y-axes).
- Summary metrics: RER\*, valid months, sample dates, latest Real Index and its
  deviation from 100 (overvalued / undervalued).
- Data-coverage and methodology panels.
- CSV download of every valid month in the sample.

## Formulas

| Symbol | Meaning |
|---|---|
| E1(t), E2(t) | IMF exchange rate: units of local currency per 1 USD (period average) |
| P1(t), P2(t) | Monthly CPI index |

**Nominal exchange rate:** units of Country 2 currency per 1 unit of Country 1 currency:

```
S(t) = E2(t) / E1(t)
```

*Why this works:* E2 is C2-currency/USD and E1 is C1-currency/USD, so dividing gives
(C2/USD) ÷ (C1/USD) = C2 per C1, and the dollar cancels out. Example: with C1 = USA
(E1 = 1) and C2 = Japan (E2 = yen per dollar), S is **yen per dollar**. With C1 = Japan
and C2 = USA, S is dollars per yen.

**Real exchange rate:**

```
RER(t) = S(t) × P1(t) / P2(t)
```

That is the number of Country 2 consumption baskets that one Country 1 basket buys.

**Sample average and index:**

```
RER*          = mean of RER(t) over valid months in the selected sample
Real Index(t) = 100 × RER(t) / RER*
```

**Interpretation:** Because S is quoted as C2 per C1, a **Real Index above 100** means
Country 1's currency is **real-appreciated (overvalued)** relative to its sample
average. Country 1 goods are relatively expensive. **Below 100** means it is
real-depreciated (undervalued).

The nominal rate S is always plotted in real currency units and is never indexed.

A month is **valid** only if all four series (E1, E2, P1, P2) are present (inner join).
Missing data are never forward-filled or interpolated.

## Data sources

International Monetary Fund, SDMX 3.0 REST API:
`https://api.imf.org/external/sdmx/3.0`

| Series | Dataflow | Key | Notes |
|---|---|---|---|
| CPI | `IMF.STA:CPI` | `{ISO3}.CPI._T.IX.M` | All items, index, monthly. The Euro area uses `G163.HICP._T.IX.M`; the app tries this automatically. |
| Exchange rate | `IMF.STA:ER` | `{ISO3}.XDC_USD.PA_RT.M` | Domestic currency per USD, period average, monthly. |
| Country list | codelist `IMF:CL_COUNTRY` | — | Falls back to a built-in list of ~50 economies if unavailable. |

Example data request (SDMX-CSV):

```
GET https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/JPN.CPI._T.IX.M?c[TIME_PERIOD]=ge:1976-01
Accept: application/vnd.sdmx.data+csv;version=2.0.0
```

Data are requested from January 1976 onward and cached for 24 hours. For the United
States, the exchange rate against USD is set to 1.0 by definition rather than downloaded.

## Run locally

Requires Python 3.12.

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
streamlit run app.py
```

The app opens at <http://localhost:8501>.

## Deploy on Streamlit Community Cloud

1. Push `app.py`, `requirements.txt` and `README.md` to a public (or private) GitHub
   repository.
2. Sign in at <https://share.streamlit.io> with your GitHub account.
3. Click **Create app** → **Deploy a public app from GitHub**.
4. Choose the repository, branch (e.g. `main`), and main file path `app.py`.
5. Under **Advanced settings**, select Python **3.12**.
6. Click **Deploy**. Streamlit installs `requirements.txt` and gives you a public URL.
   Pushing new commits redeploys the app automatically.

No API keys or secrets are needed: the IMF API is public.

## Known data limitations

- **Gaps:** Some countries have missing months in CPI or exchange-rate series. These
  months are dropped, not filled, and are listed in the *Data coverage* panel. Charts
  show breaks at gaps.
- **Euro area (G163):** Exchange-rate data start in January 1999 and HICP in December 1999
  in the IMF series. Any pre-1999 euro history in other sources is synthetic, so Euro-area
  pairs usually cover 1999 onward only.
- **Short CPI series:** Many emerging and developing economies report monthly CPI only
  from the 1990s or later. Some countries in the list have no monthly data at all. The
  app warns and shows no chart when fewer than two months overlap.
- **Hyperinflation and redenomination:** Countries such as Argentina, Brazil, Türkiye,
  Venezuela and Zimbabwe have had episodes of extreme inflation and currency
  redenominations. Nominal S can span many orders of magnitude. The real exchange rate
  is more stable, but its sample average can be dominated by crisis periods. Choose
  sample periods carefully.
- **Base years:** CPI base years differ across countries. This affects the *level* of
  RER but not the Real Index, because the index divides by the sample mean.
- **Revisions:** IMF data are revised over time. Cached results refresh every 24 hours.
- **Pegs:** For currencies pegged to the dollar or euro, nominal S is nearly flat and
  real movements come almost entirely from inflation differentials.
