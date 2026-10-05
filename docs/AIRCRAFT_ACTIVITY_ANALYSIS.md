# Aircraft activity analysis

## Purpose and interpretation

This proof of concept asks whether air-quality changes occur near the time that aircraft pass close to the monitoring instrument. It measures **association only**. A matching timestamp does not prove that an aircraft caused a pollution spike. Wind, road traffic, other emitters, atmospheric mixing, instrument response, and gaps in aircraft coverage are potential alternative explanations.

The default site is the Sound Transit GTFS parent stop for Angle Lake Station (`S01`): **47.422703, -122.297714**. Sound Transit lists the station at 19955 28th Ave S, SeaTac. This is a provisional proxy—not a surveyed instrument coordinate. Change the latitude and longitude in the Aircraft page or in `frontend/public/aircraft/config.json` when the real coordinates are available. Source: [Sound Transit open transit data downloads](https://www.soundtransit.org/help-contacts/business-information/open-transit-data-otd/otd-downloads) and [Angle Lake Station](https://www.soundtransit.org/ride-with-us/stops-stations/angle-lake-station).

## Aircraft-data source decision (verified October 4, 2026)

The feature deliberately does not call a live-flight endpoint and pretend that it supplies historical data.

| Source | Historical access | Cost/access constraint | Decision |
|---|---|---|---|
| OpenSky REST API | Authenticated state-vector requests can ask for a time up to one hour in the past; anonymous requests are latest-state only. Track history is experimental and limited to 30 days. | OAuth client credentials; credit limits depend on account/feeder/license status. | Not suitable for arbitrary historical days. |
| OpenSky Trino research archive | `state_vectors_data4` covers historical state vectors from 2013. | Free for approved university/government research after an application; private/commercial use requires a license. Queries must use hour partitions and respect concurrency/runtime limits. | **Chosen research export path.** Generate bounded SQL, export CSV, and import it. |
| Flightradar24 API | Historic flight positions are available from May 11, 2016, with ten-second granularity after November 18, 2019. | Paid API subscription and credits; full historic positions currently consume 8 credits per returned flight and light responses 6. | Technically suitable, but not purchased or embedded. |
| ADS-B Exchange | Historical/commercial data access is licensed. | Commercial projects require a commercial license. Some limited reduced-fee research/educational cases require contributing a feeder. | Not embedded; contact the provider if its coverage is preferred. |

Current provider documentation:

- [OpenSky REST API](https://openskynetwork.github.io/opensky-api/rest.html)
- [OpenSky Trino historical database](https://openskynetwork.github.io/opensky-api/trino.html)
- [Flightradar24 historical-position availability](https://fr24api.flightradar24.com/docs/endpoints/flight-positions-resolution)
- [Flightradar24 subscriptions and credits](https://fr24api.flightradar24.com/subscriptions-and-credits)
- [Flightradar24 credit overview](https://fr24api.flightradar24.com/docs/credit-overview)
- [ADS-B Exchange commercial/research API guidance](https://support.adsbexchange.com/hc/en-us/articles/37363886073613-I-m-building-a-project-for-my-company-but-we-aren-t-making-money-off-of-it-can-I-get-a-free-API-key)

Terms, limits, prices, and coverage can change. Re-check the provider documentation before beginning a larger study.

## Run the working proof of concept

From the repository root:

```powershell
cd C:\des_moines\des-moines-data-monitor\frontend
npm install
npm run dev
```

Open the local URL printed by Vite, then select **Aircraft**.

1. Choose a Pacific-local start/end time. A single run is limited to one day.
2. Select the sensor instrument. Run once to load the API's default measurement, or choose another available measurement and run again.
3. Import an aircraft CSV/JSON export, or click **Load synthetic sample** to test the complete interface. The sample uses `SAMPLE-*` identifiers, includes fabricated sensor values, and is always labeled synthetic.
4. Choose 1, 3, or 5 km and adjust the ±10-minute default window.
5. Click **Run analysis**. The interface requests one-minute Silver-layer sensor means, converts the selected Pacific window to UTC, and calculates the events.

The aircraft page is also available directly at `/aircraft` after deployment.

## Get one day from OpenSky's research archive

Do not put OpenSky credentials in this repository. After OpenSky approves archive access, create a bounded, partition-aware SQL query:

```powershell
python scripts\aircraft\build_opensky_query.py --date 2026-09-17 --radius-km 10 --output aircraft-query.sql
```

Or select a partial local day:

```powershell
python scripts\aircraft\build_opensky_query.py --start-local 2026-09-17T12:00 --end-local 2026-09-17T18:00 --radius-km 10 --output aircraft-query.sql
```

Run the SQL using the OpenSky Trino client described in their documentation and save the result as CSV. The query selects timestamp, ICAO24 identifier, callsign, latitude, longitude, geometric altitude, and barometric altitude. Aircraft type is left empty because the state-vector table does not supply it; it may be added from a properly licensed metadata source.

Preserve the raw export and create an importable JSON bundle:

```powershell
python scripts\aircraft\import_aircraft.py opensky-export.csv `
  --provider opensky-trino `
  --coverage unknown `
  --window-start-utc 2026-09-17T19:00:00Z `
  --window-end-utc 2026-09-18T01:00:00Z
```

The importer copies the untouched source to `aircraft-cache/<bundle>/raw/`, records its SHA-256 digest and provenance, and writes `aircraft-import.json`. The cache is intentionally git-ignored. Import that JSON on the Aircraft page. The browser also caches the last import locally for convenience, but the command-line bundle is the durable raw record.

## Accepted import schema

JSON may be an array or an object with `metadata` and `observations`. CSV uses the same columns. Common OpenSky aliases are recognized.

Required per observation:

- timestamp: UTC Unix seconds, an ISO timestamp ending in `Z`, or an ISO timestamp with an explicit offset;
- latitude and longitude;
- aircraft identifier: `aircraft_id`, `icao24`, `hex`, callsign, or equivalent (unknown is retained when absent).

Optional:

- `callsign`;
- `geoaltitude` or geometric altitude in metres;
- `baroaltitude` or barometric altitude in metres;
- `aircraft_type` / `typecode`.

Naive aircraft timestamps are rejected because guessing their timezone can create false matches. Altitude is stored with its reported reference. It is **not** treated as height above the instrument or adjusted for terrain.

Metadata should include `provider`, `window_start_utc`, `window_end_utc`, and `coverage_status` (`complete`, `partial`, or `unknown`). Only mark coverage complete when the provider/export process genuinely establishes this. An empty result with partial or unknown coverage is not evidence that no aircraft were present.

## Time, distance, and event definitions

- Sensor CSV clocks currently have no UTC offset. The backend assumes `America/Los_Angeles` local wall time and converts to UTC using daylight-saving rules. This is explicitly marked **unverified** in the UI and must be confirmed per instrument.
- User-entered times are interpreted in `America/Los_Angeles`. Nonexistent spring-forward times are rejected. All matching uses UTC; local labels include PST/PDT.
- Horizontal distance is the Haversine great-circle distance from the configured instrument latitude/longitude to each aircraft latitude/longitude. It does not include altitude.
- A flyby is a set of observations for one aircraft inside the selected radius. A gap longer than 30 minutes starts another pass.
- Closest approach is the observation in that pass with the smallest horizontal distance.
- With a configurable window `W` (default 10 minutes), **baseline** is the median of one-minute sensor means from `−W` to `−W/2` before closest approach.
- **Peak** is the maximum one-minute sensor mean from `−W/2` through `+W`.
- **Peak change** is `peak − baseline`. Missing values remain missing when either window has no sensor data.
- The 1, 3, and 5 km radii and ±10-minute window are exploratory settings, not validated scientific cutoffs.

## Wind and coverage limitations

The current sensor API does not provide aligned wind speed or direction, so the page displays **Wind unavailable** and performs no wind-based attribution. Add wind only when its timestamp, units, direction convention, height, and site are known.

ADS-B coverage can be incomplete because of receiver geometry, filtering, aircraft equipment, provider processing, and query/export failures. Preserve provider metadata and the requested interval. Report “no qualifying positions in the available data,” not “no aircraft,” unless coverage is demonstrably complete.

## Tests and CI

Run the same checks used by GitHub Actions:

```powershell
python -m unittest discover -s tests -v
python scripts\quality\audit_public_exposure.py
cd frontend
npm test
npm run lint
npm run build
```

`.github/workflows/ci.yml` runs these checks for every pull request and every push to `main`. In GitHub repository settings, protect `main`, require a pull request, require the two CI checks, require the branch to be up to date, and disable force pushes. The workflow only validates code; it does not deploy production.
