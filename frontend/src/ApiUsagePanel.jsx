import { useRef, useState } from 'react';
import { ArrowUpRight, ChevronRight, Search, X } from 'lucide-react';
import './ApiUsagePanel.css';

const number = value => value == null ? 'Not recorded' : value.toLocaleString('en-US');
const pacific = 'America/Los_Angeles';
const dayFormat = new Intl.DateTimeFormat('en-US', { timeZone: pacific, month: 'short', day: 'numeric', year: 'numeric' });
const timeFormat = new Intl.DateTimeFormat('en-US', { timeZone: pacific, hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
const usageDayFormat = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' });
const bytes = value => value == null ? 'Not recorded' : `${(value / (1024 * 1024)).toLocaleString('en-US', { maximumFractionDigits: 2 })} MiB`;

function UsageSummary({ users, usageDay, loading }) {
  const tracked = users.filter(user => user.usage?.lifetime != null);
  const sum = (period, field) => tracked.length
    ? tracked.reduce((total, user) => total + (user.usage[period]?.[field] ?? 0), 0)
    : null;
  const day = Date.parse(usageDay);
  return <div className="usage-summary">
    <section className="team-card usage-summary-card">
      <div className="usage-summary-heading">
        <h2>Today</h2>
        <span>{Number.isFinite(day) ? `${usageDayFormat.format(day)} · Pacific time` : 'Pacific time'}</span>
      </div>
      <dl>
        <div><dt>API calls</dt><dd className="usage-primary">{loading ? 'Loading' : number(sum('today', 'requests'))}</dd></div>
        <div><dt>CSV exports</dt><dd>{loading ? 'Loading' : number(sum('today', 'exports_prepared'))}</dd></div>
      </dl>
    </section>
    <section className="team-card usage-summary-card">
      <div className="usage-summary-heading">
        <h2>Recorded to date</h2>
        <span>{loading ? null : `${number(tracked.length)} of ${number(users.length)} users tracked`}</span>
      </div>
      <dl>
        <div><dt>API calls</dt><dd>{loading ? 'Loading' : number(sum('lifetime', 'requests'))}</dd></div>
        <div><dt>CSV exports</dt><dd>{loading ? 'Loading' : number(sum('lifetime', 'exports_prepared'))}</dd></div>
        <div><dt>CSV prepared</dt><dd>{loading ? 'Loading' : bytes(sum('lifetime', 'export_bytes'))}</dd></div>
      </dl>
    </section>
  </div>;
}

export default function ApiUsagePanel({ data, loading, apiRequest, revokeKey, canManageKeys }) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(null);
  const [days, setDays] = useState(30);
  const [history, setHistory] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [error, setError] = useState('');
  const requestId = useRef(0);
  const historyDialog = useRef(null);

  const loadHistory = async (user, period) => {
    const current = ++requestId.current;
    setSelected(user);
    setDays(period);
    setHistoryLoading(true);
    setError('');
    setHistory(null);
    try {
      const params = new URLSearchParams({ user_id: user.user_id, days: period });
      const payload = await apiRequest(`/air-quality/internal/v1/api-users?${params}`);
      if (current === requestId.current) setHistory(payload.history);
    } catch (failure) {
      if (current === requestId.current) setError(failure.message || 'Usage history could not be loaded.');
    } finally {
      if (current === requestId.current) setHistoryLoading(false);
    }
  };

  const users = data?.users || [];
  const term = query.trim().toLowerCase();
  const visibleUsers = users
    .filter(user => [user.email || 'Legacy key user', user.organization].some(value => value?.toLowerCase().includes(term)))
    .sort((left, right) => (Date.parse(right.last_used_at) || 0) - (Date.parse(left.last_used_at) || 0));
  const keys = data?.keys || [];
  const dailyRows = [...(history?.rows || [])].reverse();

  return <div className="usage-panel" aria-busy={loading}>
    <UsageSummary users={users} usageDay={data?.usage_day} loading={!data} />
    <section className="team-card usage-directory" aria-labelledby="usage-directory-title">
      <div className="usage-directory-header">
        <h2 id="usage-directory-title">Usage by user</h2>
        <label className="usage-search">
          <Search size={16} aria-hidden="true"/>
          <input type="search" aria-label="Search by email or organization" placeholder="Search users" value={query} onChange={event => setQuery(event.target.value)}/>
        </label>
      </div>
      <div className="team-table-wrap" role="region" aria-label="API user usage table" tabIndex={0}>
        <table className="usage-table" role="table">
          <thead role="rowgroup"><tr role="row">
            <th scope="col">User</th>
            <th scope="col" aria-sort="descending">Last used<small>Pacific time</small></th>
            <th scope="col" className="usage-numeric">API calls<small>Today / recorded</small></th>
            <th scope="col" className="usage-numeric">CSV exports<small>Today / recorded</small></th>
            <th scope="col" className="usage-numeric">CSV prepared<small>Recorded</small></th>
            <th scope="col"><span className="sr-only">Details</span></th>
          </tr></thead>
          <tbody role="rowgroup">{visibleUsers.map(user => {
            const life = user.usage?.lifetime;
            const today = user.usage?.today;
            const last = Date.parse(user.last_used_at);
            return <tr key={user.user_id} role="row">
              <th scope="row" role="rowheader" className="usage-user">
                <strong>{user.email || 'Legacy key user'}</strong>
                {user.organization && <small>{user.organization}</small>}
                <small>{number(user.active_keys)} active · {number(user.revoked_keys)} revoked</small>
              </th>
              <td role="cell" className="usage-timestamp"><span className="usage-mobile-label" aria-hidden="true">Last used · Pacific</span>{Number.isFinite(last) ? <time dateTime={user.last_used_at}>
                {dayFormat.format(last)}<small>{timeFormat.format(last)}</small>
              </time> : <span className="usage-muted">No use recorded</span>}</td>
              <td role="cell" className="usage-numeric">
                <span className="usage-mobile-label" aria-hidden="true">API calls · Today / recorded</span>
                {life ? <><UsagePair today={today?.requests ?? 0} total={life.requests}/>{life.tracking_since && <small>Since {dayFormat.format(new Date(life.tracking_since))}</small>}</> : <span className="usage-muted">Not recorded</span>}
              </td>
              <td role="cell" className="usage-numeric">
                <span className="usage-mobile-label" aria-hidden="true">CSV exports · Today / recorded</span>
                {life ? <UsagePair today={today?.exports_prepared ?? 0} total={life.exports_prepared}/> : <span className="usage-muted">Not recorded</span>}
              </td>
              <td role="cell" className="usage-numeric">
                <span className="usage-mobile-label" aria-hidden="true">CSV prepared</span>
                <span className={life?.export_bytes == null ? 'usage-muted' : undefined}>{bytes(life?.export_bytes)}</span>
              </td>
              <td role="cell" className="usage-action"><button className="team-row-button" aria-haspopup="dialog" onClick={() => { historyDialog.current.showModal(); loadHistory(user, days); }}>Usage history <ArrowUpRight size={14} aria-hidden="true"/></button></td>
            </tr>;
          })}{!visibleUsers.length && <tr><td colSpan="6" className="team-empty">{!data ? 'Loading usage…' : term ? 'No users match your search.' : 'No API users found.'}</td></tr>}</tbody>
        </table>
      </div>
      <div className="usage-directory-footer">
        <span role="status">{data ? (term ? `${number(visibleUsers.length)} of ${number(users.length)} users` : `${number(users.length)} ${users.length === 1 ? 'user' : 'users'}`) : ''}</span>
      </div>
    </section>

    <dialog className="team-card usage-history" aria-labelledby="usage-history-title" ref={historyDialog} onClose={() => {
      requestId.current += 1;
      setSelected(null);
    }}>
      <div className="usage-history-header">
        <h2 id="usage-history-title">Daily usage{selected?.email ? ` for ${selected.email}` : ''}</h2>
        <button className="team-row-button usage-close" aria-label="Close usage history" onClick={() => historyDialog.current.close()}><X size={18}/></button>
      </div>
      <div className="usage-history-tools">
        <select aria-label="Usage history period" value={days} onChange={event => loadHistory(selected, Number(event.target.value))}>
          <option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
        </select>
      </div>
      {error ? <div className="team-message team-error" role="alert">{error}</div> : historyLoading ? <p className="usage-history-loading" role="status">Loading usage history…</p> : <div className="team-table-wrap" role="region" aria-label="Daily usage table" tabIndex={0}>
        <table className="usage-table"><thead><tr><th scope="col">Day<small>Pacific time</small></th><th scope="col">Calls</th><th scope="col">Successful</th><th scope="col">Errors</th><th scope="col">CSV exports</th><th scope="col">Export rows</th><th scope="col">CSV size</th></tr></thead>
          <tbody>{dailyRows.map(row => <tr key={row.day}>
            <td>{row.day}{row.partial_day ? ' (partial)' : ''}</td>
            {row.requests == null ? <td colSpan="6" className="usage-unrecorded">Not recorded</td> : <>
              <td>{number(row.requests)}</td>
              <td>{number(row.successful_requests)}</td>
              <td>{number(row.failed_requests)}</td>
              <td>{number(row.exports_prepared)}</td>
              <td>{number(row.export_rows)}</td>
              <td>{bytes(row.export_bytes)}</td>
            </>}
          </tr>)}
            {!dailyRows.length && <tr><td className="team-empty" colSpan="7">No daily usage available.</td></tr>}
          </tbody>
        </table>
      </div>}
    </dialog>

    <section className="team-card usage-keys">
      <details>
        <summary className="usage-keys-heading"><ChevronRight size={16} aria-hidden="true"/> Keys and UTC quota counters <span>{data ? number(keys.length) : null}</span></summary>
        <div className="team-table-wrap" role="region" aria-label="API keys and quota table" tabIndex={0}>
          <table className="usage-table">
            <thead><tr><th scope="col">User</th><th scope="col">Key</th><th scope="col">Status</th><th scope="col">Read attempts today</th><th scope="col">Export attempts today</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{keys.map(key => <tr key={key.key_id}><td>{key.email}</td><td><code>…{key.key_id.slice(-6)}</code></td><td>{key.status}</td><td>{number(key.requests_today)}</td><td>{number(key.exports_today)}</td><td>{key.status === 'ACTIVE' && canManageKeys && <button className="team-danger-button" onClick={() => revokeKey(key)}>Revoke</button>}</td></tr>)}
              {!keys.length && <tr><td className="team-empty" colSpan="6">{data ? 'No API keys found.' : 'Loading keys…'}</td></tr>}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  </div>;
}

function UsagePair({ today, total }) {
  return <span className="usage-pair"><strong>{number(today)}</strong><span className="usage-muted"> / {number(total)}</span></span>;
}
