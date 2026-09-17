# Des Moines Air Quality API

This guide is safe to share with external users. It documents only the public,
read-only API surface for the Des Moines air quality dashboard.

## Base URL

```text
https://yvhb48sthk.execute-api.us-west-2.amazonaws.com
```

## Access Model

The researcher API is read-only. Each verified user receives a personal API key
for programmatic access.

To request a key:

1. Submit an access request from the API page.
2. Verify the email link sent to your work email.
3. Receive the automatically generated key at the same verified email address.
4. Store the issued key in an environment variable such as `AQ_API_KEY`.

Pass the key only in the `x-api-key` request header. Never put it in a URL.

The dashboard and API remain read-only. API users cannot modify records.

Each key is limited to 30 requests per minute and 5,000 requests per day. CSV
exports have a separate limit of 20 per day. Exceeding a limit returns `429`.

### Request API access

```text
POST /air-quality/v1/access-requests
```

The API page provides this form. For integrations, submit `name`, `email`,
`organization`, and `use_case` as JSON. A successful request returns `202` and
sends a verification email.

```python
import requests

API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"

response = requests.post(
    f"{API_BASE_URL}/air-quality/v1/access-requests",
    json={
        "name": "Example Researcher",
        "email": "researcher@example.org",
        "organization": "Example University",
        "use_case": "Hourly analysis for an environmental health study.",
    },
    timeout=30,
)
response.raise_for_status()
print(response.json()["message"])
```

## Endpoints

### Dashboard summary

```text
GET /air-quality/v1/keyed/summary
```

Returns the dashboard summary payload.

Main fields:

| Field | Description |
|-------|-------------|
| `refreshTime` | Latest upload timestamp seen by the API. |
| `systemStatus` | `ONLINE` when recent data exists, otherwise `DEGRADED`. |
| `kpis` | Public dashboard labels such as latest instrument and site name. |
| `instruments` | Per-instrument raw upload counts and cleaned row counts. |

Example:

```python
import os
import requests

API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"
HEADERS = {"x-api-key": os.environ["AQ_API_KEY"]}

response = requests.get(
    f"{API_BASE_URL}/air-quality/v1/keyed/summary",
    headers=HEADERS,
    timeout=30,
)
response.raise_for_status()

payload = response.json()
print(payload["systemStatus"])
print(payload["kpis"]["siteName"])
```

### Hourly time series

```text
GET /air-quality/v1/keyed/timeseries
```

Returns hourly mean values for one measurement from the cleaned records.

Parameters:

| Name | Required | Description |
|------|----------|-------------|
| `instrument` | Yes | One of `BC-MA200`, `CO2-LICOR`, `NEPH-PM25`, `NO2-CAPS`, `SMPS`. |
| `measurement` | No | Measurement column name. If omitted, the API chooses a default. |
| `start` | No | ISO timestamp. Records before this time are excluded. |
| `end` | No | ISO timestamp. Records after this time are excluded. |

Example:

```python
import os
import requests
import pandas as pd

API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"
HEADERS = {"x-api-key": os.environ["AQ_API_KEY"]}

response = requests.get(
    f"{API_BASE_URL}/air-quality/v1/keyed/timeseries",
    params={
        "instrument": "SMPS",
        "measurement": "Total Concentration (#/cm³)",
    },
    headers=HEADERS,
    timeout=30,
)
response.raise_for_status()

data = response.json()
points = pd.DataFrame(data["series"])

print(data["measurement"])
print(points.head())
```

### Observations

```text
GET /air-quality/v1/keyed/observations
```

Returns paginated row-level cleaned observations.

Parameters:

| Name | Required | Description |
|------|----------|-------------|
| `instrument` | Yes | Instrument ID. |
| `start` | No | ISO timestamp filter. |
| `end` | No | ISO timestamp filter. |
| `limit` | No | Records per page. The API caps the value. |
| `cursor` | No | Pagination cursor returned by the previous response. |
| `order` | No | `asc` or `desc`. |

Example:

```python
import os
import requests

API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"
HEADERS = {"x-api-key": os.environ["AQ_API_KEY"]}

response = requests.get(
    f"{API_BASE_URL}/air-quality/v1/keyed/observations",
    params={"instrument": "NO2-CAPS", "limit": 100, "order": "desc"},
    headers=HEADERS,
    timeout=30,
)
response.raise_for_status()

data = response.json()
rows = data["rows"]
next_cursor = data["next_cursor"]

print(len(rows))
print(next_cursor)
```

To fetch the next page, pass the returned cursor:

```python
response = requests.get(
    f"{API_BASE_URL}/air-quality/v1/keyed/observations",
    params={
        "instrument": "NO2-CAPS",
        "limit": 100,
        "cursor": next_cursor,
    },
    headers=HEADERS,
    timeout=30,
)
```

### Observation export

```text
GET /air-quality/v1/keyed/observations/export
```

Returns metadata and a short-lived export URL for the full cleaned observation
CSV.

Parameters:

| Name | Required | Description |
|------|----------|-------------|
| `instrument` | Yes | Instrument ID. |

Example:

```python
import os
import requests
import pandas as pd

API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"
HEADERS = {"x-api-key": os.environ["AQ_API_KEY"]}

response = requests.get(
    f"{API_BASE_URL}/air-quality/v1/keyed/observations/export",
    params={"instrument": "SMPS"},
    headers=HEADERS,
    timeout=30,
)
response.raise_for_status()

export_url = response.json()["url"]
df = pd.read_csv(export_url)

print(df.shape)
```

Export URLs expire after a few minutes. Scripts should request a new URL each
time they run.

## Responsible Use

- Keep requests reasonable for research and dashboard exploration.
- Cache results in notebooks or scripts when running repeated analysis.
- Do not put private credentials in URLs, screenshots, notebooks, or public
  repositories.
- Contact the project team if you need a higher-volume access pattern.

## Key Safety

- Keep your key in an environment variable or a secrets manager.
- Do not put API keys in URLs, query parameters, screenshots, notebooks, or
  public repositories.
- Contact the project team if a key may have been exposed so it can be revoked.
