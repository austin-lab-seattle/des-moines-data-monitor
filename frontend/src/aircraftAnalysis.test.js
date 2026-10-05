import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildFlybyEvents, coverageLabel, haversineKm, matchEventsToSensor,
  normalizeAircraftRecord, zonedLocalToUtc,
} from './aircraftAnalytics.js';

const site = { latitude: 47.422703, longitude: -122.297714 };

test('haversine distance is horizontal great-circle distance', () => {
  assert.ok(Math.abs(haversineKm(0, 0, 0, 1) - 111.195) < 0.01);
  assert.equal(haversineKm(47, -122, 'bad', -122), null);
});

test('Pacific local timestamps convert to UTC with daylight-saving rules', () => {
  assert.equal(zonedLocalToUtc('2026-07-15T12:00'), '2026-07-15T19:00:00.000Z');
  assert.equal(zonedLocalToUtc('2026-01-15T12:00'), '2026-01-15T20:00:00.000Z');
  assert.throws(() => zonedLocalToUtc('2026-03-08T02:30'), /does not exist/);
});

test('aircraft timestamps require an explicit UTC offset', () => {
  assert.equal(normalizeAircraftRecord({ timestamp: '2026-07-15 12:00', lat: 47.42, lon: -122.3 }, site), null);
  const row = normalizeAircraftRecord({ time: 1784122800, icao24: 'abc123', lat: 47.42, lon: -122.3, baroaltitude: 900 }, site);
  assert.equal(row.aircraftId, 'abc123');
  assert.equal(row.altitudeReference, 'barometric (reported)');
});

test('flybys split by aircraft and event metrics use documented windows', () => {
  const observations = [
    { aircraftId: 'A', timestamp: '2026-07-15T19:00:00Z', horizontalDistanceKm: 2, altitudeM: 800 },
    { aircraftId: 'A', timestamp: '2026-07-15T19:01:00Z', horizontalDistanceKm: 1, altitudeM: 700 },
    { aircraftId: 'B', timestamp: '2026-07-15T19:02:00Z', horizontalDistanceKm: 8, altitudeM: 900 },
  ];
  const events = buildFlybyEvents(observations, 3);
  assert.equal(events.length, 1);
  assert.equal(events[0].closest.timestamp, '2026-07-15T19:01:00Z');
  const sensor = [
    { t: '2026-07-15T18:51:00Z', v: 10 },
    { t: '2026-07-15T18:54:00Z', v: 14 },
    { t: '2026-07-15T18:58:00Z', v: 18 },
    { t: '2026-07-15T19:04:00Z', v: 25 },
  ];
  const [matched] = matchEventsToSensor(events, sensor, 10);
  assert.equal(matched.baseline, 12);
  assert.equal(matched.sensorPeak, 25);
  assert.equal(matched.peakChange, 13);
});

test('unknown coverage is not described as confirmed aircraft absence', () => {
  assert.match(coverageLabel({}).label, /does not confirm no aircraft/);
  assert.match(coverageLabel({ coverage_status: 'synthetic' }).label, /not a real/);
});
