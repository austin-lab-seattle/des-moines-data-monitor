import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Check, Database, Download, MapPin, Plane, Search, Upload, Wind } from 'lucide-react';
import {
  CartesianGrid, ComposedChart, Line, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis,
} from 'recharts';
import {
  buildFlybyEvents, coverageLabel, formatLocalTime, haversineKm, matchEventsToSensor,
  normalizeAircraftRecord, parseAircraftPayload, zonedLocalToUtc,
} from './aircraftAnalytics.js';

const DEFAULT_CONFIG = {
  site: { name: 'Angle Lake Station (provisional instrument location)', latitude: 47.422703, longitude: -122.297714, timezone: 'America/Los_Angeles', provisional: true },
  analysis: { distance_thresholds_km: [1, 3, 5], default_distance_km: 3, default_window_minutes: 10, flight_pass_gap_minutes: 30 },
  sensor_time: { status: 'unverified', source_timezone_assumption: 'America/Los_Angeles', note: 'Instrument timestamp timezone has not been verified.' },
};
const INSTRUMENTS = ['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'];
const CACHE_KEY = 'des-moines-aircraft-import-v1';

function localInputFromUtc(value, timeZone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(value));
  const result = Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
  return `${result.year}-${result.month}-${result.day}T${result.hour}:${result.minute}`;
}

function numberLabel(value, digits = 2) {
  return value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function FlightMap({ site, events, thresholdKm }) {
  const points = events.flatMap(event => event.points);
  const all = [{ latitude: Number(site.latitude), longitude: Number(site.longitude) }, ...points];
  const latitudes = all.map(point => point.latitude);
  const longitudes = all.map(point => point.longitude);
  const latPadding = Math.max((Math.max(...latitudes) - Math.min(...latitudes)) * 0.16, thresholdKm / 111 * 1.2, 0.005);
  const lonPadding = Math.max((Math.max(...longitudes) - Math.min(...longitudes)) * 0.16, thresholdKm / (111 * Math.cos(Number(site.latitude) * Math.PI / 180)) * 1.2, 0.005);
  const minLat = Math.min(...latitudes) - latPadding;
  const maxLat = Math.max(...latitudes) + latPadding;
  const minLon = Math.min(...longitudes) - lonPadding;
  const maxLon = Math.max(...longitudes) + lonPadding;
  const xy = point => ({
    x: 28 + (point.longitude - minLon) / (maxLon - minLon) * 544,
    y: 292 - (point.latitude - minLat) / (maxLat - minLat) * 264,
  });
  const station = xy({ latitude: Number(site.latitude), longitude: Number(site.longitude) });
  const radiusX = thresholdKm / (111 * Math.cos(Number(site.latitude) * Math.PI / 180)) / (maxLon - minLon) * 544;
  const radiusY = thresholdKm / 111 / (maxLat - minLat) * 264;
  const colors = ['#e85d75', '#ef9c36', '#6772e5', '#1c9a78', '#ad5bd3'];

  return (
    <div className="flight-map-wrap">
      <svg className="flight-map" viewBox="0 0 600 320" role="img" aria-label="Aircraft paths relative to the provisional instrument location">
        <defs><pattern id="map-grid" width="30" height="30" patternUnits="userSpaceOnUse"><path d="M 30 0 L 0 0 0 30" fill="none" stroke="var(--chart-grid)" strokeWidth="1" /></pattern></defs>
        <rect width="600" height="320" fill="var(--surface-soft)" /><rect x="0" y="0" width="600" height="320" fill="url(#map-grid)" />
        <ellipse cx={station.x} cy={station.y} rx={radiusX} ry={radiusY} fill="rgba(22,138,173,.08)" stroke="var(--brand)" strokeDasharray="5 4" />
        {events.map((event, index) => {
          const path = event.points.map(point => { const position = xy(point); return `${position.x},${position.y}`; }).join(' ');
          return <g key={event.id}><polyline points={path} fill="none" stroke={colors[index % colors.length]} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />{event.points.map((point, pointIndex) => { const position = xy(point); return <circle key={`${event.id}-${pointIndex}`} cx={position.x} cy={position.y} r="3" fill={colors[index % colors.length]} />; })}</g>;
        })}
        <circle cx={station.x} cy={station.y} r="9" fill="var(--brand)" stroke="white" strokeWidth="3" />
        <text x={Math.min(station.x + 13, 470)} y={station.y - 10} fill="var(--text)" fontSize="12" fontWeight="700">Instrument</text>
        {!events.length && <text x="300" y="165" textAnchor="middle" fill="var(--text-muted)" fontSize="13">No paths within the selected threshold</text>}
      </svg>
      <div className="map-attribution">Relative-coordinate research view • no ground elevation or altitude correction</div>
    </div>
  );
}

export default function AircraftAnalysis() {
  const [cachedImport] = useState(() => {
    try {
      const cached = JSON.parse(window.localStorage.getItem(CACHE_KEY));
      return cached?.observations?.length ? cached : null;
    } catch { return null; }
  });
  const [config, setConfig] = useState(DEFAULT_CONFIG);
  const [site, setSite] = useState(DEFAULT_CONFIG.site);
  const [instrument, setInstrument] = useState('NEPH-PM25');
  const [measurement, setMeasurement] = useState('');
  const [measurements, setMeasurements] = useState([]);
  const [startLocal, setStartLocal] = useState('2026-09-17T12:00');
  const [endLocal, setEndLocal] = useState('2026-09-17T13:00');
  const [thresholdKm, setThresholdKm] = useState(3);
  const [windowMinutes, setWindowMinutes] = useState(10);
  const [aircraftRows, setAircraftRows] = useState(cachedImport?.observations || []);
  const [aircraftMeta, setAircraftMeta] = useState(cachedImport?.metadata || { coverage_status: 'unknown' });
  const [sensorSeries, setSensorSeries] = useState([]);
  const [sensorLoading, setSensorLoading] = useState(false);
  const [message, setMessage] = useState(cachedImport ? `Restored ${cachedImport.observations.length} cached aircraft observations from this browser.` : 'Import historical aircraft data or load the clearly labeled synthetic sample.');
  const [error, setError] = useState('');
  const requestId = useRef(0);

  useEffect(() => {
    fetch('/aircraft/config.json').then(response => response.json()).then(value => {
      setConfig(value);
      setSite(value.site);
      setThresholdKm(value.analysis.default_distance_km);
      setWindowMinutes(value.analysis.default_window_minutes);
    }).catch(() => {});
  }, []);

  const normalizePayload = useCallback((payload, rawText = '') => {
    const normalized = payload.observations.map(record => normalizeAircraftRecord(record, site)).filter(Boolean);
    if (!normalized.length) throw new Error('No valid aircraft rows found. Timestamps must be UTC or include an explicit offset, with latitude and longitude.');
    const metadata = { ...payload.metadata, coverage_status: payload.metadata.coverage_status || 'unknown', imported_at_utc: new Date().toISOString() };
    setAircraftRows(normalized);
    setAircraftMeta(metadata);
    setError('');
    setMessage(`Loaded ${normalized.length} valid aircraft observations; ${payload.observations.length - normalized.length} invalid rows were skipped.`);
    try {
      window.localStorage.setItem(CACHE_KEY, JSON.stringify({ observations: normalized, metadata, raw: rawText.slice(0, 1_500_000) }));
    } catch { setMessage(`Loaded ${normalized.length} observations. Browser cache was full, so use the command-line importer for durable raw preservation.`); }
  }, [site]);

  const loadSample = async () => {
    setError('');
    try {
      const response = await fetch('/aircraft/sample-aircraft.json');
      const text = await response.text();
      const payload = parseAircraftPayload(text, 'sample-aircraft.json');
      normalizePayload(payload, text);
      setSensorSeries(payload.sensorObservations || []);
      setMeasurement('Synthetic PM2.5 (test only)');
      setMeasurements([]);
      setMessage(`Loaded ${payload.observations.length} synthetic aircraft positions and ${payload.sensorObservations.length} synthetic sensor points for an end-to-end UI test.`);
      setStartLocal(localInputFromUtc(payload.metadata.window_start_utc, site.timezone));
      setEndLocal(localInputFromUtc(payload.metadata.window_end_utc, site.timezone));
    } catch (caught) { setError(caught.message || 'Could not load sample data.'); }
  };

  const importFile = async event => {
    const file = event.target.files?.[0];
    if (!file) return;
    setError('');
    try {
      const text = await file.text();
      normalizePayload(parseAircraftPayload(text, file.name), text);
    } catch (caught) { setError(caught.message || 'Could not import aircraft data.'); }
    event.target.value = '';
  };

  const loadSensor = useCallback(async () => {
    const currentId = ++requestId.current;
    setSensorLoading(true);
    setError('');
    try {
      const startUtc = zonedLocalToUtc(startLocal, site.timezone);
      const endUtc = zonedLocalToUtc(endLocal, site.timezone);
      if (Date.parse(endUtc) <= Date.parse(startUtc)) throw new Error('End time must be after start time.');
      if (Date.parse(endUtc) - Date.parse(startUtc) > 86400000) throw new Error('The proof of concept is limited to one day per analysis.');
      const params = new URLSearchParams({ instrument, start: startUtc, end: endUtc, bucket_minutes: '1' });
      if (measurement && measurements.includes(measurement)) params.set('measurement', measurement);
      const response = await fetch(`/air-quality/v1/timeseries?${params}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Sensor API returned ${response.status}`);
      if (currentId === requestId.current) {
        setSensorSeries(result.series || []);
        setMeasurements(result.measurements || []);
        setMeasurement(result.measurement || '');
        setMessage(`Aligned ${(result.series || []).length} one-minute sensor values with aircraft observations in UTC.`);
      }
    } catch (caught) {
      if (currentId === requestId.current) setError(caught.message || 'Could not load sensor observations.');
    } finally { if (currentId === requestId.current) setSensorLoading(false); }
  }, [endLocal, instrument, measurement, measurements, site.timezone, startLocal]);

  const timeBounds = useMemo(() => {
    try { return [Date.parse(zonedLocalToUtc(startLocal, site.timezone)), Date.parse(zonedLocalToUtc(endLocal, site.timezone))]; }
    catch { return [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY]; }
  }, [endLocal, site.timezone, startLocal]);
  const observationsInWindow = useMemo(() => aircraftRows.map(row => ({
    ...row,
    horizontalDistanceKm: haversineKm(site.latitude, site.longitude, row.latitude, row.longitude),
  })).filter(row => {
    const time = Date.parse(row.timestamp);
    return time >= timeBounds[0] && time <= timeBounds[1];
  }), [aircraftRows, site.latitude, site.longitude, timeBounds]);
  const events = useMemo(() => buildFlybyEvents(observationsInWindow, Number(thresholdKm), config.analysis.flight_pass_gap_minutes), [config.analysis.flight_pass_gap_minutes, observationsInWindow, thresholdKm]);
  const matchedEvents = useMemo(() => matchEventsToSensor(events, sensorSeries, Number(windowMinutes)), [events, sensorSeries, windowMinutes]);
  const coverage = coverageLabel(aircraftMeta);
  const timelineData = sensorSeries.map(point => ({ time: Date.parse(point.t), value: Number(point.v) }));
  const flightMarkers = matchedEvents.map(event => ({ time: Date.parse(event.closest.timestamp), marker: 0.5, name: event.callsign || event.aircraftId }));

  return (
    <main className="dashboard-page aircraft-page">
      <section className="location-heading compact-heading">
        <div><div className="location-label"><Plane size={15} /> Exploratory research tool</div><h1>Aircraft activity analysis</h1><p>Compare nearby aircraft positions with one-minute air-quality observations.</p></div>
        <div className={`coverage-badge coverage-${coverage.state}`}>{coverage.label}</div>
      </section>

      <section className="association-banner"><AlertTriangle size={18} /><div><strong>Association, not causation.</strong><span>A temporal overlap cannot establish that an aircraft caused a pollution change. Meteorology, roads, other sources, and flight-data coverage must be evaluated.</span></div></section>

      <section className="surface-card aircraft-controls-card">
        <div className="section-heading"><div><span className="section-eyebrow">One-day proof of concept</span><h2>Analysis settings</h2></div><span className="section-meta">Times shown in {site.timezone}</span></div>
        <div className="aircraft-control-grid">
          <label className="control-field"><span>Start (Pacific local)</span><input className="control-input" type="datetime-local" value={startLocal} onChange={event => setStartLocal(event.target.value)} /></label>
          <label className="control-field"><span>End (Pacific local)</span><input className="control-input" type="datetime-local" value={endLocal} onChange={event => setEndLocal(event.target.value)} /></label>
          <label className="control-field"><span>Instrument</span><select className="control-input" value={instrument} onChange={event => { setInstrument(event.target.value); setMeasurement(''); setMeasurements([]); }}>{INSTRUMENTS.map(value => <option key={value}>{value}</option>)}</select></label>
          <label className="control-field"><span>Measurement</span><select className="control-input" value={measurement} onChange={event => setMeasurement(event.target.value)} disabled={!measurements.length}><option value="">API default</option>{measurements.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
          <label className="control-field"><span>Distance threshold</span><select className="control-input" value={thresholdKm} onChange={event => setThresholdKm(Number(event.target.value))}>{config.analysis.distance_thresholds_km.map(value => <option key={value} value={value}>{value} km</option>)}</select></label>
          <label className="control-field"><span>Flyby window (± minutes)</span><input className="control-input" type="number" min="1" max="60" value={windowMinutes} onChange={event => setWindowMinutes(event.target.value)} /></label>
          <label className="control-field"><span>Instrument latitude</span><input className="control-input" type="number" step="0.000001" value={site.latitude} onChange={event => setSite(current => ({ ...current, latitude: Number(event.target.value) }))} /></label>
          <label className="control-field"><span>Instrument longitude</span><input className="control-input" type="number" step="0.000001" value={site.longitude} onChange={event => setSite(current => ({ ...current, longitude: Number(event.target.value) }))} /></label>
        </div>
        <div className="aircraft-actions">
          <button className="action-button" type="button" onClick={loadSensor} disabled={sensorLoading}><Search size={15} />{sensorLoading ? 'Aligning…' : 'Run analysis'}</button>
          <label className="action-button action-secondary aircraft-upload"><Upload size={15} />Import aircraft CSV/JSON<input type="file" accept=".csv,.json,text/csv,application/json" onChange={importFile} /></label>
          <button className="action-button action-secondary" type="button" onClick={loadSample}><Database size={15} />Load synthetic sample</button>
          <a className="action-button action-secondary" href="/aircraft/sample-aircraft.json" download><Download size={15} />Sample schema</a>
        </div>
        {(message || error) && <div className={`filter-message ${error ? 'filter-message-error' : 'filter-message-success'}`}>{error ? <AlertTriangle size={14} /> : <Check size={14} />}<span>{error || message}</span></div>}
        <div className="assumption-grid">
          <div><strong><MapPin size={14} /> Provisional site</strong><span>{site.name}: {Number(site.latitude).toFixed(6)}, {Number(site.longitude).toFixed(6)}. Replace these settings when the instrument coordinates are surveyed.</span></div>
          <div><strong><AlertTriangle size={14} /> Sensor clock assumption</strong><span>{config.sensor_time.note}</span></div>
          <div><strong><Wind size={14} /> Wind unavailable</strong><span>The current sensor API has no aligned wind speed or direction. No wind-based attribution is made.</span></div>
        </div>
      </section>

      <section className="aircraft-results-grid">
        <article className="surface-card aircraft-chart-card">
          <div className="section-heading"><div><span className="section-eyebrow">UTC-aligned timeline</span><h2>{measurement || instrument} and closest approaches</h2></div><span className="section-meta">{matchedEvents.length} flyby event{matchedEvents.length === 1 ? '' : 's'}</span></div>
          {timelineData.length ? <div className="aircraft-timeline"><ResponsiveContainer width="100%" height={320}><ComposedChart margin={{ top: 10, right: 18, bottom: 8, left: 0 }} data={timelineData}><CartesianGrid vertical={false} stroke="var(--chart-grid)" /><XAxis type="number" dataKey="time" domain={['dataMin', 'dataMax']} tickFormatter={value => formatLocalTime(value, site.timezone).replace(/:\d{2} [AP]M/, match => match.slice(0, -3))} tick={{ fill: 'var(--text-muted)', fontSize: 10 }} /><YAxis yAxisId="sensor" tick={{ fill: 'var(--text-muted)', fontSize: 11 }} width={60} /><YAxis yAxisId="events" hide domain={[0, 1]} /><Tooltip labelFormatter={value => formatLocalTime(value, site.timezone)} formatter={(value, name) => name === 'marker' ? ['Nearby aircraft', 'Event'] : [numberLabel(value), measurement || instrument]} contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10 }} /><Line yAxisId="sensor" type="monotone" dataKey="value" stroke="var(--brand)" strokeWidth={2.5} dot={false} isAnimationActive={false} /><Scatter yAxisId="events" data={flightMarkers} dataKey="marker" fill="#e85d75" shape="triangle" /></ComposedChart></ResponsiveContainer></div> : <div className="chart-empty">Run the analysis to load one-minute sensor readings.</div>}
          <div className="chart-caption"><span>Sensor values: one-minute means</span><span>Triangles: closest approach</span><span>Display: Pacific local; alignment: UTC</span></div>
        </article>
        <article className="surface-card aircraft-map-card"><div className="section-heading"><div><span className="section-eyebrow">Horizontal proximity</span><h2>Flight paths near the instrument</h2></div><span className="section-meta">≤ {thresholdKm} km</span></div><FlightMap site={site} events={events} thresholdKm={Number(thresholdKm)} /></article>
      </section>

      <section className="surface-card aircraft-table-card">
        <div className="section-heading"><div><span className="section-eyebrow">Matched events</span><h2>Closest approaches and sensor response</h2></div><span className="section-meta">Exploratory thresholds: 1, 3, and 5 km</span></div>
        <div className="metric-definition">Baseline is the median one-minute sensor value from −{windowMinutes} to −{Number(windowMinutes) / 2} minutes before closest approach. Peak is the maximum from −{Number(windowMinutes) / 2} to +{windowMinutes} minutes. Peak change = peak − baseline. Missing values mean that the selected windows lack sensor readings.</div>
        <div className="table-scroll"><table className="data-table aircraft-event-table"><thead><tr><th>Aircraft</th><th>Closest approach</th><th>Min horizontal distance</th><th>Reported altitude</th><th>Sensor peak</th><th>Baseline</th><th>Peak change</th></tr></thead><tbody>{matchedEvents.map(event => <tr key={event.id}><td><strong>{event.callsign || event.aircraftId}</strong><small>{event.aircraftType || 'Type unavailable'} • {event.aircraftId}</small></td><td>{formatLocalTime(event.closest.timestamp, site.timezone)}</td><td>{numberLabel(event.closest.horizontalDistanceKm)} km</td><td>{event.closest.altitudeM == null ? 'Unavailable' : `${numberLabel(event.closest.altitudeM, 0)} m`}<small>{event.closest.altitudeReference}; not height above instrument</small></td><td>{numberLabel(event.sensorPeak)}</td><td>{numberLabel(event.baseline)}<small>{event.baselinePointCount} point{event.baselinePointCount === 1 ? '' : 's'}</small></td><td className={event.peakChange > 0 ? 'positive-change' : ''}>{numberLabel(event.peakChange)}</td></tr>)}{!matchedEvents.length && <tr><td className="empty-cell" colSpan="7">No matched flybys. With {coverage.state} coverage, this {coverage.state === 'complete' ? 'means no qualifying positions were present' : 'does not confirm that no aircraft were present'}.</td></tr>}</tbody></table></div>
      </section>
    </main>
  );
}
