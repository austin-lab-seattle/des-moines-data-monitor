import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { UserManager, WebStorageStateStore } from 'oidc-client-ts';
import { Activity, ArrowLeft, ArrowRight, Flag, KeyRound, LockKeyhole, LogOut, RefreshCw, ShieldCheck, UsersRound, Wind, X } from 'lucide-react';
import './AccessApproval.css';
import './TeamLogin.css';
import './TeamConsole.css';
import TeamEmailVerification from './TeamEmailVerification.jsx';
import ApiUsagePanel from './ApiUsagePanel.jsx';
import { TEAM_LOGIN_SCOPE, teamEmailVerified } from './teamEmailVerification.js';

// Public OAuth client metadata, not credentials. Authentication and role checks
// are enforced by API Gateway and the backend, never by these browser values.
const AUTHORITY = import.meta.env.VITE_TEAM_AUTHORITY || 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_oR7CIzxQG';
const CLIENT_ID = import.meta.env.VITE_TEAM_CLIENT_ID || '5tno54tskp2tk6lhh6c8jh9ev4';
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
    scope: TEAM_LOGIN_SCOPE,
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  }) : null, []);
  const [user, setUser] = useState(null);
  const [tab, setTab] = useState('requests');
  const [targetRequest, setTargetRequest] = useState(() => {
    const value = new URLSearchParams(window.location.search).get('request_id');
    return /^[0-9a-f-]{36}$/.test(value || '') ? value : '';
  });
  const [decision, setDecision] = useState(null);
  const [instrument, setInstrument] = useState('NO2-CAPS');
  const [data, setData] = useState(null);
  const [reviewRows, setReviewRows] = useState({});
  const [viewer, setViewer] = useState(null);
  const [error, setError] = useState('');
  const [authDenied, setAuthDenied] = useState(false);
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);
  const [flagRow, setFlagRow] = useState(null);
  const activeLoad = useRef(null);

  useEffect(() => {
    if (!manager) return;
    const finish = async () => {
      try {
        const params = new URLSearchParams(window.location.search);
        const current = params.has('code') ? await manager.signinRedirectCallback() : await manager.getUser();
        if (params.has('code')) {
          const target = current?.state?.request_id;
          const valid = /^[0-9a-f-]{36}$/.test(target || '') ? target : '';
          setTargetRequest(valid);
          window.history.replaceState({}, '', valid ? `/team?request_id=${valid}` : '/team');
        }
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
    if (!response.ok) {
      const failure = new Error(payload.error || `Request failed (${response.status})`);
      failure.status = response.status;
      throw failure;
    }
    return payload;
  }, [user]);

  const load = useCallback(async () => {
    if (!user || !teamEmailVerified(user)) return;
    activeLoad.current?.abort();
    const request = new AbortController();
    activeLoad.current = request;
    const timeout = window.setTimeout(() => request.abort(), 25000);
    setLoading(true); setError(''); setAuthDenied(false); setNotice('');
    const path = tab === 'requests'
      ? `/air-quality/internal/v1/access-requests${targetRequest ? `?request_id=${encodeURIComponent(targetRequest)}` : ''}`
      : tab === 'users'
      ? '/air-quality/internal/v1/api-users'
      : tab === 'audit'
        ? '/air-quality/internal/v1/audit'
        : `/air-quality/internal/v1/observations?instrument=${encodeURIComponent(instrument)}&limit=50&order=desc&view=latest-review`;
    try {
      const payload = await apiRequest(path, { signal: request.signal });
      if (activeLoad.current === request) {
        if (tab === 'review') {
          if (payload.instrument_id !== instrument || !Array.isArray(payload.rows)) {
            throw new Error('The records do not match the selected instrument.');
          }
          setReviewRows(current => ({ ...current, [instrument]: payload.rows }));
        } else {
          setData(payload);
        }
        setViewer(payload.viewer || null);
      }
    } catch (e) {
      if (activeLoad.current === request) {
        const denied = e.status === 401 || e.status === 403;
        setError(e.name === 'AbortError' ? 'The request timed out. Try refreshing.' : e.message);
        setAuthDenied(denied);
        if (tab !== 'review' || denied) setData(null);
        if (denied) {
          setViewer(null);
          setReviewRows({});
        }
      }
    } finally {
      window.clearTimeout(timeout);
      if (activeLoad.current === request) setLoading(false);
    }
  }, [apiRequest, instrument, tab, user, targetRequest]);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => {
      window.clearTimeout(timer);
      const request = activeLoad.current;
      activeLoad.current = null;
      request?.abort();
    };
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

  const decideRequest = async () => {
    if (!decision || loading) return;
    setLoading(true); setError(''); setNotice('');
    try {
      const result = await apiRequest('/air-quality/internal/v1/access-requests/decision', {
        method: 'POST', body: JSON.stringify({ request_id: decision.request.request_id, decision: decision.action }),
      });
      setDecision(null);
      await load();
      setNotice(result.message);
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  };

  if (!configured()) return <TeamSetup />;
  if (!user) return <TeamLogin onLogin={() => manager.signinRedirect({ state: { request_id: targetRequest } })} error={error} />;
  if (!teamEmailVerified(user)) return <TeamEmailVerification authority={AUTHORITY} user={user}
    onSignInAgain={() => manager.signinRedirect({ prompt: 'login', state: { request_id: targetRequest } })}
    onSignOut={() => manager.signoutRedirect()}/>;

  const currentReviewRows = reviewRows[instrument];
  const rows = currentReviewRows || [];
  const events = data?.events || [];
  const requests = data?.requests || [];
  const roles = new Set(viewer?.roles || []);
  const canReview = roles.has('Admin') || roles.has('Reviewer');
  const canManageKeys = roles.has('Admin') || roles.has('AccessManager');
  const canApprove = data?.viewer?.can_approve === true;

  return <div className="team-shell">
    <header className="team-header"><a href="/" className="team-brand"><ShieldCheck /> <span>Des Moines Air<small>Team Console</small></span></a><div><span>{user.profile.email}</span><button onClick={() => manager.signoutRedirect()}><LogOut size={16}/> Sign out</button></div></header>
    <div className="team-layout">
      <nav className="team-nav" aria-label="Team Console">
        <p>Workspace</p>
        {[
          ['requests', KeyRound, 'Access requests', true],
          ['users', UsersRound, 'API users', canManageKeys],
          ['review', Flag, 'Record review', canReview || roles.has('Viewer')],
          ['audit', Activity, 'Audit log', canManageKeys || canReview],
        ].filter(([, , , visible]) => visible).map(([id, Icon, label]) => (
          <button key={id} className={tab === id ? 'active' : ''} aria-current={tab === id ? 'page' : undefined}
            onClick={() => {
              if (tab === id) return;
              setTab(id);
              setData(null);
            }}>
            <Icon size={16} aria-hidden="true"/>{label}
          </button>
        ))}
      </nav>
      <main>
        <div className="team-title">
          <div>
            <h1>{tab === 'requests' ? 'Access requests' : tab === 'users' ? 'API users' : tab === 'review' ? 'Record review' : 'Audit log'}</h1>
          </div>
          <div className="team-title-actions">
            {tab === 'review' && <select aria-label="Instrument" value={instrument} onChange={(event) => setInstrument(event.target.value)}>{INSTRUMENTS.map(value => <option key={value}>{value}</option>)}</select>}
            <button onClick={load} disabled={loading} aria-busy={loading}><RefreshCw size={15} className={loading ? 'refresh-spinner' : ''} aria-hidden="true"/> {loading ? 'Refreshing…' : 'Refresh'}</button>
          </div>
        </div>
        {error && <div role="alert" className="team-message team-error">{error}{tab === 'review' && rows.length > 0 && ' Showing previously loaded records.'}{authDenied && <div className="team-auth-retry"><p>If your roles were recently assigned, refresh your sign-in to obtain current permissions.</p><button onClick={() => manager.signinRedirect({ prompt: 'login', state: { request_id: targetRequest } })}>Refresh sign-in</button></div>}</div>}
        {notice && <div className="team-message team-success">{notice}</div>}
        {tab === 'requests' && !error && <section className="team-card"><div className="team-card-intro"><p>Verified requests awaiting a human decision. Keys are issued only after approval and accepted email delivery.</p>{targetRequest && <button className="team-row-button" onClick={() => { setTargetRequest(''); window.history.replaceState({}, '', '/team'); }}>Show all pending requests</button>}</div>
          <div className="team-table-wrap"><table><thead><tr><th>Requester</th><th>Intended use</th><th>Status</th><th>Expires</th><th>Decision</th></tr></thead><tbody>{requests.map(request => <tr key={request.request_id}>
            <td><strong>{request.name}</strong><small>{request.email}</small><small>{request.organization || 'Not provided'}</small></td>
            <td className="team-request-use">{request.use_case}</td><td>{request.status}</td><td>{new Date(request.expires_at).toLocaleString('en-US', { timeZone: 'America/Los_Angeles', timeZoneName: 'short' })}</td>
            <td>{request.actionable && canApprove ? <div className="team-request-actions"><button disabled={loading} onClick={() => setDecision({ request, action: 'approve' })}>Approve</button><button disabled={loading} onClick={() => setDecision({ request, action: 'reject' })}>Reject</button></div> : request.approved_by ? `Approved by ${request.approved_by}` : request.rejected_by ? `Rejected by ${request.rejected_by}` : 'Not actionable'}</td>
          </tr>)}{!requests.length && <tr><td colSpan="5" className="team-empty">{loading ? 'Loading…' : 'No access requests found.'}</td></tr>}</tbody></table></div></section>}
        {tab === 'users' && !error && <ApiUsagePanel data={data} loading={loading} apiRequest={apiRequest} revokeKey={revokeKey} canManageKeys={canManageKeys} />}
        {tab === 'review' && (!error || currentReviewRows) && <section className="team-card" aria-label="Latest 50 records" aria-busy={loading}>
          <div className="team-table-wrap"><table>
            <thead><tr><th>Status</th><th>Timestamp</th><th>Record</th><th>Actions</th></tr></thead>
            <tbody>{rows.map(row => <tr key={row.row_key}>
              <td><span className={`team-status team-status-${row.status}`}>{row.status}</span></td><td>{row.timestamp}</td><td><code>{row.row_key}</code></td>
              <td>{canReview ? <button className="team-row-button" disabled={loading || Boolean(error) || row.status === 'flagged'} onClick={() => setFlagRow(row)}>{row.status === 'flagged' ? 'Flagged' : 'Flag record'}</button> : 'View only'}</td>
            </tr>)}{!rows.length && <tr><td colSpan="4" className="team-empty">{!currentReviewRows ? 'Loading records…' : 'No records found.'}</td></tr>}</tbody>
          </table></div>
        </section>}
        {tab === 'audit' && <section className="team-card"><div className="team-table-wrap"><table><thead><tr><th>Time</th><th>Team member</th><th>Action</th><th>Target</th><th>Result</th></tr></thead><tbody>{events.map(event => <tr key={event.audit_id}><td>{event.created_at ? new Date(event.created_at).toLocaleString() : 'Unavailable'}</td><td>{event.actor_email}</td><td>{event.action}</td><td><code>{event.target_id}</code></td><td><span className={`team-status team-status-${event.result?.toLowerCase()}`}>{event.result}</span></td></tr>)}{!events.length && <tr><td colSpan="5" className="team-empty">{loading ? 'Loading…' : 'No audit events.'}</td></tr>}</tbody></table></div></section>}
      </main>
    </div>
    {flagRow && <FlagDialog row={flagRow} busy={loading} onClose={() => setFlagRow(null)} onSubmit={submitFlag}/>}
    {decision && <div className="team-dialog-backdrop"><section className="team-dialog" role="dialog" aria-modal="true" aria-labelledby="access-decision-title"><h2 id="access-decision-title">{decision.action === 'approve' ? 'Approve API access?' : 'Reject API access?'}</h2><p>{decision.request.name} ({decision.request.email})</p><p>{decision.action === 'approve' ? 'A personal read-only key will be emailed only to this verified requester. The decision is recorded in the audit log.' : 'No key will be issued. The rejection is recorded in the audit log.'}</p><div className="team-dialog-actions"><button disabled={loading} onClick={() => setDecision(null)}>Cancel</button><button disabled={loading} onClick={decideRequest}>{loading ? 'Processing…' : decision.action === 'approve' ? 'Confirm approval' : 'Confirm rejection'}</button></div></section></div>}
  </div>;
}

function FlagDialog({ row, busy, onClose, onSubmit }) {
  const [reason, setReason] = useState('');
  const [notes, setNotes] = useState('');
  return <div className="team-dialog-backdrop" role="presentation"><form className="team-dialog" onSubmit={(event) => { event.preventDefault(); onSubmit(reason.trim(), notes.trim()); }}><div className="team-dialog-heading"><div><span>Quality review</span><h2>Flag this record</h2></div><button type="button" onClick={onClose} aria-label="Close"><X size={18}/></button></div><p>The source measurement stays intact. The flag and reviewer identity are added as a separate annotation.</p><code>{row.row_key}</code><label>Reason <textarea required maxLength="500" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Describe the quality concern"/></label><label>Notes (optional) <textarea maxLength="1000" value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Add context for the research team"/></label><div className="team-dialog-actions"><button type="button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !reason.trim()}>Save flag</button></div></form></div>;
}

function TeamLogin({ onLogin, error }) {
  const [redirecting, setRedirecting] = useState(false);
  const [redirectError, setRedirectError] = useState('');
  useEffect(() => {
    const restore = event => {
      if (event.persisted) setRedirecting(false);
    };
    window.addEventListener('pageshow', restore);
    return () => window.removeEventListener('pageshow', restore);
  }, []);

  const startSignIn = async () => {
    setRedirecting(true);
    setRedirectError('');
    try {
      await onLogin();
    } catch {
      setRedirectError('Could not open the sign-in page. Please try again.');
      setRedirecting(false);
    }
  };

  return <div className="team-login">
    <header className="team-login-header">
      <a className="team-login-brand" href="/"><Wind size={24} strokeWidth={1.5} aria-hidden="true" />Des Moines Air</a>
      <a className="team-login-back" href="/"><ArrowLeft size={16} strokeWidth={1.5} aria-hidden="true" />Public dashboard</a>
    </header>
    <main className="team-login-main">
      <section className="team-login-entry" aria-labelledby="team-sign-in-title">
        <p className="team-login-kicker"><LockKeyhole size={16} strokeWidth={1.5} aria-hidden="true" />Private research workspace</p>
        <h1 id="team-sign-in-title">Team Console</h1>
        <p className="team-login-intro">Review air quality records, manage researcher API access, and see the team’s audit history.</p>
        <div className="team-login-action">
          {(redirectError || (!redirecting && error)) && <p className="team-login-error" role="alert">{redirectError || error}</p>}
          <button type="button" className="team-login-submit" onClick={startSignIn} disabled={redirecting}>
            {redirecting ? 'Opening sign-in…' : 'Sign in to Team Console'}
            <ArrowRight size={18} strokeWidth={1.5} aria-hidden="true" />
          </button>
          <p className="team-login-note">Use your invited account. Amazon Cognito handles sign-in and requires multi-factor authentication.</p>
        </div>
      </section>
      <figure className="team-login-station">
        <div className="team-login-station-label">
          <span>Field site</span>
          <strong>Des Moines, WA</strong>
        </div>
        <svg viewBox="0 0 480 360" fill="none" aria-hidden="true" focusable="false">
          <defs>
            <pattern id="team-station-grid" width="24" height="24" patternUnits="userSpaceOnUse">
              <path d="M24 0H0V24" stroke="#334960" strokeWidth="1" />
            </pattern>
            <path id="team-station-stream" d="M-100 122C60 106 76 229 218 229S343 100 580 119" />
          </defs>
          <rect width="480" height="360" fill="url(#team-station-grid)" opacity=".7" />
          <g stroke="#87a0b4" opacity=".42">
            <use href="#team-station-stream" y="-88" /><use href="#team-station-stream" y="-64" />
            <use href="#team-station-stream" y="-40" /><use href="#team-station-stream" y="-16" />
            <use href="#team-station-stream" y="32" /><use href="#team-station-stream" y="56" />
            <use href="#team-station-stream" y="80" /><use href="#team-station-stream" y="104" />
          </g>
          <use href="#team-station-stream" y="8" stroke="#7ad8db" strokeWidth="2" />
          <path d="M218 166v142M148 237h140" stroke="#7ad8db" opacity=".45" strokeDasharray="3 5" />
          <circle cx="218" cy="237" r="16" stroke="#7ad8db" />
          <circle cx="218" cy="237" r="5" fill="#7ad8db" />
        </svg>
        <figcaption>Air quality monitoring near Sea-Tac Airport</figcaption>
      </figure>
    </main>
    <footer className="team-login-footer">University of Washington <span aria-hidden="true">/</span> DEOHS <span aria-hidden="true">/</span> Austin Lab</footer>
  </div>;
}

function TeamSetup() {
  return <div className="team-auth-page"><main className="team-auth-shell team-auth-shell-setup"><section className="team-auth-card"><div className="team-auth-mark"><ShieldCheck size={30}/></div><span className="team-auth-eyebrow">Private workspace</span><h2>Team Console</h2><p>Team authentication is not configured for this deployment.</p><div className="team-message team-error">No private data or administrative actions are available without a verified team login.</div><a className="team-auth-back" href="/">Return to public dashboard</a></section></main></div>;
}
