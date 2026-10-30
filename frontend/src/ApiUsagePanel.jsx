import { useRef, useState } from 'react';

const number = value => value == null ? 'Not recorded' : value.toLocaleString('en-US');
const date = value => value && Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
}).format(new Date(value)) : 'Never';
const bytes = value => value == null ? 'Not recorded' : `${(value / (1024 * 1024)).toLocaleString('en-US', { maximumFractionDigits: 2 })} MB`;

export default function ApiUsagePanel({ data, loading, apiRequest, revokeKey, canManageKeys }) {
  const [selected, setSelected] = useState('');
  const [days, setDays] = useState(30);
  const [history, setHistory] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [error, setError] = useState('');
  const requestId = useRef(0);
  const loadHistory = async (userId, period = days) => {
    const current = ++requestId.current;
    setSelected(userId); setDays(period); setHistoryLoading(true); setError(''); setHistory(null);
    try {
      const payload = await apiRequest(`/air-quality/internal/v1/api-users?${new URLSearchParams({ user_id: userId, days: period })}`);
      if (current === requestId.current) setHistory(payload.history);
    } catch (failure) {
      if (current === requestId.current) setError(failure.message || 'Usage history could not be loaded.');
    } finally { if (current === requestId.current) setHistoryLoading(false); }
  };
  const users = data?.users || [];
  return <>
    <section className="team-card">
      <div className="team-card-intro usage-intro"><p>Personal API-key usage, combined across each user&apos;s active and revoked keys.</p>
        <small>Usage days and last-used times are Pacific. Lifetime means recorded usage since tracking began, including API error responses. Earlier expired quota counters cannot reconstruct older history.</small>
        <small>Exports count CSV links prepared, not completed downloads. Anonymous dashboard traffic is not attributed to individual users.</small>
      </div>
      <div className="team-table-wrap"><table><thead><tr><th>User</th><th>Last used<br/>Pacific time</th><th>Calls today</th><th>Recorded lifetime</th><th>Exports<br/>Today / lifetime</th><th>CSV prepared<br/>Lifetime</th><th></th></tr></thead>
        <tbody>{users.map(user => {
          const life = user.usage?.lifetime;
          const today = user.usage?.today;
          return <tr key={user.user_id}><td><strong>{user.email || 'Legacy key user'}</strong><small>{user.organization || 'Organization not provided'}</small><small>{user.active_keys} active, {user.revoked_keys} revoked keys</small></td>
            <td>{date(user.last_used_at)}</td><td>{number(life ? today?.requests || 0 : null)}</td>
            <td><strong>{number(life?.requests)}</strong><small>{life?.tracking_since ? `Since ${date(life.tracking_since)}` : 'Tracking starts with the next authenticated request'}</small></td>
            <td>{number(life ? today?.exports_prepared || 0 : null)} / {number(life?.exports_prepared)}</td><td>{bytes(life?.export_bytes)}</td>
            <td><button className="team-row-button" onClick={() => loadHistory(user.user_id)} disabled={historyLoading}>Usage history</button></td></tr>;
        })}{!users.length && <tr><td colSpan="7" className="team-empty">{loading ? 'Loading usage…' : 'No API users found.'}</td></tr>}</tbody></table></div>
    </section>
    {selected && <section className="team-card usage-history"><div className="team-card-intro"><h2>Daily usage{history?.email ? ` for ${history.email}` : ''}</h2>
      <label>Period <select aria-label="Usage history period" value={days} onChange={event => loadHistory(selected, Number(event.target.value))}><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></select></label>
      <small>Pacific calendar days. The first tracked day can be partial. Days before tracking began remain unknown.</small></div>
      {error ? <div className="team-message team-error" role="alert">{error}</div> : historyLoading ? <p role="status">Loading usage history…</p> : <div className="team-table-wrap"><table><thead><tr><th>Day</th><th>Calls</th><th>Successful</th><th>Errors</th><th>Exports prepared</th><th>Export rows</th><th>CSV size</th></tr></thead><tbody>{(history?.rows || []).map(row => <tr key={row.day}><td>{row.day}{row.partial_day ? ' (partial)' : ''}</td><td>{number(row.requests)}</td><td>{number(row.successful_requests)}</td><td>{number(row.failed_requests)}</td><td>{number(row.exports_prepared)}</td><td>{number(row.export_rows)}</td><td>{bytes(row.export_bytes)}</td></tr>)}</tbody></table></div>}
    </section>}
    <section className="team-card usage-history"><details><summary className="team-card-intro">Keys and UTC quota counters</summary><div className="team-card-intro"><small>Quota counters reset at UTC midnight. Rejected authentication and rate-limited requests are excluded from recorded usage totals.</small></div>
      <div className="team-table-wrap"><table><thead><tr><th>User</th><th>Key</th><th>Status</th><th>Read attempts today UTC</th><th>Export attempts today UTC</th><th></th></tr></thead><tbody>{(data?.keys || []).map(key => <tr key={key.key_id}><td>{key.email}</td><td><code>…{key.key_id.slice(-6)}</code></td><td>{key.status}</td><td>{number(key.requests_today)}</td><td>{number(key.exports_today)}</td><td>{key.status === 'ACTIVE' && canManageKeys && <button className="team-danger-button" onClick={() => revokeKey(key)}>Revoke</button>}</td></tr>)}</tbody></table></div>
    </details></section>
  </>;
}
