import { useCallback, useState, useEffect } from 'react';
import { Activity, AlertTriangle, Check, Clock, Copy, Database, DollarSign, Download, KeyRound, Lock, MapPin, Menu, Moon, RefreshCw, Search, Send, Sun, Wind, X } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

const DEFAULT_API_BASE_URL = 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com';
const API_ENTRY_URL = import.meta.env.VITE_API_URL || `${DEFAULT_API_BASE_URL}/air-quality/v1/summary`;
const getApiBaseUrl = (value) => {
  const trimmed = String(value || DEFAULT_API_BASE_URL).replace(/\/+$/, '');
  const marker = '/air-quality/';
  if (trimmed.includes(marker)) return trimmed.slice(0, trimmed.indexOf(marker));
  try {
    return new URL(trimmed).origin;
  } catch {
    return DEFAULT_API_BASE_URL;
  }
};
const API_BASE_URL = getApiBaseUrl(API_ENTRY_URL);
const API_PATHS = {
  summary: '/air-quality/v1/summary',
  timeseries: '/air-quality/v1/timeseries',
  observations: '/air-quality/v1/observations',
  observationsExport: '/air-quality/v1/observations/export',
  accessRequests: '/air-quality/v1/access-requests',
};
const apiUrl = (path) => import.meta.env.DEV ? path : `${API_BASE_URL}${path}`;
const documentedApiUrl = (path) => `${API_BASE_URL}${path}`;
const API_TIMEOUT_MS = 15000;
const fetchApi = async (pathWithQuery, options = {}) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    return await fetch(apiUrl(pathWithQuery), { ...options, signal: options.signal || controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
};
const INSTRUMENT_IDS = ['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'];
const toIso = (value) => value ? new Date(value).toISOString() : '';
const formatScienceLabel = (value) => String(value || '').replaceAll('cm�', 'cm³');
const getInitialTheme = () => {
  if (typeof window === 'undefined') return 'light';
  const saved = window.localStorage.getItem('aq-dashboard-theme');
  if (saved === 'light' || saved === 'dark') return saved;
  return 'light';
};

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [activeView, setActiveView] = useState('overview');
  const [theme, setTheme] = useState(getInitialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem('aq-dashboard-theme', theme);
  }, [theme]);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const response = await fetchApi(API_PATHS.summary);
        if (!response.ok) {
          throw new Error(`API returned ${response.status}`);
        }
        const result = await response.json();
        setData(result);
        setError(null);
      } catch (err) {
        console.error('Fetch error:', err);
        setError(err.message || 'API request failed');
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    };
    fetchData();
    const interval = setInterval(fetchData, 60000);
    return () => clearInterval(interval);
  }, []);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const response = await fetchApi(API_PATHS.summary);
      if (!response.ok) {
        throw new Error(`API returned ${response.status}`);
      }
      const result = await response.json();
      setData(result);
      setError(null);
    } catch (err) {
      console.error('Fetch error:', err);
      setError(err.message || 'API request failed');
    } finally {
      setRefreshing(false);
    }
  };

  const { kpis = {}, instruments = [], refreshTime } = data || {};

  const formatBytes = (bytes) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  const formatSeattleTime = (isoString) => {
    if (!isoString) return "NO DATA";
    return new Date(isoString).toLocaleString('en-US', {
      timeZone: 'America/Los_Angeles',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true
    });
  };

  return (
    <div className={`app-shell ${theme === 'light' ? 'theme-light' : 'theme-dark'} ${activeView === 'api' ? 'api-shell' : ''}`}>
      <div className={activeView === 'api' ? 'api-portal-root' : 'site-frame'}>
        {activeView !== 'api' && <header className="site-header">
          <button className="site-brand" type="button" onClick={() => setActiveView('overview')} aria-label="Open overview">
            <span className="brand-mark"><Wind size={22} /></span>
            <span><strong>Des Moines Air</strong><small>Environmental monitor</small></span>
          </button>

          <nav className="site-nav" aria-label="Main navigation">
            {[
              { id: 'overview', label: 'Conditions' },
              { id: 'review', label: 'Observations' },
              { id: 'api', label: 'Developers' },
            ].map(tab => (
              <button key={tab.id} onClick={() => setActiveView(tab.id)} className={activeView === tab.id ? 'site-nav-active' : ''}>
                {tab.label}
              </button>
            ))}
          </nav>

          <div className="site-actions">
            <button className="icon-action" type="button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} aria-label={`Use ${theme === 'light' ? 'dark' : 'light'} theme`} title={`Use ${theme === 'light' ? 'dark' : 'light'} theme`}>
              {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
            </button>
            <button className="refresh-action" type="button" onClick={handleRefresh} disabled={refreshing}>
              <RefreshCw size={17} className={refreshing ? 'animate-spin' : ''} />
              <span>{refreshing ? 'Refreshing' : 'Refresh'}</span>
            </button>
          </div>
        </header>}

        {loading && activeView !== 'api' && (
          <div className="dashboard-api-notice dashboard-loading-notice" role="status">
            Loading the latest research data. You can still use the navigation and filters.
          </div>
        )}

        {!loading && error && activeView !== 'api' && (
          <div className="dashboard-api-notice" role="status">
            Live dashboard data is temporarily unavailable. API documentation and access requests remain available.
          </div>
        )}

        {activeView === 'overview' && (
          <Overview
            kpis={kpis}
            instruments={instruments}
            refreshTime={refreshTime}
            formatBytes={formatBytes}
            formatSeattleTime={formatSeattleTime}
            loading={loading}
          />
        )}
        {activeView === 'review' && <DataReview />}
        {activeView === 'api' && (
          <ApiSnippets
            theme={theme}
            onThemeChange={setTheme}
            onOpenDashboard={() => setActiveView('overview')}
          />
        )}
      </div>
    </div>
  );
}

function Overview({ kpis, instruments, refreshTime, formatBytes, formatSeattleTime, loading }) {
  const [referenceTime] = useState(Date.now);
  const totalCleanedRows = instruments.reduce((total, instrument) => total + (instrument.silverRows || 0), 0);
  const hasMtdCost = Number.isFinite(Number(kpis.mtdCost));
  const mtdCost = hasMtdCost
    ? Number(kpis.mtdCost).toLocaleString(undefined, {
        style: 'currency',
        currency: kpis.costCurrency || 'USD',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    : null;
  const ageDays = refreshTime
    ? Math.max(0, Math.floor((referenceTime - new Date(refreshTime).getTime()) / 86400000))
    : null;
  const ageLabel = ageDays == null ? 'Not available' : ageDays === 0 ? 'Today' : `${ageDays} days ago`;

  return (
    <main className="dashboard-page">
      <section className="location-heading">
        <div>
          <div className="location-label"><MapPin size={15} /> Des Moines, Washington</div>
          <h1>Air monitoring conditions</h1>
          <p>Live field-instrument status and quality-checked environmental observations.</p>
        </div>
        <div className="updated-label"><span>Last network update</span><strong>{formatSeattleTime(refreshTime)}</strong></div>
      </section>

      <section className="network-hero">
        <div className="network-hero-main">
          <span className="hero-orbit"><Clock size={32} /></span>
          <div>
            <span className="hero-kicker">Latest completed upload</span>
            <h2>{refreshTime ? formatSeattleTime(refreshTime) : loading ? 'Loading upload history…' : 'No upload timestamp available'}</h2>
            <p>{ageDays != null && ageDays > 1 ? `The dataset is ${ageDays} days old. Check the field-laptop schedule if this is unexpected.` : 'Timestamp reported by the research data pipeline.'}</p>
          </div>
        </div>
        <div className="network-hero-facts">
          <div><span>Latest source</span><strong>{kpis.lastUpdatedInstrument || '—'}</strong></div>
          <div><span>Clean observations</span><strong>{totalCleanedRows.toLocaleString()}</strong></div>
          <div><span>Data age</span><strong>{ageLabel}</strong></div>
        </div>
      </section>

      <section className="kpi-grid" aria-label="Monitoring summary">
        <KPICard title="Last upload" value={refreshTime ? formatSeattleTime(refreshTime).split(',')[1]?.trim() : 'No data'} unit="Local time" color="blue" Icon={Clock} />
        {hasMtdCost ? (
          <KPICard title="Month to date" value={mtdCost} unit="Internal infrastructure" color="violet" Icon={DollarSign} />
        ) : (
          <KPICard title="Cleaned rows" value={totalCleanedRows.toLocaleString()} unit="Quality checked" color="violet" Icon={Check} />
        )}
        <KPICard title="Latest source" value={kpis.lastUpdatedInstrument || '—'} unit="Reporting instrument" color="green" Icon={Activity} />
        <KPICard title="Instruments" value={instruments.length.toString()} unit="Connected sources" color="orange" Icon={Database} />
      </section>

      <TimeSeriesChart />

      <section className="surface-card inventory-card">
        <div className="section-heading">
          <div><span className="section-eyebrow">Network inventory</span><h2>Instrument activity</h2></div>
          <span className="section-meta">{instruments.length} sources</span>
        </div>

        <div className="table-scroll"><table className="data-table">
          <thead>
            <tr>
              <th>Instrument</th><th>Raw rows</th><th>Cleaned rows</th><th>Last update</th>
            </tr>
          </thead>
          <tbody>
            {instruments.map(instrument => {
              return (
                <tr key={instrument.id}>
                  <td>
                    <div className="instrument-name">
                      <div>
                        <strong>{instrument.name}</strong><small>{instrument.id}</small>
                      </div>
                    </div>
                  </td>
                  <td><strong>{(instrument.bronzeRows || 0).toLocaleString()}</strong><small>{formatBytes(instrument.bronzeSize)}</small>
                  </td>
                  <td><strong>{instrument.silverRows != null ? instrument.silverRows.toLocaleString() : '—'}</strong>
                    {instrument.silverRows != null && instrument.bronzeRows > instrument.silverRows && (
                      <small className="filtered-note"
                        title="Raw rows not carried forward: duplicates, schema mismatches, and non-approved source files"
                      >
                        {(instrument.bronzeRows - instrument.silverRows).toLocaleString()} filtered
                      </small>
                    )}
                  </td>
                  <td>{formatSeattleTime(instrument.lastUpdate)}</td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
      </section>
    </main>
  );
}

function DataReview() {
  const [instrument, setInstrument] = useState('NO2-CAPS');
  const [startTime, setStartTime] = useState('');
  const [endTime, setEndTime] = useState('');
  const [columns, setColumns] = useState([]);
  const [rows, setRows] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const displayColumns = columns.slice(0, 8);

  const loadRecordsFor = useCallback(async ({
    cursor = 0,
    selectedInstrument,
    selectedStart = '',
    selectedEnd = '',
    auto = false,
  }) => {
    setLoading(true);
    setError('');
    setMessage('');
    try {
      const params = new URLSearchParams({
        instrument: selectedInstrument,
        limit: '100',
        cursor: String(cursor),
        order: selectedStart || selectedEnd ? 'asc' : 'desc',
      });
      if (selectedStart) params.set('start', toIso(selectedStart));
      if (selectedEnd) params.set('end', toIso(selectedEnd));

      const response = await fetchApi(`${API_PATHS.observations}?${params.toString()}`);
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || `API returned ${response.status}`);
      }
      setColumns(result.columns || []);
      setRows(result.rows || []);
      setNextCursor(result.next_cursor ?? null);
      setMessage(`${auto ? 'Showing latest' : 'Loaded'} ${(result.rows || []).length} cleaned records.`);
    } catch (err) {
      setError(err.message || 'Could not load cleaned records');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRecords = (cursor = 0) => loadRecordsFor({
    cursor,
    selectedInstrument: instrument,
    selectedStart: startTime,
    selectedEnd: endTime,
  });

  useEffect(() => {
    const task = setTimeout(() => {
      loadRecordsFor({
        selectedInstrument: instrument,
        auto: true,
      });
    }, 0);
    return () => clearTimeout(task);
  }, [instrument, loadRecordsFor]);

  return (
    <main className="dashboard-page review-page">
      <section className="location-heading compact-heading">
        <div>
          <div className="location-label"><Database size={15} /> Quality-checked data</div>
          <h1>Observation explorer</h1>
          <p>Browse recent cleaned records or focus on a specific time window.</p>
        </div>
        <div className="auth-banner auth-off"><Lock size={15} /> Public read-only view</div>
      </section>

      <section className="surface-card filter-card">
        <div className="filter-grid">
          <Control label="Instrument">
            <select value={instrument} onChange={event => setInstrument(event.target.value)} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
          </Control>
          <Control label="Start Time (filter)">
            <input type="datetime-local" value={startTime} onChange={event => setStartTime(event.target.value)} className="control-input" />
          </Control>
          <Control label="End Time (filter)">
            <input type="datetime-local" value={endTime} onChange={event => setEndTime(event.target.value)} className="control-input" />
          </Control>
          <div className="control-action">
            <button onClick={() => loadRecords()} disabled={loading} className="action-button">
              <Search size={14} />
              {loading ? 'Loading' : 'Load Records'}
            </button>
          </div>
        </div>

        {(message || error) && (
          <div className={`filter-message ${error ? 'filter-message-error' : 'filter-message-success'}`}>
            {error ? <AlertTriangle size={14} /> : <Check size={14} />}
            <span>{error || message}</span>
          </div>
        )}
      </section>

      <section className="surface-card observation-card">
        <div className="section-heading">
          <div>
            <span className="section-eyebrow">Cleaned records</span>
            <h2>{instrument} observations</h2>
          </div>
          <div className="section-actions">
            <span className="section-meta">{rows.length} loaded</span>
            {nextCursor !== null && (
              <button onClick={() => loadRecords(nextCursor)} className="action-button"><RefreshCw size={14} /> Next Page</button>
            )}
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table review-table">
            <thead>
              <tr>
                <th className="review-th">Status</th>
                <th className="review-th">Timestamp</th>
                {displayColumns.map(column => <th key={column} className="review-th">{column}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map(row => {
                const flagReasonText = (row.flags || []).map(flag => flag.reason).filter(Boolean).join('; ');
                return (
                  <tr key={row.row_key}>
                    <td>
                      <span
                        title={flagReasonText || undefined}
                        className={`status-pill ${row.status === 'normal' ? 'status-normal' : row.status === 'flagged' ? 'status-flagged' : 'status-corrected'}`}
                      >
                        {row.status}
                      </span>
                    </td>
                    <td className="mono-cell">{row.timestamp || 'No timestamp'}</td>
                    {displayColumns.map(column => (
                      <td key={column} className="mono-cell">{row.values[column]}</td>
                    ))}
                  </tr>
                );
              })}
              {!rows.length && (
                <tr>
                  <td className="empty-cell" colSpan={displayColumns.length + 2}>No observations match this selection.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

function Segmented({ options, value, onChange }) {
  return (
    <div className="seg">
      {options.map(option => (
        <button
          key={option.value}
          onClick={() => onChange(option.value)}
          className={`seg-item ${value === option.value ? 'seg-item-active' : ''}`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

const SNIPPET_TASKS = [
  { value: 'raw', label: 'CSV Export' },
  { value: 'series', label: 'Time Series' },
  { value: 'records', label: 'Records (JSON)' },
];

const SNIPPET_LANGS = [
  { value: 'python', label: 'Python' },
  { value: 'r', label: 'R' },
  { value: 'curl', label: 'curl' },
];

const PUBLIC_ENDPOINTS = [
  {
    id: 'metrics',
    method: 'GET',
    path: API_PATHS.summary,
    title: 'Dashboard Summary',
    summary: 'Returns the public dashboard summary with upload freshness, instrument inventory, raw upload counts, cleaned row counts, and site status.',
    access: 'Read',
    apiCall: documentedApiUrl(API_PATHS.summary),
    params: [
      { name: 'None', required: '-', description: 'This endpoint does not require query parameters.' },
    ],
    responseFields: ['refreshTime', 'systemStatus', 'kpis', 'instruments'],
  },
  {
    id: 'series',
    method: 'GET',
    path: API_PATHS.timeseries,
    title: 'Hourly Time Series',
    summary: 'Returns hourly mean values for one measurement from the cleaned records.',
    access: 'Read',
    apiCall: `${documentedApiUrl(API_PATHS.timeseries)}?instrument=SMPS&measurement=Total%20Concentration%20(%23/cm%C2%B3)`,
    params: [
      { name: 'instrument', required: 'Yes', description: 'Instrument ID such as SMPS, NO2-CAPS, CO2-LICOR, NEPH-PM25, or BC-MA200.' },
      { name: 'measurement', required: 'No', description: 'Measurement column name. If omitted, the API chooses a default for the instrument.' },
      { name: 'start', required: 'No', description: 'ISO timestamp. Records before this time are excluded.' },
      { name: 'end', required: 'No', description: 'ISO timestamp. Records after this time are excluded.' },
    ],
    responseFields: ['instrument_id', 'measurement', 'measurements', 'series', 'plotted_rows'],
  },
  {
    id: 'observations',
    method: 'GET',
    path: API_PATHS.observations,
    title: 'Observations',
    summary: 'Returns paginated row-level cleaned records with timestamp and measurement values.',
    access: 'Read',
    apiCall: `${documentedApiUrl(API_PATHS.observations)}?instrument=SMPS&limit=100&order=desc`,
    params: [
      { name: 'instrument', required: 'Yes', description: 'Instrument ID.' },
      { name: 'start', required: 'No', description: 'ISO timestamp filter.' },
      { name: 'end', required: 'No', description: 'ISO timestamp filter.' },
      { name: 'limit', required: 'No', description: 'Number of records per page. The API caps the value.' },
      { name: 'cursor', required: 'No', description: 'Pagination cursor returned by the previous response.' },
      { name: 'order', required: 'No', description: 'Use asc or desc.' },
    ],
    responseFields: ['columns', 'rows', 'next_cursor'],
  },
  {
    id: 'observations-export',
    method: 'GET',
    path: API_PATHS.observationsExport,
    title: 'Observation Export',
    summary: 'Returns metadata and a short-lived download URL for the full cleaned observation CSV.',
    access: 'Read',
    apiCall: `${documentedApiUrl(API_PATHS.observationsExport)}?instrument=SMPS`,
    params: [
      { name: 'instrument', required: 'Yes', description: 'Instrument ID.' },
    ],
    responseFields: ['instrument_id', 'filename', 'bytes', 'url'],
  },
];

const DOC_NAV = [
  { id: 'getting-started', label: 'Overview' },
  { id: 'access', label: 'Authentication' },
  { id: 'endpoints', label: 'Endpoint reference' },
  { id: 'examples', label: 'Code examples' },
  { id: 'limits', label: 'Usage limits' },
];

function buildSnippet({ task, lang, instrument, measurement }) {
  const base = API_BASE_URL;
  const encodedMeasurement = encodeURIComponent(measurement || '');

  if (task === 'raw') {
    if (lang === 'r') {
      return `library(jsonlite)
library(httr)

api_key <- Sys.getenv("AQ_API_KEY")
headers <- c("x-api-key" = api_key)

# 1. Ask the API for a short-lived export link (valid ~5 min)
response <- GET("${base}${API_PATHS.observationsExport}?instrument=${instrument}",
                add_headers(.headers = headers))
stop_for_status(response)
meta <- fromJSON(content(response, "text", encoding = "UTF-8"))

# 2. Download the full cleaned observation CSV and read it
download.file(meta$url, "${instrument}_observations.csv", mode = "wb")
df <- read.csv("${instrument}_observations.csv", check.names = FALSE)

nrow(df)`;
    }
    if (lang === 'python') {
      return `import os
import requests
import pandas as pd

# The API returns a short-lived download link; pandas reads it directly
headers = {"x-api-key": os.environ["AQ_API_KEY"]}
url = requests.get(
    "${base}${API_PATHS.observationsExport}",
    params={"instrument": "${instrument}"},
    headers=headers,
).json()["url"]

df = pd.read_csv(url)
print(df.shape)`;
    }
    return `# 1. Get a short-lived export link
curl -H "x-api-key: $AQ_API_KEY" "${base}${API_PATHS.observationsExport}?instrument=${instrument}"

# 2. Download using the "url" field from the JSON response
curl -o ${instrument}_observations.csv "PASTE_URL_HERE"`;
  }

  if (task === 'series') {
    if (lang === 'r') {
      return `library(jsonlite)
library(httr)

api_key <- Sys.getenv("AQ_API_KEY")
headers <- c("x-api-key" = api_key)

# Hourly mean of one measurement. res$measurements lists the choices.
response <- GET("${base}${API_PATHS.timeseries}?instrument=${instrument}&measurement=${encodedMeasurement}",
                add_headers(.headers = headers))
stop_for_status(response)
res <- fromJSON(content(response, "text", encoding = "UTF-8"))

ts <- res$series          # data.frame: t (hour, UTC), v (hourly mean)
head(ts)`;
    }
    if (lang === 'python') {
      return `import os
import requests
import pandas as pd

headers = {"x-api-key": os.environ["AQ_API_KEY"]}
res = requests.get("${base}${API_PATHS.timeseries}", params={
    "instrument": "${instrument}",
    "measurement": "${measurement}",
}, headers=headers).json()

ts = pd.DataFrame(res["series"])   # columns: t (hour, UTC), v (hourly mean)
print(res["measurements"])          # available measurements
ts.head()`;
    }
    return `curl -H "x-api-key: $AQ_API_KEY" "${base}${API_PATHS.timeseries}?instrument=${instrument}&measurement=${encodedMeasurement}"`;
  }

  if (lang === 'r') {
    return `library(jsonlite)
library(httr)

api_key <- Sys.getenv("AQ_API_KEY")
headers <- c("x-api-key" = api_key)

# Paginated cleaned observations (100 per page)
response <- GET("${base}${API_PATHS.observations}?instrument=${instrument}&limit=100",
                add_headers(.headers = headers))
stop_for_status(response)
res <- fromJSON(content(response, "text", encoding = "UTF-8"))

records <- res$rows        # each row: timestamp and measurement values
res$next_cursor            # pass as &cursor= to fetch the next page`;
  }
  if (lang === 'python') {
    return `import os
import requests

headers = {"x-api-key": os.environ["AQ_API_KEY"]}
res = requests.get("${base}${API_PATHS.observations}", params={
    "instrument": "${instrument}",
    "limit": 100,
}, headers=headers).json()

rows = res["rows"]                 # timestamp and measurement values
next_cursor = res["next_cursor"]   # pass as cursor= for the next page`;
  }
  return `curl -H "x-api-key: $AQ_API_KEY" "${base}${API_PATHS.observations}?instrument=${instrument}&limit=100"`;
}

function ApiAccessDialog({ onClose }) {
  const [form, setForm] = useState({ name: '', email: '', organization: '', useCase: '' });
  const [state, setState] = useState('idle');
  const [message, setMessage] = useState('');

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const updateField = (field) => (event) => {
    setForm(current => ({ ...current, [field]: event.target.value }));
  };

  const submit = async (event) => {
    event.preventDefault();
    setState('submitting');
    setMessage('');
    try {
      const response = await fetchApi(API_PATHS.accessRequests, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name,
          email: form.email,
          organization: form.organization,
          use_case: form.useCase,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || 'The access request service is not available at the moment.');
      }
      setState('success');
      setMessage(payload.message || 'Check your email to verify this access request.');
      setForm({ name: '', email: '', organization: '', useCase: '' });
    } catch (error) {
      setState('error');
      setMessage(error.message || 'The access request service is not available at the moment.');
    }
  };

  return (
    <div className="api-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="api-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="api-access-dialog-title"
        onMouseDown={event => event.stopPropagation()}
      >
        <div className="api-dialog-header">
          <div>
            <p className="api-dialog-eyebrow">Read-only access</p>
            <h2 id="api-access-dialog-title">Request an API key</h2>
          </div>
          <button className="api-icon-button" type="button" onClick={onClose} aria-label="Close request form" title="Close">
            <X size={19} />
          </button>
        </div>
        <p className="api-dialog-copy">
          Requests are verified and reviewed before a personal read-only key is issued. API users cannot modify monitoring records.
        </p>
        <form className="api-dialog-form" onSubmit={submit}>
          <div className="api-dialog-fields">
            <label>
              <span>Name</span>
              <input value={form.name} onChange={updateField('name')} autoComplete="name" required />
            </label>
            <label>
              <span>Work email</span>
              <input value={form.email} onChange={updateField('email')} type="email" autoComplete="email" required />
            </label>
            <label className="api-field-wide">
              <span>Organization</span>
              <input value={form.organization} onChange={updateField('organization')} autoComplete="organization" />
            </label>
            <label className="api-field-wide">
              <span>Intended use</span>
              <textarea value={form.useCase} onChange={updateField('useCase')} required rows="4" />
            </label>
          </div>
          <div className="api-dialog-actions">
            <button className="api-primary-button" type="submit" disabled={state === 'submitting'}>
              <Send size={16} /> {state === 'submitting' ? 'Sending request' : 'Submit request'}
            </button>
            {message && (
              <p className={`api-dialog-message api-dialog-message-${state}`} role="status">{message}</p>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}

function ApiSnippets({ theme, onThemeChange, onOpenDashboard }) {
  const [task, setTask] = useState('raw');
  const [lang, setLang] = useState('python');
  const [instrument, setInstrument] = useState('SMPS');
  const [measurement, setMeasurement] = useState('Total Concentration (#/cm³)');
  const [measurements, setMeasurements] = useState([]);
  const [copied, setCopied] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  const [activeEndpointId, setActiveEndpointId] = useState('series');

  useEffect(() => {
    let cancelled = false;
    fetchApi(`${API_PATHS.timeseries}?instrument=${encodeURIComponent(instrument)}`)
      .then(res => res.json())
      .then(data => {
        if (cancelled) return;
        setMeasurements(data.measurements || []);
        if (data.measurement) setMeasurement(data.measurement);
      })
      .catch(() => { /* keep the default measurement */ });
    return () => { cancelled = true; };
  }, [instrument]);

  const code = buildSnippet({ task, lang, instrument, measurement });
  const closeNav = () => setNavOpen(false);
  const activeEndpoint = PUBLIC_ENDPOINTS.find(endpoint => endpoint.id === activeEndpointId) || PUBLIC_ENDPOINTS[0];

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked; the user can still select manually */
    }
  };

  return (
    <div className="api-portal">
      <header className="api-portal-header">
        <div className="api-portal-header-inner">
          <div className="api-portal-brand-group">
            <button
              className="api-icon-button"
              type="button"
              onClick={() => setNavOpen(current => !current)}
              aria-controls="api-docs-nav"
              aria-expanded={navOpen}
              aria-label="Open documentation navigation"
              title="Documentation navigation"
            >
              <Menu size={20} />
            </button>
            <div>
              <div className="api-product-name">Des Moines Air Quality API</div>
              <div className="api-product-version">Developer portal <span>v1</span></div>
            </div>
          </div>
          <div className="api-portal-actions">
            <button className="api-dashboard-button" type="button" onClick={onOpenDashboard}>Data dashboard</button>
            <div className="api-theme-switch" aria-label="Color theme">
              <button
                type="button"
                className={theme === 'light' ? 'api-theme-option api-theme-option-active' : 'api-theme-option'}
                onClick={() => onThemeChange('light')}
                aria-pressed={theme === 'light'}
                title="Use light theme"
              >
                <Sun size={15} /> <span>Light</span>
              </button>
              <button
                type="button"
                className={theme === 'dark' ? 'api-theme-option api-theme-option-active' : 'api-theme-option'}
                onClick={() => onThemeChange('dark')}
                aria-pressed={theme === 'dark'}
                title="Use dark theme"
              >
                <Moon size={15} /> <span>Dark</span>
              </button>
            </div>
          </div>
        </div>
      </header>

      {navOpen && <button className="api-nav-scrim" onClick={closeNav} aria-label="Close API navigation" />}

      <div className="api-portal-layout">
        <aside id="api-docs-nav" className={`api-portal-sidebar ${navOpen ? 'api-portal-sidebar-open' : ''}`}>
          <div className="api-sidebar-heading">
            <span>Documentation</span>
            <button className="api-icon-button api-sidebar-close" type="button" onClick={closeNav} aria-label="Close documentation navigation">
              <X size={18} />
            </button>
          </div>
          <nav className="api-portal-nav" aria-label="API documentation sections">
            {DOC_NAV.map(item => (
              <a key={item.id} href={`#${item.id}`} onClick={closeNav}>{item.label}</a>
            ))}
          </nav>
          <div className="api-sidebar-access">
            <span>API access</span>
            <strong>Personal read-only keys</strong>
            <p>Keys are issued after verification and team approval.</p>
            <button className="api-text-button" type="button" onClick={() => { closeNav(); setAccessOpen(true); }}>Request a key</button>
          </div>
        </aside>

        <main className="api-portal-content">
          <div className="api-breadcrumb">Documentation <span>/</span> Air quality data</div>

          <section id="getting-started" className="api-intro">
            <p className="api-section-kicker">Air quality observations</p>
            <h1>Air Quality Data API</h1>
            <p className="api-intro-copy">Read cleaned observations, hourly time series, and export links from the Des Moines monitoring project.</p>
            <div className="api-base-url-row">
              <div>
                <span>Base URL</span>
                <code>{API_BASE_URL}</code>
              </div>
              <button className="api-copy-small" type="button" onClick={() => navigator.clipboard?.writeText(API_BASE_URL)} title="Copy base URL">
                <Copy size={15} /> Copy
              </button>
            </div>
          </section>

          <section className="api-quickstart" aria-label="Quick start">
            <div><span>01</span><h2>Request access</h2><p>Use a work email to request a personal read-only API key.</p></div>
            <div><span>02</span><h2>Authenticate</h2><p>Send the key in the <code>x-api-key</code> request header.</p></div>
            <div><span>03</span><h2>Query data</h2><p>Select an endpoint, then use the parameter reference below.</p></div>
          </section>

          <section id="access" className="api-portal-section">
            <div className="api-section-heading">
              <div><p className="api-section-kicker">Authentication</p><h2>Use a personal API key</h2></div>
              <button className="api-primary-button" type="button" onClick={() => setAccessOpen(true)}><KeyRound size={16} /> Request API key</button>
            </div>
            <p>The API is read-only. Pass your key in a request header. Do not place it in a URL, shared notebook, screenshot, or repository.</p>
            <pre className="api-header-example"><code>x-api-key: YOUR_API_KEY</code></pre>
          </section>

          <section id="endpoints" className="api-portal-section">
            <div className="api-section-heading">
              <div><p className="api-section-kicker">Reference</p><h2>Endpoints</h2></div>
              <span className="api-read-only-label"><Lock size={14} /> Read-only</span>
            </div>
            <p>Each endpoint returns JSON, except export which returns a short-lived download link.</p>
            <div className="api-reference">
              <div className="api-endpoint-list" role="tablist" aria-label="API endpoints">
                {PUBLIC_ENDPOINTS.map(endpoint => (
                  <button
                    key={endpoint.id}
                    type="button"
                    role="tab"
                    aria-selected={activeEndpoint.id === endpoint.id}
                    className={activeEndpoint.id === endpoint.id ? 'api-endpoint-list-item api-endpoint-list-item-active' : 'api-endpoint-list-item'}
                    onClick={() => setActiveEndpointId(endpoint.id)}
                  >
                    <span className="api-method">{endpoint.method}</span>
                    <span><strong>{endpoint.title}</strong><code>{endpoint.path}</code></span>
                  </button>
                ))}
              </div>
              <article className="api-endpoint-detail" role="tabpanel">
                <div className="api-endpoint-title-row"><span className="api-method">{activeEndpoint.method}</span><code>{activeEndpoint.path}</code></div>
                <h3>{activeEndpoint.title}</h3>
                <p>{activeEndpoint.summary}</p>
                <div className="api-call-example"><span>Example request</span><code>{activeEndpoint.apiCall}</code></div>
                <div className="api-table-wrap">
                  <table className="api-table">
                    <thead><tr><th>Parameter</th><th>Required</th><th>Description</th></tr></thead>
                    <tbody>
                      {activeEndpoint.params.map(param => (
                        <tr key={`${activeEndpoint.id}-${param.name}`}><td><code>{param.name}</code></td><td>{param.required}</td><td>{param.description}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="api-response-fields"><span>Response fields</span><div>{activeEndpoint.responseFields.map(field => <code key={field}>{field}</code>)}</div></div>
              </article>
            </div>
          </section>

          <section id="examples" className="api-portal-section">
            <div className="api-section-heading"><div><p className="api-section-kicker">Code examples</p><h2>Start with Python</h2></div></div>
            <p>Choose the type of data you need. Set <code>AQ_API_KEY</code> in your environment before running the sample.</p>
            <div className="api-example-controls">
              <div className="api-control-group"><span>Resource</span><Segmented options={SNIPPET_TASKS} value={task} onChange={setTask} /></div>
              <div className="api-control-group"><span>Language</span><Segmented options={SNIPPET_LANGS} value={lang} onChange={setLang} /></div>
              <label className="api-select-field"><span>Instrument</span><select value={instrument} onChange={event => setInstrument(event.target.value)}>{INSTRUMENT_IDS.map(id => <option key={id} value={id}>{id}</option>)}</select></label>
              {task === 'series' && (
                <label className="api-select-field api-measurement-field"><span>Measurement</span><select value={measurement} onChange={event => setMeasurement(event.target.value)}>{(measurements.length ? measurements : [measurement]).map(name => <option key={name} value={name}>{formatScienceLabel(name)}</option>)}</select></label>
              )}
            </div>
            <div className="api-code-sample">
              <button onClick={copy} className={`api-copy-code ${copied ? 'api-copy-code-done' : ''}`} type="button">{copied ? <><Check size={14} /> Copied</> : <><Copy size={14} /> Copy code</>}</button>
              <pre><code>{code}</code></pre>
            </div>
          </section>

          <section id="limits" className="api-portal-section api-limits-section">
            <div><p className="api-section-kicker">Usage</p><h2>Reasonable use</h2></div>
            <p>Use pagination for row-level data, cache results in your analysis, and request a fresh export link for each download. Export links expire after a few minutes.</p>
          </section>
        </main>
      </div>

      {accessOpen && <ApiAccessDialog onClose={() => setAccessOpen(false)} />}
    </div>
  );
}

function TimeSeriesChart() {
  const [instrument, setInstrument] = useState('SMPS');
  const [measurement, setMeasurement] = useState('');
  const [measurements, setMeasurements] = useState([]);
  const [series, setSeries] = useState([]);
  const [seriesMeta, setSeriesMeta] = useState({});
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [loading, setLoading] = useState(false);
  const [rawLoading, setRawLoading] = useState(false);

  const fetchSeries = useCallback(async ({ inst, meas = '', s = '', e = '' }) => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ instrument: inst });
      if (meas) params.set('measurement', meas);
      if (s) params.set('start', toIso(s));
      if (e) params.set('end', toIso(e));
      const res = await fetchApi(`${API_PATHS.timeseries}?${params.toString()}`);
      const payload = await res.json();
      if (res.ok) {
        setMeasurements(payload.measurements || []);
        setMeasurement(payload.measurement || '');
        setSeries(payload.series || []);
        setSeriesMeta({
          sourceRows: payload.source_rows || 0,
          plottedRows: payload.plotted_rows || 0,
          skippedSchemaMismatch: payload.skipped_schema_mismatch || 0,
          skippedInvalidMeasurement: payload.skipped_invalid_measurement || 0,
        });
      }
    } catch {
      /* keep the last successful data on screen */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const task = setTimeout(() => {
      fetchSeries({ inst: instrument });
    }, 0);
    return () => clearTimeout(task);
  }, [instrument, fetchSeries]);

  const downloadCSV = () => {
    if (!series.length) return;
    const header = `time,${measurement || 'value'}`;
    const body = series.map(point => `${point.t},${point.v}`).join('\n');
    const url = URL.createObjectURL(new Blob([`${header}\n${body}\n`], { type: 'text/csv' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `${instrument}_${(measurement || 'series').replace(/[^A-Za-z0-9]+/g, '_')}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  // The full cleaned observation file can be tens of MB, so the API returns a
  // short-lived S3 link instead of proxying the CSV through Lambda.
  const downloadRaw = async () => {
    setRawLoading(true);
    try {
      const res = await fetchApi(`${API_PATHS.observationsExport}?instrument=${encodeURIComponent(instrument)}`);
      const payload = await res.json();
      if (res.ok && payload.url) {
        const link = document.createElement('a');
        link.href = payload.url;
        link.download = payload.filename || `${instrument}_observations.csv`;
        link.click();
      }
    } catch {
      /* transient; the button can be pressed again */
    } finally {
      setRawLoading(false);
    }
  };

  const fmtTick = (value) => (value ? value.slice(5, 16).replace('T', ' ') : '');
  const fmtLabel = (value) => (value ? value.slice(0, 16).replace('T', ' ') : '');
  const fmtNumber = (value) => {
    const number = Number(value);
    if (!Number.isFinite(number)) return value;
    const absolute = Math.abs(number);
    if (absolute >= 1_000_000_000) return `${(number / 1_000_000_000).toFixed(1)}B`;
    if (absolute >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
    if (absolute >= 1_000) return `${(number / 1_000).toFixed(1)}K`;
    return number.toLocaleString(undefined, { maximumFractionDigits: absolute < 10 ? 2 : 0 });
  };

  return (
    <section className="surface-card chart-card">
      <div className="section-heading chart-heading">
        <div><span className="section-eyebrow">Hourly series</span><h2>Explore measurements over time</h2></div>
        <div className="section-actions">
          <button onClick={downloadCSV} disabled={!series.length} className="action-button action-secondary" title="Hourly-averaged values shown in the chart">
            <Download size={14} /> Chart CSV
          </button>
          <button onClick={downloadRaw} disabled={rawLoading} className="action-button action-secondary" title="Full cleaned observation file for this instrument">
            <Download size={14} /> {rawLoading ? 'Preparing…' : 'Raw CSV'}
          </button>
        </div>
      </div>
      <div className="chart-controls">
          <Control label="Instrument">
            <select value={instrument} onChange={event => setInstrument(event.target.value)} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
          </Control>
          <Control label="Measurement">
            <select
              value={measurement}
              onChange={event => { setMeasurement(event.target.value); fetchSeries({ inst: instrument, meas: event.target.value, s: start, e: end }); }}
              className="control-input measurement-input"
            >
              {measurements.map(name => <option key={name} value={name}>{formatScienceLabel(name)}</option>)}
            </select>
          </Control>
          <Control label="Start">
            <input type="datetime-local" value={start} onChange={event => setStart(event.target.value)} className="control-input" />
          </Control>
          <Control label="End">
            <input type="datetime-local" value={end} onChange={event => setEnd(event.target.value)} className="control-input" />
          </Control>
          <div className="control-action">
            <button onClick={() => fetchSeries({ inst: instrument, meas: measurement, s: start, e: end })} className="action-button">
              <Search size={14} /> Apply
            </button>
          </div>
      </div>

      {loading ? (
        <div className="chart-empty">Loading measurements…</div>
      ) : series.length ? (
        <div className="chart-plot"><ResponsiveContainer width="100%" height={320}>
          <LineChart data={series} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis dataKey="t" tickFormatter={fmtTick} tick={{ fill: 'var(--text-muted)', fontSize: 11 }} tickLine={false} axisLine={{ stroke: 'var(--chart-grid)' }} minTickGap={48} />
            <YAxis tickFormatter={fmtNumber} tick={{ fill: 'var(--text-muted)', fontSize: 11 }} tickLine={false} axisLine={false} width={64} />
            <Tooltip
              contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, fontSize: 12, boxShadow: '0 14px 30px rgba(15,35,55,.12)' }}
              labelStyle={{ color: 'var(--text-muted)' }}
              itemStyle={{ color: 'var(--brand)' }}
              labelFormatter={fmtLabel}
              formatter={value => [fmtNumber(value), formatScienceLabel(measurement)]}
            />
            <Line type="monotone" dataKey="v" stroke="var(--brand)" strokeWidth={2.5} dot={false} activeDot={{ r: 5, fill: 'var(--brand)' }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer></div>
      ) : (
        <div className="chart-empty">No measurements are available for this selection.</div>
      )}

      <div className="chart-caption">
        <span>{formatScienceLabel(measurement) || 'No measurement selected'}</span><span>Hourly mean</span><span>{series.length} points</span>
      </div>
      {seriesMeta.skippedSchemaMismatch > 0 && (
        <div className="chart-warning">
          Ignored {seriesMeta.skippedSchemaMismatch.toLocaleString()} {instrument} rows with mismatched column count for this chart.
        </div>
      )}
    </section>
  );
}

function Control({ label, children }) {
  return (
    <label className="control-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function KPICard({ title, value, unit, color, Icon }) {
  return (
    <article className={`kpi-card kpi-${color}`}>
      <span className="kpi-icon"><Icon size={20} /></span>
      <span className="kpi-title">{title}</span>
      <strong>{value}</strong>
      <small>{unit}</small>
    </article>
  );
}
