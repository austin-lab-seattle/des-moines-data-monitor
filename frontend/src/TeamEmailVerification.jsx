import { useEffect, useRef, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { canVerifyTeamEmail, teamEmailOperation } from './teamEmailVerification.js';

export default function TeamEmailVerification({ authority, user, onSignInAgain, onSignOut }) {
  const [sent, setSent] = useState(false);
  const [verified, setVerified] = useState(false);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  useEffect(() => {
    if (!cooldown) return;
    const timer = setTimeout(() => setCooldown(value => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  const run = async operation => {
    if (inFlight.current || operation === 'GetUserAttributeVerificationCode' && cooldown) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      await teamEmailOperation(authority, user, operation, code);
      if (operation === 'GetUserAttributeVerificationCode') { setSent(true); setCooldown(60); }
      else { setVerified(true); setCode(''); }
    } catch (e) { setError(e.message); }
    finally { inFlight.current = false; setBusy(false); }
  };

  return <div className="team-auth-page"><main className="team-auth-shell team-auth-shell-setup team-email-verification-shell"><section className="team-auth-card" aria-labelledby="team-email-title">
    <div className="team-auth-mark"><ShieldCheck size={30}/></div>
    <span className="team-auth-eyebrow">Your team account</span>
    <h2 id="team-email-title">{verified ? 'Email verified' : 'Verify your team email'}</h2>
    <p className="team-verification-email">{user.profile.email}</p>
    {error && <div role="alert" className="team-message team-error">{error}</div>}
    {verified ? <><p role="status">Your email is verified in Cognito. Sign in again to refresh your secure session. Approval access still depends on your assigned role.</p><button className="team-auth-submit" onClick={onSignInAgain}>Sign in again</button></>
      : !canVerifyTeamEmail(user) ? <><p>Your existing session does not support self-service verification. Sign in again to enable it. No email has been sent.</p><button className="team-auth-submit" onClick={onSignInAgain}>Sign in again</button></>
        : <><p>Verify ownership before reviewing API requests. A code is sent only when you press the button, and only to your account email. This does not approve any request or issue a key.</p>
          <button className="team-auth-submit" disabled={busy || cooldown > 0} onClick={() => run('GetUserAttributeVerificationCode')}>{busy ? 'Please wait…' : cooldown ? `Request another code in ${cooldown}s` : sent ? 'Send another verification code' : 'Send verification code'}</button>
          {sent && <form className="team-email-code" onSubmit={event => { event.preventDefault(); run('VerifyUserAttribute'); }}>
            <p role="status">Verification code sent. Check your account email.</p>
            <label htmlFor="team-email-code">Email verification code</label>
            <input id="team-email-code" autoComplete="one-time-code" inputMode="numeric" maxLength={2048} required value={code} onChange={event => setCode(event.target.value)}/>
            <button className="team-auth-submit" disabled={busy || !code.trim()} type="submit">Verify email</button>
          </form>}</>}
    <button className="team-verification-signout" disabled={busy} onClick={onSignOut}>Sign out</button>
    <a className="team-auth-back" href="/">Return to public dashboard</a>
  </section></main></div>;
}
