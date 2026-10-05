import { useEffect, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import './FlightsLive.css';

const DEFAULT_API_BASE_URL = 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com';
const API_ENTRY_URL = import.meta.env.VITE_API_URL || `${DEFAULT_API_BASE_URL}/air-quality/v1/summary`;
const API_BASE_URL = (() => {
  try {
    return new URL(API_ENTRY_URL).origin;
  } catch {
    return DEFAULT_API_BASE_URL;
  }
})();

const DEFAULT_SITE = {
  name: 'Angle Lake Station (provisional instrument location)',
  latitude: 47.422703,
  longitude: -122.297714,
};
const RADIUS_KM = 8;
const REFRESH_MS = 20000;

function haversineKm(lat1, lon1, lat2, lon2) {
  const radius = 6371.0088;
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dPhi = toRad(lat2 - lat1);
  const dLambda = toRad(lon2 - lon1);
  const a =
    Math.sin(dPhi / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLambda / 2) ** 2;
  return Math.round(2 * radius * Math.asin(Math.min(1, Math.sqrt(a))) * 100) / 100;
}

// Local, clearly labeled placeholder so the map still renders before the backend
// route is deployed or when the API is unreachable. Never shown as real data.
function localSample(lat, lon) {
  const seeds = [
    ['ASA123', 'Alaska Airlines', 0.02, -0.015, 1200, 70],
    ['DAL456', 'Delta Air Lines', -0.03, 0.025, 2400, 250],
    ['SWA789', 'Southwest Airlines', 0.04, 0.03, 3100, 300],
  ];
  const flights = seeds
    .map(([callsign, carrier, dLat, dLon, alt, heading]) => {
      const latitude = lat + dLat;
      const longitude = lon + dLon;
      return {
        icao24: 'sample',
        callsign,
        carrier,
        latitude,
        longitude,
        baro_altitude_m: alt,
        on_ground: false,
        velocity_ms: 180,
        heading_deg: heading,
        distance_km: haversineKm(lat, lon, latitude, longitude),
      };
    })
    .sort((a, b) => a.distance_km - b.distance_km);
  return {
    source: 'sample',
    is_sample: true,
    count: flights.length,
    flights,
    attribution: 'Aircraft data from The OpenSky Network, https://opensky-network.org',
  };
}

const metresToFeet = (m) => (m == null ? null : Math.round(m * 3.28084));
const msToKnots = (v) => (v == null ? null : Math.round(v * 1.94384));

function planeIcon(heading) {
  const rotation = Number.isFinite(heading) ? heading : 0;
  return L.divIcon({
    className: 'flight-marker',
    html: `<div class="flight-plane" style="transform: rotate(${rotation}deg)">✈️</div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}

export default function FlightsLive() {
  const mapRef = useRef(null);
  const mapObj = useRef(null);
  const aircraftLayer = useRef(null);
  const siteLayer = useRef(null);
  const [site, setSite] = useState(DEFAULT_SITE);
  const [data, setData] = useState(null);
  const [note, setNote] = useState('');

  // Load the configurable instrument location once.
  useEffect(() => {
    let cancelled = false;
    fetch('/aircraft/config.json')
      .then((res) => (res.ok ? res.json() : null))
      .then((cfg) => {
        if (!cancelled && cfg && cfg.site && cfg.site.latitude != null) setSite(cfg.site);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Initialise the map once.
  useEffect(() => {
    if (mapObj.current || !mapRef.current) return undefined;
    const map = L.map(mapRef.current).setView([DEFAULT_SITE.latitude, DEFAULT_SITE.longitude], 11);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map);
    siteLayer.current = L.layerGroup().addTo(map);
    aircraftLayer.current = L.layerGroup().addTo(map);
    mapObj.current = map;
    setTimeout(() => map.invalidateSize(), 0);
    return () => {
      map.remove();
      mapObj.current = null;
    };
  }, []);

  // Draw/refresh the instrument marker and ring, and recentre, when the site loads.
  useEffect(() => {
    if (!mapObj.current || !siteLayer.current) return;
    siteLayer.current.clearLayers();
    const ring = L.circle([site.latitude, site.longitude], {
      radius: RADIUS_KM * 1000,
      color: '#22d3ee',
      weight: 1,
      fill: false,
      opacity: 0.35,
    }).addTo(siteLayer.current);
    L.marker([site.latitude, site.longitude], {
      icon: L.divIcon({
        className: 'instrument-marker',
        html: '<div class="instrument-dot"></div>',
        iconSize: [16, 16],
        iconAnchor: [8, 8],
      }),
    })
      .addTo(siteLayer.current)
      .bindPopup(`<b>${site.name}</b><br>Instrument location`);
    mapObj.current.fitBounds(ring.getBounds(), { padding: [20, 20] });
  }, [site]);

  useEffect(() => {
    let active = true;
    const load = async () => {
      const params = new URLSearchParams({
        lat: site.latitude,
        lon: site.longitude,
        radius_km: RADIUS_KM,
      });
      try {
        const res = await fetch(`${API_BASE_URL}/air-quality/v1/flights?${params}`);
        if (!res.ok) throw new Error(`API ${res.status}`);
        const payload = await res.json();
        if (!active) return;
        setData(payload);
        setNote(payload.is_sample ? payload.note || 'Showing labeled sample aircraft.' : '');
      } catch {
        if (!active) return;
        setData(localSample(site.latitude, site.longitude));
        setNote('Live flights endpoint not reachable yet; showing labeled sample aircraft.');
      }
    };
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [site]);

  // Render aircraft markers whenever the data refreshes.
  useEffect(() => {
    if (!aircraftLayer.current || !data) return;
    aircraftLayer.current.clearLayers();
    (data.flights || []).forEach((flight) => {
      const altFt = metresToFeet(flight.baro_altitude_m);
      const knots = msToKnots(flight.velocity_ms);
      L.marker([flight.latitude, flight.longitude], { icon: planeIcon(flight.heading_deg) })
        .addTo(aircraftLayer.current)
        .bindPopup(
          `<b>${flight.carrier || flight.callsign || 'Unknown aircraft'}</b><br>` +
            `${flight.callsign || ''}<br>` +
            `Altitude: ${altFt != null ? `${altFt.toLocaleString()} ft MSL` : 'n/a'}<br>` +
            `Distance: ${flight.distance_km} km<br>` +
            `Speed: ${knots != null ? `${knots} kt` : 'n/a'}`,
        );
    });
  }, [data]);

  const flights = data?.flights || [];
  const isSample = data?.is_sample;

  return (
    <section className="flights-view">
      <div className="flights-header">
        <div>
          <h2>Aircraft overhead</h2>
          <p className="flights-sub">{site.name}</p>
        </div>
        {isSample && <span className="flights-sample-badge">SAMPLE DATA, not real observations</span>}
      </div>

      {note && <div className="flights-note">{note}</div>}

      <div className="flights-body">
        <div ref={mapRef} className="flights-map" />
        <div className="flights-list">
          <div className="flights-list-head">
            {flights.length} aircraft within {RADIUS_KM} km
          </div>
          <table>
            <thead>
              <tr>
                <th>Carrier</th>
                <th>Flight</th>
                <th>Alt (ft)</th>
                <th>Dist (km)</th>
              </tr>
            </thead>
            <tbody>
              {flights.map((flight) => (
                <tr key={`${flight.icao24}-${flight.callsign || ''}`}>
                  <td>{flight.carrier || 'n/a'}</td>
                  <td>{flight.callsign || flight.icao24}</td>
                  <td>{metresToFeet(flight.baro_altitude_m)?.toLocaleString() ?? 'n/a'}</td>
                  <td>{flight.distance_km}</td>
                </tr>
              ))}
              {!flights.length && (
                <tr>
                  <td colSpan={4} className="flights-empty">
                    No aircraft in range right now.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flights-attribution">
        {data?.attribution || 'Aircraft data from The OpenSky Network'} · Map © OpenStreetMap
        contributors · Altitude is above mean sea level, not height above the instrument.
      </div>
    </section>
  );
}
