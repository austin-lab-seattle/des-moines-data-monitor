import { lazy, Suspense, useCallback, useState, useEffect, useRef } from 'react';
import { AlertTriangle, Check, Copy, Database, Download, KeyRound, Lock, MapPin, Menu, Moon, RefreshCw, Search, Send, Sun, Table2, Wind, X } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
// Aircraft work has a separate local workspace and is not part of public navigation.
const FlightsLive = import.meta.env.DEV ? lazy(() => import('./FlightsLive.jsx')) : null;
const isAircraftWorkspace = import.meta.env.DEV && window.location.pathname === '/lab/aircraft';

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
const RESEARCH_API_PATHS = {
  summary: '/air-quality/v1/keyed/summary',
  timeseries: '/air-quality/v1/keyed/timeseries',
  observations: '/air-quality/v1/keyed/observations',
  observationsExport: '/air-quality/v1/keyed/observations/export',
};
const apiUrl = (path) => path;
const documentedApiUrl = (path) => `${API_BASE_URL}${path}`;
// A cold summary request currently rebuilds the S3 inventory and can take
// slightly over 15 seconds. Keep the UI loading rather than reporting a false
// outage just before the response arrives.
const API_TIMEOUT_MS = 30000;
const fetchApi = async (pathWithQuery, options = {}) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  const abortFromCaller = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener('abort', abortFromCaller, { once: true });
  try {
    return await fetch(apiUrl(pathWithQuery), { ...options, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener('abort', abortFromCaller);
  }
};
const INSTRUMENT_IDS = ['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'];
const PUBLIC_INSTRUMENTS = {
  'BC-MA200': { name: 'Black carbon', detail: 'Soot particles', column: 'BC1' },
  'CO2-LICOR': { name: 'Carbon dioxide', detail: 'CO₂', column: 'CO2_(umol_mol-1)', unit: 'ppm' },
  'NEPH-PM25': { name: 'Fine particles', detail: 'PM₂.₅', column: 'PM2.5 (µg/m³)', unit: 'µg/m³' },
  'NO2-CAPS': { name: 'Nitrogen dioxide', detail: 'NO₂', column: 'Concentration', unit: 'ppb' },
  SMPS: { name: 'Particle number', detail: 'Particle concentration', column: 'Total Concentration (#/cm³)', unit: 'particles/cm³' },
};
const INSTRUMENT_NAMES = {
  'BC-MA200': 'Black Carbon MA200',
  'CO2-LICOR': 'CO₂ LI-COR',
  'NEPH-PM25': 'Nephelometer PM₂.₅',
  'NO2-CAPS': 'NO₂ CAPS',
  SMPS: 'SMPS',
};
const RECENT_READING_COLUMNS = {
  'BC-MA200': ['BC1', 'BC2', 'BC3', 'BC4', 'BC5'],
  'CO2-LICOR': ['CO2_(umol_mol-1)'],
  'NEPH-PM25': ['PM2.5 (µg/m³)'],
  'NO2-CAPS': ['Concentration'],
  SMPS: ['Total Concentration (#/cm³)', 'Median (nm)', 'Mean (nm)', 'Geo. Mean (nm)', 'Mode (nm)'],
};
const RECENT_READING_LABELS = {
  Concentration: 'NO₂ concentration (ppb)',
  Pressure: 'Pressure',
  Temperature: 'Temperature (K)',
  Signal: 'Signal',
  Status: 'Status',
  'CO2_(umol_mol-1)': 'CO₂ (µmol/mol)',
  'H2O_(mmol_mol-1)': 'H₂O (mmol/mol)',
  'Cell_Temp_(C)': 'Cell temp (°C)',
  'Cell_Pressure_(kPa)': 'Cell pressure (kPa)',
  'Flow_Rate_(L_min-1)': 'Flow (L/min)',
  'Sample temperature': 'Sample temp',
  'Relative humidity': 'Humidity',
  'Atmospheric pressure': 'Pressure',
};
const DEFAULT_CHART_MEASUREMENT = {
  'NO2-CAPS': 'Concentration',
};
const isChartMeasurement = name => String(name || '').toUpperCase() !== 'HHMMSS';
// timestamp_iso contains UTC from the API, including older responses without
// an explicit offset. Raw instrument timestamps are retained as a fallback.
const observationTime = row => {
  const iso = row?.timestamp_iso;
  return iso ? (/(Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`) : row?.timestamp;
};
const toIso = (value) => value ? new Date(value).toISOString() : '';
const formatScienceLabel = (value) => String(value || '').replaceAll('cm�', 'cm³');
const recentColumnsFor = (instrumentId, columns) => {
  const available = new Set(columns);
  const preferred = (RECENT_READING_COLUMNS[instrumentId] || []).filter(column => available.has(column));
  if (preferred.length) return preferred.slice(0, 5);
  return [];
};
const formatReadingValue = (value, column) => {
  if (value == null || value === '') return '—';
  if (/(status|state|error)/i.test(column)) return String(value);
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (numeric !== 0 && Math.abs(numeric) < 0.001) return numeric.toExponential(3);
  return numeric.toLocaleString(undefined, { maximumFractionDigits: 3 });
};
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
  const [activeView, setActiveView] = useState(isAircraftWorkspace ? 'aircraft' : 'overview');
  const [theme, setTheme] = useState(getInitialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem('aq-dashboard-theme', theme);
  }, [theme]);

  const selectView = (view) => {
    setActiveView(view);
    const path = view === 'aircraft' ? '/lab/aircraft' : '/';
    if (view !== 'api' && window.location.pathname !== path) window.history.replaceState({}, '', path);
  };

  const loadReadings = useCallback(async () => {
    const results = await Promise.allSettled(INSTRUMENT_IDS.map(async id => {
      const params = new URLSearchParams({ instrument: id, limit: '1', order: 'desc' });
      const response = await fetchApi(`${API_PATHS.observations}?${params}`);
      if (response.status === 404) return { id, ...PUBLIC_INSTRUMENTS[id], latest: null };
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      const result = await response.json();
      const latest = result.rows?.[0] || null;
      const stamp = observationTime(latest);
      return { id, ...PUBLIC_INSTRUMENTS[id], latest,
        older: Boolean(stamp && Date.now() - new Date(stamp).getTime() > 60 * 60 * 1000) };
    }));
    setData(previous => ({ instruments: results.map((result, index) => {
      if (result.status === 'fulfilled') return result.value;
      const id = INSTRUMENT_IDS[index];
      const cached = previous?.instruments?.find(instrument => instrument.id === id);
      return { ...cached, id, ...PUBLIC_INSTRUMENTS[id], unavailable: true };
    }) }));
    setError(results.some(result => result.status === 'rejected') ? 'Some readings could not be refreshed.' : null);
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    const initial = setTimeout(loadReadings, 0);
    const interval = setInterval(loadReadings, 60000);
    return () => { clearTimeout(initial); clearInterval(interval); };
  }, [loadReadings]);

  const handleRefresh = () => {
    setRefreshing(true);
    loadReadings();
  };

  const instruments = data?.instruments || INSTRUMENT_IDS.map(id => ({ id, ...PUBLIC_INSTRUMENTS[id] }));

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
          <button className="site-brand" type="button" onClick={() => selectView('overview')} aria-label="Open overview">
            <span className="brand-mark"><Wind size={22} /></span>
            <span><strong>Des Moines Air</strong><small>Environmental monitor</small></span>
          </button>

          <nav className="site-nav" aria-label="Main navigation">
            {[
              { id: 'overview', label: 'Conditions' },
              { id: 'review', label: 'Observations' },
              { id: 'api', label: 'Developers' },
            ].map(tab => (
              <button key={tab.id} onClick={() => selectView(tab.id)} className={activeView === tab.id ? 'site-nav-active' : ''}>
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
            Loading the latest air-quality readings…
          </div>
        )}

        {!loading && error && activeView !== 'api' && (
          <div className="dashboard-api-notice" role="status">
            Some readings could not be refreshed. Check the time shown beside each reading.
          </div>
        )}

        {activeView === 'overview' && (
          <Overview
            instruments={instruments}
            formatSeattleTime={formatSeattleTime}
            loading={loading}
          />
        )}
        {activeView === 'review' && <DataReview />}
        {activeView === 'aircraft' && FlightsLive && (
          <Suspense fallback={<div className="loading-screen">Loading aircraft workspace…</div>}>
            <FlightsLive />
          </Suspense>
        )}
        {activeView === 'api' && (
          <ApiSnippets
            theme={theme}
            onThemeChange={setTheme}
            onOpenDashboard={() => selectView('overview')}
          />
        )}
      </div>
    </div>
  );
}

function Overview({ instruments, formatSeattleTime, loading }) {
  const [recentInstrument, setRecentInstrument] = useState(null);

  return (
    <main className="dashboard-page">
      <section className="location-heading">
        <div>
          <div className="location-label"><MapPin size={15} /> Des Moines, Washington</div>
          <h1>Air monitoring conditions</h1>
          <p>Explore recent pollutant measurements near Sea-Tac Airport.</p>
        </div>
      </section>

      <section className="latest-conditions" aria-labelledby="latest-conditions-title">
        <h2 id="latest-conditions-title">Latest readings</h2>
        <p className="latest-conditions-copy">Select a pollutant to see recent values. Reading times are shown in Pacific time.</p>
        <div className="pollutant-grid">
            {instruments.map(instrument => {
              const value = instrument.latest?.values?.[instrument.column];
              const stamp = observationTime(instrument.latest);
              const oldReading = instrument.older;
              return (
                <button key={instrument.id} type="button" className="pollutant-card"
                  onClick={() => setRecentInstrument(instrument)}
                  aria-label={`View latest readings from ${instrument.name}`}>
                  <span className="pollutant-name">{instrument.name}</span>
                  <span className="pollutant-detail">{instrument.detail}</span>
                  <span className="pollutant-instrument">{INSTRUMENT_NAMES[instrument.id]}</span>
                  <span className="pollutant-instrument-id">{instrument.id}</span>
                  <strong className="pollutant-value">{loading ? '…' : formatReadingValue(value, instrument.column)}</strong>
                  <span className="pollutant-unit">{instrument.unit || (value == null ? 'Reading unavailable' : instrument.column)}</span>
                  <span className={`pollutant-time ${oldReading || instrument.unavailable ? 'pollutant-time-old' : ''}`}>
                    {loading ? 'Loading…' : instrument.latest
                      ? `${instrument.unavailable ? 'Last available: ' : oldReading ? 'Older reading: ' : ''}${formatSeattleTime(stamp || instrument.latest.timestamp)}`
                      : instrument.unavailable ? 'Temporarily unavailable' : 'No reading available'}
                  </span>
                  <span className="pollutant-open"><Table2 size={14} /> Recent values</span>
                </button>
              );
            })}
        </div>
      </section>

      <TimeSeriesChart />

      {recentInstrument && (
        <LatestReadingsDialog
          instrument={recentInstrument}
          formatSeattleTime={formatSeattleTime}
          onClose={() => setRecentInstrument(null)}
        />
      )}
    </main>
  );
}

function LatestReadingsDialog({ instrument, formatSeattleTime, onClose }) {
  const [columns, setColumns] = useState([]);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const closeButtonRef = useRef(null);
  const displayColumns = recentColumnsFor(instrument.id, columns);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError('');
      try {
        const params = new URLSearchParams({
          instrument: instrument.id,
          limit: '8',
          order: 'desc',
        });
        const response = await fetchApi(`${API_PATHS.observations}?${params.toString()}`, {
          signal: controller.signal,
        });
        const result = await response.json();
        if (!response.ok) {
          if (response.status === 404) throw new Error('No readings are available for this pollutant yet.');
          throw new Error(result.error || `API returned ${response.status}`);
        }
        setColumns(result.columns || []);
        setRows(result.rows || []);
      } catch (err) {
        if (err.name !== 'AbortError') setError(err.message || 'Could not load recent readings.');
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    load();
    closeButtonRef.current?.focus();
    return () => controller.abort();
  }, [instrument.id]);

  useEffect(() => {
    const handleKeyDown = event => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="readings-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className={`readings-dialog ${displayColumns.length > 2 ? 'readings-dialog-wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="readings-dialog-title"
        onMouseDown={event => event.stopPropagation()}
      >
        <div className="readings-dialog-header">
          <div>
            <span className="section-eyebrow">Recent readings</span>
            <h2 id="readings-dialog-title">{instrument.name}</h2>
            <p>{INSTRUMENT_NAMES[instrument.id]} · {instrument.id}</p>
            <p>Newest reading first · Pacific time</p>
          </div>
          <button ref={closeButtonRef} type="button" onClick={onClose} aria-label="Close recent readings">
            <X size={19} />
          </button>
        </div>

        {loading && (
          <div className="readings-dialog-state" role="status">
            <RefreshCw size={18} className="animate-spin" /> Loading recent readings…
          </div>
        )}
        {!loading && error && (
          <div className="readings-dialog-state readings-dialog-error" role="alert">
            <AlertTriangle size={18} /> {error}
          </div>
        )}
        {!loading && !error && !rows.length && (
          <div className="readings-dialog-state">No recent readings are available.</div>
        )}
        {!loading && !error && rows.length > 0 && (
          <div className="readings-table-scroll">
            <table className="readings-table">
              <thead>
                <tr>
                  <th>Recorded (Pacific time)</th>
                  {displayColumns.map(column => (
                    <th key={column}>{RECENT_READING_LABELS[column] || formatScienceLabel(column)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map(row => (
                  <tr key={row.row_key}>
                    <td className="readings-time">{formatSeattleTime(observationTime(row))}</td>
                    {displayColumns.map(column => (
                      <td key={column}>{formatReadingValue(row.values?.[column], column)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="readings-dialog-footer">
          <span>Latest available measurements</span>
          <button type="button" onClick={onClose}>Close</button>
        </div>
      </section>
    </div>
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
  const recordsRequestId = useRef(0);

  const displayColumns = recentColumnsFor(instrument, columns);

  const loadRecordsFor = useCallback(async ({
    cursor = 0,
    selectedInstrument,
    selectedStart = '',
    selectedEnd = '',
    auto = false,
  }) => {
    const requestId = ++recordsRequestId.current;
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
      if (requestId === recordsRequestId.current) {
        setColumns(result.columns || []);
        setRows(result.rows || []);
        setNextCursor(result.next_cursor ?? null);
        setMessage(auto ? 'Showing recent readings, newest first.' : 'Readings loaded.');
      }
    } catch (err) {
      if (requestId === recordsRequestId.current) {
        setError(err.message || 'Could not load cleaned records');
      }
    } finally {
      if (requestId === recordsRequestId.current) setLoading(false);
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
          <div className="location-label"><Database size={15} /> Recorded measurements</div>
          <h1>Observation explorer</h1>
          <p>Browse recent pollutant readings or explore a specific time window.</p>
        </div>
        <div className="auth-banner auth-off"><Lock size={15} /> Public read-only view</div>
      </section>

      <section className="surface-card filter-card">
        <div className="filter-grid">
          <Control label="Instrument">
            <select value={instrument} onChange={event => setInstrument(event.target.value)} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{PUBLIC_INSTRUMENTS[id].name} — {INSTRUMENT_NAMES[id]}</option>)}
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
            <span className="section-eyebrow">Recent readings</span>
            <h2>{PUBLIC_INSTRUMENTS[instrument]?.name || instrument}</h2>
          </div>
          <div className="section-actions">
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
                {displayColumns.map(column => <th key={column} className="review-th">{RECENT_READING_LABELS[column] || formatScienceLabel(column)}</th>)}
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
                      <td key={column} className="mono-cell">{formatReadingValue(row.values[column], column)}</td>
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
    path: RESEARCH_API_PATHS.summary,
    title: 'Dashboard Summary',
    summary: 'Returns the public dashboard summary with upload freshness, instrument inventory, raw upload counts, cleaned row counts, and site status.',
    access: 'Read',
    apiCall: `curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${documentedApiUrl(RESEARCH_API_PATHS.summary)}"`,
    params: [
      { name: 'None', required: '-', description: 'This endpoint does not require query parameters.' },
    ],
    responseFields: ['refreshTime', 'systemStatus', 'kpis', 'instruments'],
  },
  {
    id: 'series',
    method: 'GET',
    path: RESEARCH_API_PATHS.timeseries,
    title: 'Time Series',
    summary: 'Returns bucketed mean values for one measurement from the cleaned records.',
    access: 'Read',
    apiCall: `curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${documentedApiUrl(RESEARCH_API_PATHS.timeseries)}?instrument=SMPS&measurement=Total%20Concentration%20(%23/cm%C2%B3)"`,
    params: [
      { name: 'instrument', required: 'Yes', description: 'Instrument ID such as SMPS, NO2-CAPS, CO2-LICOR, NEPH-PM25, or BC-MA200.' },
      { name: 'measurement', required: 'No', description: 'Measurement column name. If omitted, the API chooses a default for the instrument.' },
      { name: 'start', required: 'No', description: 'ISO timestamp. Records before this time are excluded.' },
      { name: 'end', required: 'No', description: 'ISO timestamp. Records after this time are excluded.' },
      { name: 'bucket_minutes', required: 'No', description: 'Aggregation resolution: 1, 5, 15, or 60 minutes. Defaults to 60.' },
    ],
    responseFields: ['instrument_id', 'measurement', 'measurements', 'bucket_minutes', 'series', 'plotted_rows'],
  },
  {
    id: 'observations',
    method: 'GET',
    path: RESEARCH_API_PATHS.observations,
    title: 'Observations',
    summary: 'Returns paginated row-level cleaned records with timestamp and measurement values.',
    access: 'Read',
    apiCall: `curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${documentedApiUrl(RESEARCH_API_PATHS.observations)}?instrument=SMPS&limit=100&order=desc"`,
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
    path: RESEARCH_API_PATHS.observationsExport,
    title: 'Observation Export',
    summary: 'Returns a short-lived download URL for a bounded, date-filtered cleaned CSV.',
    access: 'Read',
    apiCall: `curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${documentedApiUrl(RESEARCH_API_PATHS.observationsExport)}?instrument=SMPS"`,
    params: [
      { name: 'instrument', required: 'Yes', description: 'Instrument ID.' },
      { name: 'start', required: 'No', description: 'ISO timestamp. Defaults to 14 days before end.' },
      { name: 'end', required: 'No', description: 'ISO timestamp. Defaults to the latest available observation.' },
    ],
    responseFields: ['instrument_id', 'filename', 'rows', 'start', 'end', 'bytes', 'url'],
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
response <- GET("${base}${RESEARCH_API_PATHS.observationsExport}?instrument=${instrument}",
                add_headers(.headers = headers))
stop_for_status(response)
meta <- fromJSON(content(response, "text", encoding = "UTF-8"))

# 2. Download the cleaned CSV (latest 14 days by default; 31-day maximum)
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
response = requests.get(
    "${base}${RESEARCH_API_PATHS.observationsExport}",
    params={"instrument": "${instrument}"},
    headers=headers,
)
response.raise_for_status()
url = response.json()["url"]

df = pd.read_csv(url)
print(df.shape)`;
    }
    return `# 1. Get a short-lived export link
curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${base}${RESEARCH_API_PATHS.observationsExport}?instrument=${instrument}"

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
response <- GET("${base}${RESEARCH_API_PATHS.timeseries}?instrument=${instrument}&measurement=${encodedMeasurement}",
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
response = requests.get("${base}${RESEARCH_API_PATHS.timeseries}", params={
    "instrument": "${instrument}",
    "measurement": "${measurement}",
}, headers=headers)
response.raise_for_status()
res = response.json()

ts = pd.DataFrame(res["series"])   # columns: t (hour, UTC), v (hourly mean)
print(res["measurements"])          # available measurements
ts.head()`;
    }
    return `curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${base}${RESEARCH_API_PATHS.timeseries}?instrument=${instrument}&measurement=${encodedMeasurement}"`;
  }

  if (lang === 'r') {
    return `library(jsonlite)
library(httr)

api_key <- Sys.getenv("AQ_API_KEY")
headers <- c("x-api-key" = api_key)

# Paginated cleaned observations (100 per page)
response <- GET("${base}${RESEARCH_API_PATHS.observations}?instrument=${instrument}&limit=100",
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
response = requests.get("${base}${RESEARCH_API_PATHS.observations}", params={
    "instrument": "${instrument}",
    "limit": 100,
}, headers=headers)
response.raise_for_status()
res = response.json()

rows = res["rows"]                 # timestamp and measurement values
next_cursor = res["next_cursor"]   # pass as cursor= for the next page`;
  }
  return `curl --fail-with-body -sS -H "x-api-key: $AQ_API_KEY" "${base}${RESEARCH_API_PATHS.observations}?instrument=${instrument}&limit=100"`;
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
      setMessage(payload.message || 'Check your email for the verification link.');
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
          Submit your email, open the verification link, and your personal read-only key will be generated and emailed automatically. API users cannot modify monitoring records.
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
            <p>Verify your email and your personal read-only key will be generated and emailed automatically.</p>
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
            <p>The researcher API is read-only and every documented endpoint requires a valid key. Pass it in a request header; invalid keys are rejected.</p>
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
            <p>Each key is limited to 30 requests per minute and 5,000 requests per day. CSV exports are limited to 20 per day. Use pagination, cache analysis results, and request a fresh export link for each download.</p>
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
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [loading, setLoading] = useState(false);
  const [exportLoading, setExportLoading] = useState(false);
  const [exportMessage, setExportMessage] = useState('');
  const seriesRequestId = useRef(0);

  const fetchSeries = useCallback(async ({ inst, meas = '', s = '', e = '' }) => {
    const requestId = ++seriesRequestId.current;
    setLoading(true);
    setSeries([]);
    try {
      const params = new URLSearchParams({ instrument: inst });
      if (meas) params.set('measurement', meas);
      if (s) params.set('start', toIso(s));
      if (e) params.set('end', toIso(e));
      const res = await fetchApi(`${API_PATHS.timeseries}?${params.toString()}`);
      const payload = await res.json();
      // A slower response for the previously selected measurement must never
      // replace the data (and Y-axis scale) for the current selection.
      if (requestId === seriesRequestId.current && res.ok) {
        const publicColumns = RECENT_READING_COLUMNS[inst] || [];
        const availableMeasurements = (payload.measurements || []).filter(name => isChartMeasurement(name) && publicColumns.includes(name));
        const selectedMeasurement = isChartMeasurement(payload.measurement)
          ? payload.measurement
          : '';
        setMeasurements(availableMeasurements);
        setMeasurement(selectedMeasurement);
        setSeries(selectedMeasurement ? (payload.series || []) : []);
      }
    } catch {
      /* the empty state already replaces data from the previous selection */
    } finally {
      if (requestId === seriesRequestId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const task = setTimeout(() => {
      fetchSeries({ inst: 'SMPS' });
    }, 0);
    return () => clearTimeout(task);
  }, [fetchSeries]);

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

  // The API materializes only the selected bounded time window and returns a
  // short-lived S3 link. With no filters it exports the latest 14 days.
  const downloadExport = async () => {
    setExportLoading(true);
    setExportMessage('');
    try {
      const params = new URLSearchParams({ instrument });
      if (start) params.set('start', toIso(start));
      if (end) params.set('end', toIso(end));
      const res = await fetchApi(`${API_PATHS.observationsExport}?${params.toString()}`);
      const payload = await res.json();
      if (res.ok && payload.url) {
        const link = document.createElement('a');
        link.href = payload.url;
        link.download = payload.filename || `${instrument}_observations.csv`;
        link.click();
      } else if (res.status === 404) {
        setExportMessage('No measurements are available to download for this pollutant yet.');
      } else {
        setExportMessage(payload.error || 'The cleaned CSV could not be prepared. Please try again.');
      }
    } catch {
      setExportMessage('The cleaned CSV could not be prepared. Please try again.');
    } finally {
      setExportLoading(false);
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
        <div><span className="section-eyebrow">Hourly averages</span><h2>Explore readings over time</h2></div>
        <div className="section-actions">
          <button onClick={downloadCSV} disabled={!series.length} className="action-button action-secondary" title="Hourly-averaged values shown in the chart">
            <Download size={14} /> Chart CSV
          </button>
          <button onClick={downloadExport} disabled={exportLoading} className="action-button action-secondary" title="Download measurements for the selected dates; defaults to the latest 14 days">
            <Download size={14} /> {exportLoading ? 'Preparing…' : 'Export CSV'}
          </button>
        </div>
      </div>
      {exportMessage && (
        <div className="filter-message filter-message-error" role="status">
          <AlertTriangle size={14} />
          <span>{exportMessage}</span>
        </div>
      )}
      <div className="chart-controls">
          <Control label="Instrument">
            <select value={instrument} onChange={event => {
              const nextInstrument = event.target.value;
              const nextMeasurement = DEFAULT_CHART_MEASUREMENT[nextInstrument] || '';
              setInstrument(nextInstrument);
              setMeasurement(nextMeasurement);
              setMeasurements([]);
              setExportMessage('');
              fetchSeries({ inst: nextInstrument, meas: nextMeasurement, s: start, e: end });
            }} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{PUBLIC_INSTRUMENTS[id].name} — {INSTRUMENT_NAMES[id]}</option>)}
            </select>
          </Control>
          <Control label="Measurement">
            <select
              value={measurement}
              onChange={event => { setMeasurement(event.target.value); fetchSeries({ inst: instrument, meas: event.target.value, s: start, e: end }); }}
              className="control-input measurement-input"
            >
              {measurements.map(name => <option key={name} value={name}>{RECENT_READING_LABELS[name] || formatScienceLabel(name)}</option>)}
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
          <LineChart key={`${instrument}:${measurement}`} data={series} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
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
        <span>{RECENT_READING_LABELS[measurement] || formatScienceLabel(measurement) || 'No measurement selected'}</span><span>Hourly average · chart times in UTC</span>
      </div>
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
