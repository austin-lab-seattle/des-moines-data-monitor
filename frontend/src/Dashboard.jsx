import { useCallback, useState, useEffect } from 'react';
import { Activity, AlertTriangle, CheckSquare, Clock, DollarSign, Edit3, MapPin, RefreshCw, Save, Search } from 'lucide-react';

const API_URL = import.meta.env.VITE_API_URL || 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/metrics';
const API_BASE_URL = API_URL.replace(/\/metrics\/?$/, '');
const INSTRUMENT_IDS = ['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'];
const toIso = (value) => value ? new Date(value).toISOString() : '';
const getReviewApiKey = () => {
  if (typeof window === 'undefined') return '';
  const params = new URLSearchParams(window.location.search);
  return params.get('api_key') || params.get('review_key') || '';
};

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [activeView, setActiveView] = useState('overview');

  useEffect(() => {
    const fetchData = async () => {
      try {
        const response = await fetch(API_URL);
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
      const response = await fetch(API_URL);
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

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-black text-gray-400 font-mono">
        INITIALIZING SENSORS...
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col gap-5 items-center justify-center min-h-screen bg-black text-gray-300 font-mono p-6">
        <div className="text-sm tracking-widest text-red-400">API CONNECTION UNAVAILABLE</div>
        <div className="max-w-xl text-center text-xs text-gray-500">{error || 'No metrics payload returned.'}</div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-2 text-xs font-bold tracking-widest uppercase px-3 py-1.5 rounded border border-cyan-400 text-cyan-400 disabled:opacity-50"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
          Retry
        </button>
      </div>
    );
  }

  const { kpis, instruments, refreshTime, systemStatus } = data;

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
    <div
      className="min-h-screen bg-black text-gray-300 font-sans p-6"
      style={{
        backgroundImage: 'linear-gradient(rgba(5, 5, 5, 0.85), rgba(5, 5, 5, 0.95)), url("/bg.png")',
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        backgroundAttachment: 'fixed'
      }}
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex justify-between items-baseline border-b border-gray-800/60 pb-3 mb-8">
          <h1 className="text-xl font-black tracking-wider text-white">
            AQ MONITOR <span className="text-gray-500">| <span className="text-cyan-400">SEATTLE</span></span>
          </h1>
          <div className="flex items-center gap-4">
            <div className="flex rounded border border-gray-800/80 overflow-hidden">
              <button
                onClick={() => setActiveView('overview')}
                className={`text-[0.65rem] font-bold tracking-widest uppercase px-3 py-1.5 ${activeView === 'overview' ? 'bg-cyan-400 text-black' : 'text-gray-400 hover:text-cyan-300'}`}
              >
                Overview
              </button>
              <button
                onClick={() => setActiveView('review')}
                className={`text-[0.65rem] font-bold tracking-widest uppercase px-3 py-1.5 border-l border-gray-800/80 ${activeView === 'review' ? 'bg-cyan-400 text-black' : 'text-gray-400 hover:text-cyan-300'}`}
              >
                Data Review
              </button>
            </div>
            <div className="text-xs font-semibold text-gray-500 tracking-wider">
              SYSTEM STATUS: <span className={systemStatus === 'ONLINE' ? 'text-green-400' : 'text-red-400'}>{systemStatus || 'CHECKING...'}</span>
            </div>
            <button
              id="refresh-btn"
              onClick={handleRefresh}
              disabled={refreshing}
              className="text-xs font-bold tracking-widest uppercase px-3 py-1.5 rounded border transition-all"
              style={{
                borderColor: refreshing ? '#374151' : '#22d3ee',
                color: refreshing ? '#6b7280' : '#22d3ee',
                background: 'transparent',
                cursor: refreshing ? 'not-allowed' : 'pointer',
                opacity: refreshing ? 0.5 : 1,
              }}
            >
              <span className="inline-flex items-center gap-2">
                <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
                {refreshing ? 'REFRESHING...' : 'REFRESH'}
              </span>
            </button>
          </div>
        </header>

        {activeView === 'overview' ? (
          <Overview
            kpis={kpis}
            instruments={instruments}
            refreshTime={refreshTime}
            formatBytes={formatBytes}
            formatSeattleTime={formatSeattleTime}
          />
        ) : (
          <DataReview />
        )}
      </div>
    </div>
  );
}

function Overview({ kpis, instruments, refreshTime, formatBytes, formatSeattleTime }) {
  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5 mb-10">
        <KPICard title="LAST UPLOAD" value={refreshTime ? formatSeattleTime(refreshTime).split(',')[1]?.trim() : 'NO DATA'} unit="SEATTLE TIME" color="text-cyan-400" Icon={Clock} />
        <KPICard title="MTD COST" value={kpis.mtdCost === "N/A" ? "N/A" : `$${kpis.mtdCost}`} unit={kpis.costScope || "AWS ACCOUNT MTD"} color="text-pink-500" Icon={DollarSign} />
        <KPICard title="LATEST UPLOAD" value={kpis.lastUpdatedInstrument} unit="INSTRUMENT" color="text-green-400" Icon={Activity} />
        <KPICard title="SITE NAME" value={kpis.siteName} unit="LOCATION" color="text-cyan-400" Icon={MapPin} />
      </div>

      <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-6 shadow-2xl">
        <div className="flex justify-between border-b border-gray-800/50 pb-3 mb-4">
          <h2 className="text-[0.70rem] font-extrabold text-gray-400 tracking-widest uppercase">INSTRUMENT DATA INVENTORY</h2>
        </div>

        <table className="w-full text-left border-collapse">
          <thead>
            <tr>
              <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50">Instrument</th>
              <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50 text-right">Bronze Rows</th>
              <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50 text-right">Silver Rows</th>
              <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50 text-right">Last Update Time (PST/PDT)</th>
            </tr>
          </thead>
          <tbody>
            {instruments.map(instrument => {
              const isActive = instrument.lastUpdate !== null;

              return (
                <tr key={instrument.id} className="hover:bg-white/5 transition-colors group">
                  <td className="py-5 border-b border-gray-800/30">
                    <div className="flex items-center">
                      <span className={`w-2 h-2 rounded-full mr-4 ${isActive ? 'bg-cyan-400 shadow-[0_0_8px_#22d3ee]' : 'bg-gray-700'}`}></span>
                      <div>
                        <div className="font-bold text-gray-200 text-sm tracking-wide group-hover:text-cyan-300 transition-colors">{instrument.name}</div>
                        <div className="font-mono text-gray-500 text-[0.65rem]">{instrument.id}</div>
                      </div>
                    </div>
                  </td>
                  <td className="py-5 border-b border-gray-800/30 text-right">
                    <div className="text-white font-bold">{(instrument.bronzeRows || 0).toLocaleString()}</div>
                    <div className="text-gray-500 text-[0.6rem] tracking-wider uppercase">{formatBytes(instrument.bronzeSize)}</div>
                  </td>
                  <td className="py-5 border-b border-gray-800/30 text-right">
                    <div className="text-cyan-300 font-bold">{instrument.silverRows != null ? instrument.silverRows.toLocaleString() : '-'}</div>
                    {instrument.silverRows != null && instrument.bronzeRows > instrument.silverRows && (
                      <div className="text-amber-400/70 text-[0.6rem] tracking-wider uppercase">-{(instrument.bronzeRows - instrument.silverRows).toLocaleString()} dupes</div>
                    )}
                  </td>
                  <td className="py-5 border-b border-gray-800/30 text-right">
                    <div className="font-mono text-sm text-gray-300">{formatSeattleTime(instrument.lastUpdate)}</div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function DataReview() {
  const reviewApiKey = getReviewApiKey();
  const [instrument, setInstrument] = useState('NO2-CAPS');
  const [startTime, setStartTime] = useState('');
  const [endTime, setEndTime] = useState('');
  const [reason, setReason] = useState('');
  const [columns, setColumns] = useState([]);
  const [rows, setRows] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [selectedKeys, setSelectedKeys] = useState(new Set());
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [editingRow, setEditingRow] = useState(null);
  const [correctionValues, setCorrectionValues] = useState({});

  const selectedRows = rows.filter(row => selectedKeys.has(row.row_key));
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

      const response = await fetch(`${API_BASE_URL}/silver-records?${params.toString()}`);
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || `API returned ${response.status}`);
      }
      setColumns(result.columns || []);
      setRows(result.rows || []);
      setNextCursor(result.next_cursor ?? null);
      setSelectedKeys(new Set());
      setEditingRow(null);
      setCorrectionValues({});
      setMessage(`${auto ? 'Showing latest' : 'Loaded'} ${(result.rows || []).length} silver records.`);
    } catch (err) {
      setError(err.message || 'Could not load silver records');
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

  const toggleSelected = (rowKey) => {
    const next = new Set(selectedKeys);
    if (next.has(rowKey)) {
      next.delete(rowKey);
    } else {
      next.add(rowKey);
    }
    setSelectedKeys(next);
  };

  const postReview = async (path, payload) => {
    setError('');
    setMessage('');
    if (!reviewApiKey) {
      throw new Error('Open the dashboard with ?api_key=... to save flags or corrections.');
    }
    const params = new URLSearchParams({ api_key: reviewApiKey });
    const response = await fetch(`${API_BASE_URL}${path}?${params.toString()}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.message || result.error || `API returned ${response.status}`);
    }
    return result;
  };

  const saveSelectedFlag = async () => {
    if (!selectedRows.length) {
      setError('Select at least one row.');
      return;
    }
    try {
      await postReview('/record-flags', {
        instrument_id: instrument,
        scope: 'selected_rows',
        row_keys: selectedRows.map(row => row.row_key),
        reason,
      });
      setMessage(`Saved flag for ${selectedRows.length} selected records.`);
      await loadRecords();
    } catch (err) {
      setError(err.message || 'Could not save flag');
    }
  };

  const saveRangeFlag = async () => {
    if (!startTime || !endTime) {
      setError('Select start and end time for a range flag.');
      return;
    }
    try {
      await postReview('/record-flags', {
        instrument_id: instrument,
        scope: 'time_range',
        start_time: toIso(startTime),
        end_time: toIso(endTime),
        reason,
      });
      setMessage('Saved flag for the selected time range.');
      await loadRecords();
    } catch (err) {
      setError(err.message || 'Could not save range flag');
    }
  };

  const startCorrection = () => {
    if (selectedRows.length !== 1) {
      setError('Select exactly one row to correct.');
      return;
    }
    const row = selectedRows[0];
    setEditingRow(row);
    setCorrectionValues({ ...row.values });
    setError('');
  };

  const saveCorrection = async () => {
    if (!editingRow) return;
    const changedValues = Object.fromEntries(
      Object.entries(correctionValues).filter(([key, value]) => editingRow.values[key] !== value)
    );
    if (!Object.keys(changedValues).length) {
      setError('Change at least one value before saving correction.');
      return;
    }
    try {
      await postReview('/record-corrections', {
        instrument_id: instrument,
        row_key: editingRow.row_key,
        timestamp: editingRow.timestamp,
        original_values: editingRow.values,
        corrected_values: changedValues,
        reason,
      });
      setMessage('Saved correction for selected record.');
      setEditingRow(null);
      setCorrectionValues({});
      await loadRecords();
    } catch (err) {
      setError(err.message || 'Could not save correction');
    }
  };

  return (
    <div className="grid gap-5">
      <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-5 shadow-2xl">
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-3">
          <Control label="Instrument">
            <select value={instrument} onChange={event => setInstrument(event.target.value)} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
          </Control>
          <Control label="Start Time">
            <input type="datetime-local" value={startTime} onChange={event => setStartTime(event.target.value)} className="control-input" />
          </Control>
          <Control label="End Time">
            <input type="datetime-local" value={endTime} onChange={event => setEndTime(event.target.value)} className="control-input" />
          </Control>
          <Control label="Reason">
            <input value={reason} onChange={event => setReason(event.target.value)} className="control-input" />
          </Control>
          <div className="flex items-end">
            <button onClick={() => loadRecords()} disabled={loading} className="action-button w-full">
              <Search size={14} />
              {loading ? 'Loading' : 'Load Records'}
            </button>
          </div>
        </div>

        {(message || error) && (
          <div className={`mt-4 text-xs font-semibold tracking-wide ${error ? 'text-red-400' : 'text-green-400'}`}>
            {error || message}
          </div>
        )}
      </div>

      <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-5 shadow-2xl">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-800/50 pb-3 mb-4">
          <div>
            <h2 className="text-[0.70rem] font-extrabold text-gray-400 tracking-widest uppercase">SILVER RECORD REVIEW</h2>
            <div className="text-[0.65rem] text-gray-500 mt-1">{selectedRows.length} selected</div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button onClick={saveSelectedFlag} className="action-button"><CheckSquare size={14} /> Flag Selected</button>
            <button onClick={saveRangeFlag} className="action-button"><AlertTriangle size={14} /> Flag Range</button>
            <button onClick={startCorrection} className="action-button"><Edit3 size={14} /> Correct One</button>
            {nextCursor !== null && (
              <button onClick={() => loadRecords(nextCursor)} className="action-button"><RefreshCw size={14} /> Next Page</button>
            )}
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse min-w-[980px]">
            <thead>
              <tr>
                <th className="review-th w-10"></th>
                <th className="review-th">Status</th>
                <th className="review-th">Timestamp</th>
                {displayColumns.map(column => <th key={column} className="review-th">{column}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={row.row_key} className="hover:bg-white/5 transition-colors">
                  <td className="review-td">
                    <input type="checkbox" checked={selectedKeys.has(row.row_key)} onChange={() => toggleSelected(row.row_key)} />
                  </td>
                  <td className="review-td">
                    <span className={`status-pill ${row.status === 'normal' ? 'status-normal' : row.status === 'flagged' ? 'status-flagged' : 'status-corrected'}`}>
                      {row.status}
                    </span>
                  </td>
                  <td className="review-td font-mono">{row.timestamp || 'NO TIME'}</td>
                  {displayColumns.map(column => (
                    <td key={column} className="review-td font-mono">{row.values[column]}</td>
                  ))}
                </tr>
              ))}
              {!rows.length && (
                <tr>
                  <td className="review-td text-center text-gray-500" colSpan={displayColumns.length + 3}>No records loaded.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {editingRow && (
        <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-5 shadow-2xl">
          <div className="flex items-center justify-between border-b border-gray-800/50 pb-3 mb-4">
            <div>
              <h2 className="text-[0.70rem] font-extrabold text-gray-400 tracking-widest uppercase">CORRECT SELECTED RECORD</h2>
              <div className="font-mono text-[0.65rem] text-gray-500 mt-1">{editingRow.row_key}</div>
            </div>
            <button onClick={saveCorrection} className="action-button"><Save size={14} /> Save Correction</button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 max-h-[480px] overflow-y-auto pr-2">
            {Object.entries(correctionValues).map(([key, value]) => (
              <Control key={key} label={key}>
                <input value={value ?? ''} onChange={event => setCorrectionValues({ ...correctionValues, [key]: event.target.value })} className="control-input" />
              </Control>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Control({ label, children }) {
  return (
    <label className="grid gap-1">
      <span className="text-[0.62rem] font-extrabold text-gray-500 tracking-widest uppercase">{label}</span>
      {children}
    </label>
  );
}

function KPICard({ title, value, unit, color, Icon }) {
  return (
    <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 p-6 flex flex-col relative rounded-lg shadow-xl hover:border-gray-700 transition-colors">
      <div className="absolute top-6 right-6 text-gray-700">
        <Icon size={20} />
      </div>
      <div className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase mb-3">{title}</div>
      <div className={`text-3xl font-black tracking-tight leading-none ${color}`}>{value}</div>
      <div className="text-[0.65rem] font-bold text-gray-600 tracking-wider uppercase mt-3">{unit}</div>
    </div>
  );
}
