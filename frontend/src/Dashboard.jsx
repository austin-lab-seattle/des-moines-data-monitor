import { useCallback, useState, useEffect } from 'react';
import { Activity, AlertTriangle, Check, CheckSquare, Clock, Code2, Copy, Download, DollarSign, Edit3, Lock, MapPin, RefreshCw, Save, Search, ShieldCheck, X } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

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
              {[
                { id: 'overview', label: 'Overview' },
                { id: 'review', label: 'Data Review' },
                { id: 'api', label: 'API' },
              ].map((tab, index) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveView(tab.id)}
                  className={`text-[0.65rem] font-bold tracking-widest uppercase px-3 py-1.5 ${index > 0 ? 'border-l border-gray-800/80' : ''} ${activeView === tab.id ? 'bg-cyan-400 text-black' : 'text-gray-400 hover:text-cyan-300'}`}
                >
                  {tab.label}
                </button>
              ))}
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

        {activeView === 'overview' && (
          <Overview
            kpis={kpis}
            instruments={instruments}
            refreshTime={refreshTime}
            formatBytes={formatBytes}
            formatSeattleTime={formatSeattleTime}
          />
        )}
        {activeView === 'review' && <DataReview />}
        {activeView === 'api' && <ApiSnippets />}
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

      <TimeSeriesChart />

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
                      <div
                        className="text-amber-400/70 text-[0.6rem] tracking-wider uppercase"
                        title="Bronze rows not carried into Silver: duplicates, schema mismatches, and non-approved source files"
                      >
                        -{(instrument.bronzeRows - instrument.silverRows).toLocaleString()} filtered
                      </div>
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
  const [flagMode, setFlagMode] = useState(null);        // 'selected' | 'range' | null
  const [flagReason, setFlagReason] = useState('');
  const [rangeStart, setRangeStart] = useState('');
  const [rangeEnd, setRangeEnd] = useState('');
  const [rangeCount, setRangeCount] = useState(null);
  const [countLoading, setCountLoading] = useState(false);

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

  const fmtLocal = (value) => (value ? value.replace('T', ' ') : '');

  const openFlag = (mode) => {
    setError('');
    setMessage('');
    if (mode === 'selected' && !selectedRows.length) {
      setError('Select at least one row first.');
      return;
    }
    if (mode === 'range') {
      setRangeStart(startTime);
      setRangeEnd(endTime);
    }
    setRangeCount(null);
    setFlagReason('');
    setFlagMode(mode);
  };

  // Preview how many records a time window covers so the range flag is never a
  // blind commit. Re-runs whenever the window or instrument changes.
  useEffect(() => {
    if (flagMode !== 'range' || !rangeStart || !rangeEnd) {
      return;
    }
    let cancelled = false;
    const task = setTimeout(() => {
      if (cancelled) return;
      setCountLoading(true);
      const params = new URLSearchParams({
        instrument,
        start: toIso(rangeStart),
        end: toIso(rangeEnd),
        count_only: 'true',
      });
      fetch(`${API_BASE_URL}/silver-records?${params.toString()}`)
        .then(res => res.json())
        .then(data => { if (!cancelled) setRangeCount(typeof data.count === 'number' ? data.count : null); })
        .catch(() => { if (!cancelled) setRangeCount(null); })
        .finally(() => { if (!cancelled) setCountLoading(false); });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(task);
    };
  }, [flagMode, rangeStart, rangeEnd, instrument]);

  const confirmFlag = async () => {
    try {
      if (flagMode === 'range') {
        if (!rangeStart || !rangeEnd) {
          setError('Pick a start and end time.');
          return;
        }
        await postReview('/record-flags', {
          instrument_id: instrument,
          scope: 'time_range',
          start_time: toIso(rangeStart),
          end_time: toIso(rangeEnd),
          reason: flagReason,
        });
        setMessage(`Flagged ${rangeCount != null ? rangeCount.toLocaleString() : 'all'} records from ${fmtLocal(rangeStart)} to ${fmtLocal(rangeEnd)}.`);
      } else {
        await postReview('/record-flags', {
          instrument_id: instrument,
          scope: 'selected_rows',
          row_keys: selectedRows.map(row => row.row_key),
          reason: flagReason,
        });
        setMessage(`Flagged ${selectedRows.length} selected record${selectedRows.length === 1 ? '' : 's'}.`);
        setSelectedKeys(new Set());
      }
      setFlagMode(null);
      await loadRecords();
    } catch (err) {
      setError(err.message || 'Could not save flag');
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

  const allSelected = rows.length > 0 && rows.every(row => selectedKeys.has(row.row_key));
  const toggleAll = () => setSelectedKeys(allSelected ? new Set() : new Set(rows.map(row => row.row_key)));
  const flagValid = flagMode === 'range'
    ? Boolean(rangeStart && rangeEnd && flagReason.trim())
    : Boolean(selectedRows.length && flagReason.trim());
  const REASON_PRESETS = ['Instrument fault', 'Low flow', 'Calibration', 'Power loss', 'Maintenance', 'Out of range'];

  return (
    <div className="grid gap-5">
      {reviewApiKey ? (
        <div className="auth-banner auth-on"><ShieldCheck size={15} /> Review mode active. Flags and corrections you make here are saved.</div>
      ) : (
        <div className="auth-banner auth-off"><Lock size={15} /> Read-only view. Add <code>?api_key=YOUR_KEY</code> to the dashboard URL to flag or correct records.</div>
      )}

      <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-5 shadow-2xl">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
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
          <div className="flex items-end">
            <button onClick={() => loadRecords()} disabled={loading} className="action-button w-full">
              <Search size={14} />
              {loading ? 'Loading' : 'Load Records'}
            </button>
          </div>
        </div>

        {(message || error) && (
          <div className={`mt-4 flex items-start gap-2 text-xs font-semibold tracking-wide ${error ? 'text-red-400' : 'text-green-400'}`}>
            {error ? <AlertTriangle size={14} className="mt-px shrink-0" /> : <Check size={14} className="mt-px shrink-0" />}
            <span>{error || message}</span>
          </div>
        )}
      </div>

      <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-5 shadow-2xl">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-800/50 pb-3 mb-4">
          <div>
            <h2 className="text-[0.70rem] font-extrabold text-gray-400 tracking-widest uppercase">Silver Record Review</h2>
            <div className="text-[0.65rem] text-gray-500 mt-1">
              {rows.length} loaded{selectedRows.length ? ` · ${selectedRows.length} selected` : ''}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => openFlag('selected')}
              disabled={!reviewApiKey || !selectedRows.length}
              title={!reviewApiKey ? 'Add ?api_key= to enable' : !selectedRows.length ? 'Select rows first' : 'Flag the selected rows'}
              className="action-button"
            >
              <CheckSquare size={14} /> Flag Selected
            </button>
            <button
              onClick={() => openFlag('range')}
              disabled={!reviewApiKey}
              title={!reviewApiKey ? 'Add ?api_key= to enable' : 'Flag every record in a time window'}
              className="action-button"
            >
              <AlertTriangle size={14} /> Flag Range
            </button>
            <button
              onClick={startCorrection}
              disabled={!reviewApiKey || selectedRows.length !== 1}
              title={!reviewApiKey ? 'Add ?api_key= to enable' : selectedRows.length !== 1 ? 'Select exactly one row' : 'Correct this record'}
              className="action-button"
            >
              <Edit3 size={14} /> Correct One
            </button>
            {nextCursor !== null && (
              <button onClick={() => loadRecords(nextCursor)} className="action-button"><RefreshCw size={14} /> Next Page</button>
            )}
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse min-w-[980px]">
            <thead>
              <tr>
                <th className="review-th w-10">
                  <input type="checkbox" checked={allSelected} onChange={toggleAll} disabled={!rows.length} aria-label="Select all rows" />
                </th>
                <th className="review-th">Status</th>
                <th className="review-th">Timestamp</th>
                {displayColumns.map(column => <th key={column} className="review-th">{column}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map(row => {
                const isSelected = selectedKeys.has(row.row_key);
                const flagReasonText = (row.flags || []).map(flag => flag.reason).filter(Boolean).join('; ');
                return (
                  <tr key={row.row_key} className={`transition-colors ${isSelected ? 'bg-cyan-400/5' : 'hover:bg-white/5'}`}>
                    <td className="review-td">
                      <input type="checkbox" checked={isSelected} onChange={() => toggleSelected(row.row_key)} />
                    </td>
                    <td className="review-td">
                      <span
                        title={flagReasonText || undefined}
                        className={`status-pill ${row.status === 'normal' ? 'status-normal' : row.status === 'flagged' ? 'status-flagged' : 'status-corrected'}`}
                      >
                        {row.status}
                      </span>
                    </td>
                    <td className="review-td font-mono">{row.timestamp || 'NO TIME'}</td>
                    {displayColumns.map(column => (
                      <td key={column} className="review-td font-mono">{row.values[column]}</td>
                    ))}
                  </tr>
                );
              })}
              {!rows.length && (
                <tr>
                  <td className="review-td text-center text-gray-500 py-8" colSpan={displayColumns.length + 3}>No records loaded.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {editingRow && (
        <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-5 shadow-2xl">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-800/50 pb-3 mb-4">
            <div>
              <h2 className="text-[0.70rem] font-extrabold text-gray-400 tracking-widest uppercase">Correct Record</h2>
              <div className="font-mono text-[0.65rem] text-gray-500 mt-1">{editingRow.timestamp} &middot; {editingRow.row_key}</div>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => setEditingRow(null)} className="copy-btn">Cancel</button>
              <button onClick={saveCorrection} className="action-button"><Save size={14} /> Save Correction</button>
            </div>
          </div>
          <div className="mb-4 max-w-sm">
            <Control label="Reason">
              <input value={reason} onChange={event => setReason(event.target.value)} placeholder="Why is this being corrected?" className="control-input" />
            </Control>
          </div>
          <div className="text-[0.6rem] text-gray-500 uppercase tracking-widest mb-3">Edit only the fields that need fixing (changed fields are outlined)</div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 max-h-[460px] overflow-y-auto pr-2">
            {Object.entries(correctionValues).map(([key, value]) => {
              const changed = editingRow.values[key] !== value;
              return (
                <Control key={key} label={key}>
                  <input
                    value={value ?? ''}
                    onChange={event => setCorrectionValues({ ...correctionValues, [key]: event.target.value })}
                    className="control-input"
                    style={changed ? { borderColor: '#22d3ee', boxShadow: '0 0 0 1px rgba(34,211,238,0.35)' } : undefined}
                  />
                </Control>
              );
            })}
          </div>
        </div>
      )}

      {flagMode && (
        <Modal
          title={flagMode === 'range' ? 'Flag a time range' : 'Flag selected records'}
          subtitle={instrument}
          onClose={() => setFlagMode(null)}
          footer={(
            <>
              <button onClick={() => setFlagMode(null)} className="copy-btn">Cancel</button>
              <button onClick={confirmFlag} disabled={!flagValid} className="action-button">
                <AlertTriangle size={14} /> {flagMode === 'range' ? 'Flag this range' : `Flag ${selectedRows.length} record${selectedRows.length === 1 ? '' : 's'}`}
              </button>
            </>
          )}
        >
          {flagMode === 'range' ? (
            <div className="grid gap-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <Control label="Start Time">
                  <input type="datetime-local" value={rangeStart} onChange={event => setRangeStart(event.target.value)} className="control-input" />
                </Control>
                <Control label="End Time">
                  <input type="datetime-local" value={rangeEnd} onChange={event => setRangeEnd(event.target.value)} className="control-input" />
                </Control>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className="text-gray-500 uppercase tracking-widest text-[0.6rem]">Impact</span>
                {!rangeStart || !rangeEnd ? (
                  <span className="text-gray-500">Pick a start and end time</span>
                ) : countLoading ? (
                  <span className="text-gray-400">Counting&hellip;</span>
                ) : rangeCount != null ? (
                  <span className="text-amber-300 font-bold">{rangeCount.toLocaleString()} records will be flagged</span>
                ) : (
                  <span className="text-gray-500">Count unavailable</span>
                )}
              </div>
            </div>
          ) : (
            <div className="text-sm text-gray-300">
              <span className="text-amber-300 font-bold">{selectedRows.length}</span> selected record{selectedRows.length === 1 ? '' : 's'} will be flagged.
            </div>
          )}

          <div className="mt-5">
            <div className="text-[0.6rem] text-gray-500 uppercase tracking-widest mb-2">Reason (required)</div>
            <div className="flex flex-wrap gap-2 mb-3">
              {REASON_PRESETS.map(preset => (
                <button
                  key={preset}
                  onClick={() => setFlagReason(preset)}
                  className={`reason-chip ${flagReason === preset ? 'reason-chip-active' : ''}`}
                >
                  {preset}
                </button>
              ))}
            </div>
            <input
              value={flagReason}
              onChange={event => setFlagReason(event.target.value)}
              placeholder="Describe why these records are being flagged"
              className="control-input"
            />
          </div>
        </Modal>
      )}
    </div>
  );
}

function Modal({ title, subtitle, onClose, children, footer }) {
  useEffect(() => {
    const onKey = (event) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={event => event.stopPropagation()}>
        <div className="flex items-start justify-between border-b border-gray-800/60 pb-3 mb-4">
          <div>
            <h3 className="text-[0.72rem] font-extrabold text-gray-200 tracking-widest uppercase">{title}</h3>
            {subtitle && <div className="text-[0.65rem] text-gray-500 mt-1 font-mono">{subtitle}</div>}
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-200 transition-colors"><X size={18} /></button>
        </div>
        {children}
        {footer && <div className="flex justify-end gap-2 mt-5 pt-4 border-t border-gray-800/60">{footer}</div>}
      </div>
    </div>
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
  { value: 'raw', label: 'Raw CSV' },
  { value: 'series', label: 'Time Series' },
  { value: 'records', label: 'Records (JSON)' },
];

const SNIPPET_LANGS = [
  { value: 'r', label: 'R' },
  { value: 'python', label: 'Python' },
  { value: 'curl', label: 'curl' },
];

function buildSnippet({ task, lang, instrument, measurement }) {
  const base = API_BASE_URL;
  const encodedMeasurement = encodeURIComponent(measurement || '');

  if (task === 'raw') {
    if (lang === 'r') {
      return `library(jsonlite)

# 1. Ask the API for a short-lived download link (valid ~5 min)
meta <- fromJSON("${base}/silver-download?instrument=${instrument}")

# 2. Download the full raw silver CSV and read it
download.file(meta$url, "${instrument}_silver.csv", mode = "wb")
df <- read.csv("${instrument}_silver.csv", check.names = FALSE)

nrow(df)`;
    }
    if (lang === 'python') {
      return `import requests
import pandas as pd

# The API returns a short-lived S3 link; pandas reads it directly
url = requests.get(
    "${base}/silver-download",
    params={"instrument": "${instrument}"},
).json()["url"]

df = pd.read_csv(url)
print(df.shape)`;
    }
    return `# 1. Get a short-lived download link
curl "${base}/silver-download?instrument=${instrument}"

# 2. Download using the "url" field from the JSON response
curl -o ${instrument}_silver.csv "PASTE_URL_HERE"`;
  }

  if (task === 'series') {
    if (lang === 'r') {
      return `library(jsonlite)

# Hourly mean of one measurement. res$measurements lists the choices.
res <- fromJSON("${base}/series?instrument=${instrument}&measurement=${encodedMeasurement}")

ts <- res$series          # data.frame: t (hour, UTC), v (hourly mean)
head(ts)`;
    }
    if (lang === 'python') {
      return `import requests
import pandas as pd

res = requests.get("${base}/series", params={
    "instrument": "${instrument}",
    "measurement": "${measurement}",
}).json()

ts = pd.DataFrame(res["series"])   # columns: t (hour, UTC), v (hourly mean)
print(res["measurements"])          # available measurements
ts.head()`;
    }
    return `curl "${base}/series?instrument=${instrument}&measurement=${encodedMeasurement}"`;
  }

  if (lang === 'r') {
    return `library(jsonlite)

# Paginated raw records with flag/correction status (100 per page)
res <- fromJSON("${base}/silver-records?instrument=${instrument}&limit=100")

records <- res$rows        # each row: timestamp, values, status, flags
res$next_cursor            # pass as &cursor= to fetch the next page`;
  }
  if (lang === 'python') {
    return `import requests

res = requests.get("${base}/silver-records", params={
    "instrument": "${instrument}",
    "limit": 100,
}).json()

rows = res["rows"]                 # timestamp, values, status, flags
next_cursor = res["next_cursor"]   # pass as cursor= for the next page`;
  }
  return `curl "${base}/silver-records?instrument=${instrument}&limit=100"`;
}

function ApiSnippets() {
  const [task, setTask] = useState('raw');
  const [lang, setLang] = useState('r');
  const [instrument, setInstrument] = useState('SMPS');
  const [measurement, setMeasurement] = useState('Total Concentration (#/cm³)');
  const [measurements, setMeasurements] = useState([]);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/series?instrument=${encodeURIComponent(instrument)}`)
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
    <div className="grid gap-5">
      <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-6 shadow-2xl">
        <div className="flex items-start gap-3 border-b border-gray-800/50 pb-4 mb-5">
          <Code2 size={20} className="text-cyan-400 mt-0.5 shrink-0" />
          <div>
            <h2 className="text-sm font-black tracking-wide text-white">GET THE DATA IN CODE</h2>
            <p className="text-xs text-gray-500 mt-1 max-w-2xl">Pick a task and your language, then copy the snippet straight into your IDE. It is filled in with the live API URL and your current selection.</p>
          </div>
        </div>

        <div className="flex flex-wrap items-end gap-x-6 gap-y-4 mb-5">
          <div className="grid gap-1.5">
            <span className="text-[0.6rem] font-extrabold text-gray-500 tracking-widest uppercase">Task</span>
            <Segmented options={SNIPPET_TASKS} value={task} onChange={setTask} />
          </div>
          <div className="grid gap-1.5">
            <span className="text-[0.6rem] font-extrabold text-gray-500 tracking-widest uppercase">Language</span>
            <Segmented options={SNIPPET_LANGS} value={lang} onChange={setLang} />
          </div>
          <Control label="Instrument">
            <select value={instrument} onChange={event => setInstrument(event.target.value)} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
          </Control>
          {task === 'series' && (
            <Control label="Measurement">
              <select value={measurement} onChange={event => setMeasurement(event.target.value)} className="control-input min-w-[220px]">
                {(measurements.length ? measurements : [measurement]).map(name => <option key={name} value={name}>{name}</option>)}
              </select>
            </Control>
          )}
        </div>

        <div className="relative">
          <div className="absolute top-3 right-3 z-10">
            <button onClick={copy} className={`copy-btn ${copied ? 'copy-btn-done' : ''}`}>
              {copied ? <><Check size={13} /> Copied</> : <><Copy size={13} /> Copy</>}
            </button>
          </div>
          <pre className="code-block">{code}</pre>
        </div>

        <div className="text-[0.65rem] text-gray-500 mt-4 leading-relaxed">
          <span className="text-gray-400 font-semibold">Base URL:</span> <span className="font-mono break-all">{API_BASE_URL}</span><br />
          Times are UTC. The raw download link expires after about 5 minutes, so request a fresh one each run.
        </div>
      </div>
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
      const res = await fetch(`${API_BASE_URL}/series?${params.toString()}`);
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

  // The raw silver file can be tens of MB, so the API returns a short-lived S3
  // link and the browser downloads every original record straight from S3.
  const downloadRaw = async () => {
    setRawLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/silver-download?instrument=${encodeURIComponent(instrument)}`);
      const payload = await res.json();
      if (res.ok && payload.url) {
        const link = document.createElement('a');
        link.href = payload.url;
        link.download = payload.filename || `${instrument}_silver.csv`;
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
    <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-6 shadow-2xl mb-8">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-gray-800/50 pb-4 mb-5">
        <div className="flex flex-wrap gap-3">
          <Control label="Instrument">
            <select value={instrument} onChange={event => setInstrument(event.target.value)} className="control-input">
              {INSTRUMENT_IDS.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
          </Control>
          <Control label="Measurement">
            <select
              value={measurement}
              onChange={event => { setMeasurement(event.target.value); fetchSeries({ inst: instrument, meas: event.target.value, s: start, e: end }); }}
              className="control-input min-w-[220px]"
            >
              {measurements.map(name => <option key={name} value={name}>{name}</option>)}
            </select>
          </Control>
          <Control label="Start">
            <input type="datetime-local" value={start} onChange={event => setStart(event.target.value)} className="control-input" />
          </Control>
          <Control label="End">
            <input type="datetime-local" value={end} onChange={event => setEnd(event.target.value)} className="control-input" />
          </Control>
          <div className="flex items-end">
            <button onClick={() => fetchSeries({ inst: instrument, meas: measurement, s: start, e: end })} className="action-button">
              <Search size={14} /> Apply
            </button>
          </div>
        </div>
        <div className="flex items-end gap-2">
          <button onClick={downloadCSV} disabled={!series.length} className="action-button" title="Hourly-averaged values shown in the chart">
            <Download size={14} /> Chart CSV
          </button>
          <button onClick={downloadRaw} disabled={rawLoading} className="action-button" title="Every raw silver record for this instrument">
            <Download size={14} /> {rawLoading ? 'Preparing…' : 'Raw CSV'}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="text-gray-600 text-xs py-24 text-center tracking-widest uppercase">Loading&hellip;</div>
      ) : series.length ? (
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={series} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
            <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
            <XAxis dataKey="t" tickFormatter={fmtTick} tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={{ stroke: 'rgba(255,255,255,0.10)' }} minTickGap={48} />
            <YAxis tickFormatter={fmtNumber} tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} axisLine={false} width={64} />
            <Tooltip
              contentStyle={{ background: '#0a0a0a', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
              labelStyle={{ color: '#9ca3af' }}
              itemStyle={{ color: '#22d3ee' }}
              labelFormatter={fmtLabel}
              formatter={value => [fmtNumber(value), measurement]}
            />
            <Line type="monotone" dataKey="v" stroke="#22d3ee" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: '#22d3ee' }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="text-gray-600 text-xs py-24 text-center tracking-widest uppercase">No data for this selection.</div>
      )}

      <div className="text-gray-600 text-[0.6rem] tracking-wider uppercase mt-3">
        {measurement || 'no measurement'} &middot; hourly mean &middot; {series.length} points
      </div>
      {seriesMeta.skippedSchemaMismatch > 0 && (
        <div className="text-amber-300/80 text-[0.65rem] mt-2">
          Ignored {seriesMeta.skippedSchemaMismatch.toLocaleString()} {instrument} rows with mismatched column count for this chart.
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
