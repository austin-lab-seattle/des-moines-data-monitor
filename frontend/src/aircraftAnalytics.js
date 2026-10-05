const EARTH_RADIUS_KM = 6371.0088;
const COVERAGE_STATES = new Set(['complete', 'partial', 'unknown', 'synthetic']);

export function haversineKm(lat1, lon1, lat2, lon2) {
  const values = [lat1, lon1, lat2, lon2].map(Number);
  if (!values.every(Number.isFinite)) return null;
  const [aLat, aLon, bLat, bLon] = values.map(value => value * Math.PI / 180);
  const dLat = bLat - aLat;
  const dLon = bLon - aLon;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(aLat) * Math.cos(bLat) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

function partsAt(date, timeZone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  return Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, Number(part.value)]));
}

function offsetAt(date, timeZone) {
  const part = partsAt(date, timeZone);
  return Date.UTC(part.year, part.month - 1, part.day, part.hour, part.minute, part.second) - date.getTime();
}

export function zonedLocalToUtc(value, timeZone = 'America/Los_Angeles') {
  const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!match) throw new Error('Use a complete local date and time.');
  const [, year, month, day, hour, minute, second = '00'] = match;
  const wallClockUtc = Date.UTC(+year, +month - 1, +day, +hour, +minute, +second);
  let candidate = new Date(wallClockUtc);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    candidate = new Date(wallClockUtc - offsetAt(candidate, timeZone));
  }
  const actual = partsAt(candidate, timeZone);
  if ([actual.year, actual.month, actual.day, actual.hour, actual.minute, actual.second]
    .join('-') !== [+year, +month, +day, +hour, +minute, +second].join('-')) {
    throw new Error(`That local time does not exist in ${timeZone} because of a daylight-saving transition.`);
  }
  return candidate.toISOString();
}

export function formatLocalTime(value, timeZone = 'America/Los_Angeles') {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'Invalid time';
  return new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', second: '2-digit',
    timeZoneName: 'short',
  }).format(date);
}

function pick(record, aliases) {
  const key = Object.keys(record).find(candidate => aliases.includes(candidate.toLowerCase().trim()));
  return key == null ? undefined : record[key];
}

function finite(value) {
  if (value == null || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizeTimestamp(value) {
  if (typeof value === 'number' || /^\d{10}(?:\.\d+)?$/.test(String(value || '').trim())) {
    const parsed = new Date(Number(value) * 1000);
    return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : null;
  }
  const text = String(value || '').trim();
  // Aircraft data must carry an explicit offset. A naive timestamp is unsafe.
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) return null;
  const parsed = new Date(text);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : null;
}

export function normalizeAircraftRecord(record, site) {
  const timestamp = normalizeTimestamp(pick(record, ['timestamp', 'time', 'time_position', 'lastcontact', 'datetime', 'utc']));
  const latitude = finite(pick(record, ['latitude', 'lat']));
  const longitude = finite(pick(record, ['longitude', 'lon', 'lng']));
  if (!timestamp || latitude == null || longitude == null) return null;

  const geometric = finite(pick(record, ['geoaltitude', 'geometric_altitude_m', 'geometric altitude (m)']));
  const barometric = finite(pick(record, ['baroaltitude', 'barometric_altitude_m', 'barometric altitude (m)', 'altitude_m']));
  const genericAltitude = finite(pick(record, ['altitude', 'alt']));
  const altitudeM = geometric ?? barometric ?? genericAltitude;
  const explicitReference = pick(record, ['altitude_reference', 'altitude reference']);
  const altitudeReference = String(explicitReference || (geometric != null ? 'geometric (reported)' : barometric != null ? 'barometric (reported)' : altitudeM != null ? 'reported; reference unknown' : 'unavailable'));
  const identifier = String(pick(record, ['aircraft_id', 'icao24', 'hex', 'callsign', 'flight', 'id']) || '').trim() || 'UNKNOWN';
  const callsign = String(pick(record, ['callsign', 'flight']) || '').trim();
  const aircraftType = String(pick(record, ['aircraft_type', 'typecode', 'type', 'aircraft type']) || '').trim();
  return {
    aircraftId: identifier,
    callsign,
    timestamp,
    latitude,
    longitude,
    altitudeM,
    altitudeReference,
    aircraftType,
    horizontalDistanceKm: haversineKm(site.latitude, site.longitude, latitude, longitude),
  };
}

export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '"' && quoted && text[index + 1] === '"') { field += '"'; index += 1; }
    else if (char === '"') quoted = !quoted;
    else if (char === ',' && !quoted) { row.push(field); field = ''; }
    else if ((char === '\n' || char === '\r') && !quoted) {
      if (char === '\r' && text[index + 1] === '\n') index += 1;
      row.push(field); field = '';
      if (row.some(value => value.trim())) rows.push(row);
      row = [];
    } else field += char;
  }
  if (field || row.length) { row.push(field); if (row.some(value => value.trim())) rows.push(row); }
  if (rows.length < 2) return [];
  const headers = rows[0].map(header => header.trim());
  return rows.slice(1).map(values => Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ''])));
}

export function parseAircraftPayload(text, filename = '') {
  const trimmed = String(text || '').trim();
  if (!trimmed) throw new Error('The aircraft file is empty.');
  if (filename.toLowerCase().endsWith('.json') || trimmed.startsWith('{') || trimmed.startsWith('[')) {
    const payload = JSON.parse(trimmed);
    const observations = Array.isArray(payload) ? payload : payload.observations;
    if (!Array.isArray(observations)) throw new Error('JSON must be an array or contain an observations array.');
    return {
      observations,
      metadata: Array.isArray(payload) ? {} : (payload.metadata || {}),
      sensorObservations: Array.isArray(payload) ? [] : (payload.sensor_observations || []),
    };
  }
  return { observations: parseCsv(trimmed), metadata: {} };
}

export function buildFlybyEvents(observations, thresholdKm, gapMinutes = 30) {
  const located = observations
    .filter(item => item.horizontalDistanceKm != null)
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  const byAircraft = new Map();
  located.forEach(item => {
    const key = item.aircraftId;
    const passes = byAircraft.get(key) || [];
    const lastPass = passes.at(-1);
    const lastPoint = lastPass?.at(-1);
    if (!lastPoint || Date.parse(item.timestamp) - Date.parse(lastPoint.timestamp) > gapMinutes * 60000) passes.push([item]);
    else lastPass.push(item);
    byAircraft.set(key, passes);
  });
  return [...byAircraft.values()].flat().map(points => {
    const closest = points.reduce((best, point) => point.horizontalDistanceKm < best.horizontalDistanceKm ? point : best);
    return { id: `${closest.aircraftId}:${closest.timestamp}`, aircraftId: closest.aircraftId, callsign: closest.callsign, aircraftType: closest.aircraftType, closest, points };
  }).filter(event => event.closest.horizontalDistanceKm <= thresholdKm)
    .sort((a, b) => Date.parse(a.closest.timestamp) - Date.parse(b.closest.timestamp));
}

export function median(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function matchEventsToSensor(events, sensorSeries, windowMinutes = 10) {
  const windowMs = Number(windowMinutes) * 60000;
  return events.map(event => {
    const center = Date.parse(event.closest.timestamp);
    const baselineValues = [];
    const peakCandidates = [];
    sensorSeries.forEach(point => {
      const time = Date.parse(point.t);
      const value = Number(point.v);
      if (!Number.isFinite(time) || !Number.isFinite(value)) return;
      if (time >= center - windowMs && time < center - windowMs / 2) baselineValues.push(value);
      if (time >= center - windowMs / 2 && time <= center + windowMs) peakCandidates.push({ time, value });
    });
    const baseline = median(baselineValues);
    const peakPoint = peakCandidates.reduce((best, point) => !best || point.value > best.value ? point : best, null);
    return {
      ...event,
      baseline,
      sensorPeak: peakPoint?.value ?? null,
      sensorPeakTime: peakPoint ? new Date(peakPoint.time).toISOString() : null,
      peakChange: baseline != null && peakPoint ? peakPoint.value - baseline : null,
      baselinePointCount: baselineValues.length,
      peakPointCount: peakCandidates.length,
    };
  });
}

export function coverageLabel(metadata = {}) {
  const state = COVERAGE_STATES.has(metadata.coverage_status) ? metadata.coverage_status : 'unknown';
  const labels = {
    complete: 'Provider reports complete coverage for the requested window',
    partial: 'Partial flight coverage — missing records are possible',
    unknown: 'Coverage unknown — zero matches does not confirm no aircraft',
    synthetic: 'Synthetic sample — not a real aircraft observation',
  };
  return { state, label: labels[state] };
}
