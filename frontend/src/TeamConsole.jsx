import { useCallback, useEffect, useMemo, useState } from 'react';
import { UserManager, WebStorageStateStore } from 'oidc-client-ts';
import { Activity, Database, Flag, KeyRound, LogIn, LogOut, RefreshCw, ShieldCheck, X } from 'lucide-react';

const AUTHORITY = import.meta.env.VITE_TEAM_AUTHORITY;
const CLIENT_ID = import.meta.env.VITE_TEAM_CLIENT_ID;
const TEAM_API = import.meta.env.VITE_TEAM_API_URL || 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com';
const INSTRUMENTS = ['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'];

function configured() { return Boolean(AUTHORITY && CLIENT_ID); }

export default function TeamConsole() {
  const manager = useMemo(() => configured() ? new UserManager({
    authority: AUTHORITY,
    client_id: CLIENT_ID,
    redirect_uri: `${window.location.origin}/team`,
    post_logout_redirect_uri: `${window.location.origin}/team`,
    response_type: 'code',
    scope: 'openid email profile',
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  }) : null, []);
  const [user, setUser] = useState(null);
  const [tab, setTab] = useState('users');
  const [instrument, setInstrument] = useState('NO2-CAPS');
  const [data, setData] = useState(null);
  const [viewer, setViewer] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);
  const [flagRow, setFlagRow] = useState(null);

  useEffect(() => {
    if (!manager) return;
    const finish = async () => {
      try {
        const params = new URLSearchParams(window.location.search);
        const current = params.has('code') ? await manager.signinRedirectCallback() : await manager.getUser();
        if (params.has('code')) window.history.replaceState({}, '', '/team');
        setUser(current && !current.expired ? current : null);
      } catch { setError('Sign-in could not be completed.'); }
    };
    finish();
  }, [manager]);

  const apiRequest = useCallback(async (path, options = {}) => {
    if (!user?.id_token) throw new Error('Your team session is unavailable. Please sign in again.');
    const response = await fetch(`${TEAM_API}${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${user.id_token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }, [user]);

  const load = useCallback(async () => {
    if (!user) return;
    setLoading(true); setError(''); setNotice('');
    const path = tab === 'users'
      ? '/air-quality/internal/v1/api-users'
      : tab === 'audit'
        ? '/air-quality/internal/v1/audit'
        : `/air-quality/internal/v1/observations?instrument=${encodeURIComponent(instrument)}&limit=50&order=desc`;
    try {
      const payload = await apiRequest(path);
      setData(payload);
      setViewer(payload.viewer || null);
    } catch (e) { setError(e.message); setData(null); }
    finally { setLoading(false); }
  }, [apiRequest, instrument, tab, user]);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const revokeKey = async (key) => {
    const reason = window.prompt(`Why should the key for ${key.email} be revoked?`);
    if (!reason?.trim()) return;
    setLoading(true); setError(''); setNotice('');
    try {
      await apiRequest('/air-quality/internal/v1/api-users/revoke', {
        method: 'POST',
        body: JSON.stringify({ key_id: key.key_id, reason: reason.trim() }),
      });
      setNotice(`Key …${key.key_id.slice(-6)} was revoked.`);
      await load();
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  };

  const submitFlag = async (reason, notes) => {
    setLoading(true); setError(''); setNotice('');
    try {
      await apiRequest('/air-quality/internal/v1/flags', {
        method: 'POST',
        body: JSON.stringify({
          instrument_id: instrument,
          scope: 'selected_rows',
          row_key: flagRow.row_key,
          timestamp: flagRow.timestamp,
          reason,
          notes,
        }),
      });
      setFlagRow(null);
      setNotice('The record was flagged. Its original measurement was not changed.');
      await load();
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  };

  if (!configured()) return <TeamSetup />;
  if (!user) return <TeamLogin onLogin={() => manager.signinRedirect()} error={error} />;

  const keys = data?.keys || [];
  const rows = data?.rows || [];
  const events = data?.events || [];
  const roles = new Set(viewer?.roles || []);
  const canReview = roles.has('Admin') || roles.has('Reviewer');
  const canManageKeys = roles.has('Admin') || roles.has('AccessManager');

  return <div className="team-shell">
    <header className="team-header"><a href="/" className="team-brand"><ShieldCheck /> <span>Des Moines Air<small>Team Console</small></span></a><div><span>{user.profile.email}</span><button onClick={() => manager.signoutRedirect()}><LogOut size={16}/> Sign out</button></div></header>
    <div className="team-layout">
      <aside><p>Private workspace</p>{[['users', KeyRound, 'API users'], ['review', Flag, 'Record review'], ['audit', Activity, 'Audit log']].map(([id, Icon, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => { setTab(id); setData(null); }}><Icon size={17}/>{label}</button>)}</aside>
      <main><div className="team-title"><div><span>UW research team</span><h1>{tab === 'users' ? 'API users' : tab === 'review' ? 'Record review' : 'Audit log'}</h1></div><div className="team-title-actions">{tab === 'review' && <select value={instrument} onChange={(event) => setInstrument(event.target.value)}>{INSTRUMENTS.map(value => <option key={value}>{value}</option>)}</select>}<button onClick={load} disabled={loading}><RefreshCw size={16}/> Refresh</button></div></div>
        {error && <div className="team-message team-error">{error}</div>}
        {notice && <div className="team-message team-success">{notice}</div>}
        {tab === 'users' && <section className="team-card"><div className="team-card-intro"><p>Personal read-only API keys and today&apos;s usage.</p>{data?.limits && <small>{data.limits.requests_per_minute}/minute · {data.limits.requests_per_day}/day · {data.limits.exports_per_day} exports/day</small>}</div><div className="team-table-wrap"><table><thead><tr><th>User</th><th>Key</th><th>Status</th><th>Last used</th><th>Today</th><th>Exports</th><th></th></tr></thead><tbody>{keys.map(key => <tr key={key.key_id}><td><strong>{key.email}</strong><small>{key.organization || '—'}</small></td><td><code>…{key.key_id.slice(-6)}</code></td><td><span className={`team-status ${key.status === 'ACTIVE' ? 'team-status-active' : 'team-status-muted'}`}>{key.status}</span></td><td>{key.last_used_at ? new Date(key.last_used_at).toLocaleString() : 'Never'}</td><td>{key.requests_today || 0}</td><td>{key.exports_today || 0}</td><td>{key.status === 'ACTIVE' && canManageKeys && <button className="team-danger-button" onClick={() => revokeKey(key)}>Revoke</button>}</td></tr>)}{!keys.length && <tr><td colSpan="7" className="team-empty">{loading ? 'Loading…' : 'No keys found.'}</td></tr>}</tbody></table></div></section>}
        {tab === 'review' && <section className="team-card"><div className="team-note"><Database size={17}/> Original measurements remain unchanged; reviews are stored as separate annotations.</div><div className="team-table-wrap"><table><thead><tr><th>Status</th><th>Timestamp</th><th>Record</th><th>Actions</th></tr></thead><tbody>{rows.map(row => <tr key={row.row_key}><td><span className={`team-status team-status-${row.status}`}>{row.status}</span></td><td>{row.timestamp}</td><td><code>{row.row_key}</code></td><td>{canReview ? <button className="team-row-button" disabled={row.status === 'flagged'} onClick={() => setFlagRow(row)}>{row.status === 'flagged' ? 'Flagged' : 'Flag record'}</button> : 'View only'}</td></tr>)}{!rows.length && <tr><td colSpan="4" className="team-empty">{loading ? 'Loading…' : 'No records loaded.'}</td></tr>}</tbody></table></div></section>}
        {tab === 'audit' && <section className="team-card"><div className="team-table-wrap"><table><thead><tr><th>Time</th><th>Team member</th><th>Action</th><th>Target</th><th>Result</th></tr></thead><tbody>{events.map(event => <tr key={event.audit_id}><td>{event.created_at ? new Date(event.created_at).toLocaleString() : '—'}</td><td>{event.actor_email}</td><td>{event.action}</td><td><code>{event.target_id}</code></td><td><span className={`team-status team-status-${event.result?.toLowerCase()}`}>{event.result}</span></td></tr>)}{!events.length && <tr><td colSpan="5" className="team-empty">{loading ? 'Loading…' : 'No audit events.'}</td></tr>}</tbody></table></div></section>}
      </main>
    </div>
    {flagRow && <FlagDialog row={flagRow} busy={loading} onClose={() => setFlagRow(null)} onSubmit={submitFlag}/>}
  </div>;
}

function FlagDialog({ row, busy, onClose, onSubmit }) {
  const [reason, setReason] = useState('');
  const [notes, setNotes] = useState('');
  return <div className="team-dialog-backdrop" role="presentation"><form className="team-dialog" onSubmit={(event) => { event.preventDefault(); onSubmit(reason.trim(), notes.trim()); }}><div className="team-dialog-heading"><div><span>Quality review</span><h2>Flag this record</h2></div><button type="button" onClick={onClose} aria-label="Close"><X size={18}/></button></div><p>The source measurement stays intact. The flag and reviewer identity are added as a separate annotation.</p><code>{row.row_key}</code><label>Reason <textarea required maxLength="500" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Describe the quality concern"/></label><label>Notes (optional) <textarea maxLength="1000" value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Add context for the research team"/></label><div className="team-dialog-actions"><button type="button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !reason.trim()}>Save flag</button></div></form></div>;
}

function TeamLogin({ onLogin, error }) {
  return <div className="team-auth-page">
    <div className="team-auth-glow team-auth-glow-one" />
    <div className="team-auth-glow team-auth-glow-two" />
    <main className="team-auth-shell">
      <section className="team-auth-intro">
        <a className="team-auth-brand" href="/"><span><ShieldCheck size={21}/></span><strong>Des Moines Air</strong></a>
        <div className="team-auth-intro-copy">
          <span className="team-auth-eyebrow">UW research operations</span>
          <h1>One secure place to review data and manage access.</h1>
          <p>Built for the research team to handle quality review and API access without exposing administrative controls on the public dashboard.</p>
        </div>
        <div className="team-auth-capabilities">
          <div><span><KeyRound size={18}/></span><p><strong>API access</strong><small>Track keys, usage and limits</small></p></div>
          <div><span><Flag size={18}/></span><p><strong>Quality review</strong><small>Flag records without changing source data</small></p></div>
          <div><span><Activity size={18}/></span><p><strong>Audit history</strong><small>See who performed each action</small></p></div>
        </div>
        <small className="team-auth-owner">University of Washington · DEOHS</small>
      </section>
      <section className="team-auth-card" aria-labelledby="team-sign-in-title">
        <div className="team-auth-mark"><ShieldCheck size={30}/></div>
        <span className="team-auth-eyebrow">Private workspace</span>
        <h2 id="team-sign-in-title">Team Console</h2>
        <p>Sign in with your invited research-team account. Multi-factor authentication is required.</p>
        {error && <div className="team-message team-error">{error}</div>}
        <button className="team-auth-submit" onClick={onLogin}><LogIn size={18}/> Continue securely</button>
        <div className="team-auth-security"><ShieldCheck size={15}/><span>Invite-only access · MFA protected</span></div>
        <a className="team-auth-back" href="/">Return to public dashboard</a>
      </section>
    </main>
  </div>;
}

function TeamSetup() {
  return <div className="team-auth-page"><main className="team-auth-shell team-auth-shell-setup"><section className="team-auth-card"><div className="team-auth-mark"><ShieldCheck size={30}/></div><span className="team-auth-eyebrow">Private workspace</span><h2>Team Console</h2><p>Team authentication is not configured for this deployment.</p><div className="team-message team-error">No private data or administrative actions are available without a verified team login.</div><a className="team-auth-back" href="/">Return to public dashboard</a></section></main></div>;
}
