import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, RefreshCw, ShieldCheck } from 'lucide-react';
import { startVisiblePolling } from './visiblePolling.js';

const ROOT = '/air-quality/internal/v1/research';
const pending = job => ['PENDING', 'PROCESSING'].includes(job.state);
const stateName = state => ({ PENDING: 'Queued', PROCESSING: 'Preparing', READY: 'Ready',
  FAILED: 'Failed', EXPIRED: 'Expired', OUTDATED: 'Review changed' }[state] || 'Unavailable');
const dateLabel = stamp => new Date(stamp).toLocaleString('en-US', {
  timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
});

export default function ResearchData({ apiRequest, onSignInAgain }) {
  const [access, setAccess] = useState(null);
  const [instrument, setInstrument] = useState('NO2-CAPS');
  const [includeFlagged, setIncludeFlagged] = useState(false);
  const [range, setRange] = useState('all');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(true);
  const [downloading, setDownloading] = useState('');
  const active = useRef(null);
  const jobsRef = useRef(jobs);
  useEffect(() => { jobsRef.current = jobs; }, [jobs]);

  const refresh = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    setChecking(true);
    try {
      const nextAccess = await apiRequest(`${ROOT}/access`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setAccess(nextAccess);
      if (!nextAccess.enabled) { setJobs([]); return; }
      const result = await apiRequest(`${ROOT}/exports`, { signal: controller.signal });
      const next = result.exports || [];
      // The jobs list uses an eventually consistent index. Newly submitted
      // jobs are checked directly until the index includes them.
      for (const job of jobsRef.current.filter(pending)) {
        if (!next.some(item => item.job_id === job.job_id)) {
          next.push(await apiRequest(`${ROOT}/exports/${job.job_id}`, { signal: controller.signal }));
        }
      }
      if (!controller.signal.aborted) {
        setJobs(next.sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20));
        setError('');
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(failure.message);
        if ([401, 403].includes(failure.status)) { setAccess(null); setJobs([]); }
      }
    } finally {
      clearTimeout(timeout);
      if (active.current === controller) setChecking(false);
    }
  }, [apiRequest]);

  useEffect(() => {
    const initial = setTimeout(refresh, 0);
    return () => { clearTimeout(initial); active.current?.abort(); };
  }, [refresh]);

  const hasPending = jobs.some(pending);
  useEffect(() => {
    if (!hasPending || !access?.enabled) return;
    return startVisiblePolling(refresh, 3000);
  }, [access?.enabled, hasPending, refresh]);

  const prepare = async (event) => {
    event.preventDefault();
    if (range === 'custom' && (!start || !end || new Date(start) > new Date(end))) {
      setError('Choose a start and end time in the correct order.');
      return;
    }
    const body = { instrument, include_flagged: includeFlagged };
    if (range === 'week') {
      const now = Date.now();
      body.start = new Date(now - 7 * 86400000).toISOString();
      body.end = new Date(now).toISOString();
    } else if (range === 'custom') {
      body.start = new Date(start).toISOString(); body.end = new Date(end).toISOString();
    }
    setBusy(true); setError('');
    try {
      const job = await apiRequest(`${ROOT}/exports`, { method: 'POST', body: JSON.stringify(body) });
      setJobs(current => [job, ...current.filter(item => item.job_id !== job.job_id)].slice(0, 20));
    } catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  };

  const download = async job => {
    setDownloading(job.job_id); setError('');
    try {
      const result = await apiRequest(`${ROOT}/exports/${job.job_id}/download`);
      if (result.state !== 'READY' || !result.url) {
        setJobs(current => current.map(item => item.job_id === job.job_id ? result : item));
        setError(result.message || 'Prepare a new export to download these readings.');
        return;
      }
      const url = new URL(result.url);
      if (url.protocol !== 'https:') throw new Error('The download link is invalid.');
      const link = document.createElement('a');
      link.href = url.href; link.download = result.filename; link.rel = 'noopener noreferrer';
      document.body.appendChild(link); link.click(); link.remove();
    } catch (failure) { setError(failure.message); }
    finally { setDownloading(''); }
  };

  return <div className="research-data">
    <section className="team-card research-access" aria-label="Research access">
      <div><ShieldCheck size={20} aria-hidden="true"/><strong>{!access ? checking ? 'Checking access…' : 'Access could not be checked'
        : access.enabled ? 'Full-history access enabled' : 'Full-history access not enabled'}</strong></div>
      <button className="team-row-button" onClick={refresh} disabled={checking}><RefreshCw size={15} aria-hidden="true"/> Check access</button>
      {access && !access.enabled && <p>Ask <a href={`mailto:${access.contact_email}`}>{access.contact_email}</a> to enable research access for your team account.</p>}
    </section>
    {error && <div className="team-message team-error" role="alert">{error}{!access && <button className="team-row-button" onClick={onSignInAgain}>Sign in again</button>}</div>}
    {access?.enabled && <>
      <form className="team-card research-form" onSubmit={prepare}>
        <p>Download original-resolution measurements and their review annotations.</p>
        <div className="research-controls">
          <label>Instrument<select value={instrument} onChange={event => setInstrument(event.target.value)}>
            {access.instruments.map(id => <option key={id}>{id}</option>)}
          </select></label>
          <label>Range<select value={range} onChange={event => setRange(event.target.value)}>
            <option value="all">Full history</option><option value="week">Last 7 days</option><option value="custom">Custom dates</option>
          </select></label>
          {range === 'custom' && <>
            <label>Start (local time)<input type="datetime-local" required value={start} onChange={event => setStart(event.target.value)}/></label>
            <label>End (local time)<input type="datetime-local" required value={end} onChange={event => setEnd(event.target.value)}/></label>
          </>}
        </div>
        <label className="research-flag-option"><input type="checkbox" checked={includeFlagged} onChange={event => setIncludeFlagged(event.target.checked)}/>
          <span>Include flagged records<small>{includeFlagged ? 'Flagged records and their review reasons will be included.' : 'Flagged records will be excluded.'}</small></span>
        </label>
        <button className="team-row-button" type="submit" disabled={busy}><Download size={16} aria-hidden="true"/>{busy ? 'Submitting…' : 'Prepare CSV (.gz)'}</button>
      </form>
      <section className="team-card" aria-label="Research exports">
        <div className="research-export-heading"><h2>Your exports</h2><button className="team-row-button" onClick={refresh} disabled={checking}>Refresh exports</button></div>
        <div className="team-table-wrap"><table><thead><tr><th>Dataset</th><th>Flagged records</th><th>Status</th><th>Rows</th><th>Created (Pacific)</th><th>Download</th></tr></thead>
          <tbody>{jobs.map(job => <tr key={job.job_id}>
            <td><strong>{job.spec.instrument}</strong><small>{job.spec.start || job.spec.end ? 'Selected dates' : 'Full history'}</small></td>
            <td>{job.spec.include_flagged ? 'Included' : 'Excluded'}</td><td>{stateName(job.state)}</td>
            <td>{job.rows != null ? Number(job.rows).toLocaleString('en-US') : 'Pending'}</td>
            <td>{dateLabel(job.created_at)}</td><td>{job.state === 'READY' && <button className="team-row-button" onClick={() => download(job)} disabled={downloading === job.job_id}>
              <Download size={15} aria-hidden="true"/>{downloading === job.job_id ? 'Opening…' : 'Download CSV'}
            </button>}</td>
          </tr>)}{!jobs.length && <tr><td colSpan="6" className="team-empty">No research exports yet.</td></tr>}</tbody>
        </table></div>
      </section>
    </>}
  </div>;
}
