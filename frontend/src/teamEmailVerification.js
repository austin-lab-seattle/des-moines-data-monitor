// Cognito self-service calls use the signed-in user's access token, never an
// AWS operator credential or a researcher API key. No arbitrary email is sent.
export const SELF_SERVICE_SCOPE = 'aws.cognito.signin.user.admin';
export const TEAM_LOGIN_SCOPE = `openid email profile ${SELF_SERVICE_SCOPE}`;

export function teamEmailVerified(user) {
  return user?.profile?.email_verified === true || user?.profile?.email_verified === 'true';
}

export function canVerifyTeamEmail(user) {
  return Boolean(user?.access_token && !user.expired && user.scope?.split(/\s+/).includes(SELF_SERVICE_SCOPE));
}

export async function teamEmailOperation(authority, user, operation, code, fetcher = fetch) {
  if (!canVerifyTeamEmail(user)) throw new Error('Sign in again to enable email verification for your own account.');
  const match = /^https:\/\/cognito-idp\.([a-z0-9-]+)\.amazonaws\.com\/[a-z0-9-]+_[A-Za-z0-9]+\/?$/.exec(authority);
  if (!match) throw new Error('The team identity provider is not supported for email verification.');
  if (!['GetUserAttributeVerificationCode', 'VerifyUserAttribute'].includes(operation)) throw new Error('Unsupported email verification operation.');
  const body = { AccessToken: user.access_token, AttributeName: 'email' };
  if (operation === 'VerifyUserAttribute') {
    if (typeof code !== 'string' || !code.trim() || code.trim().length > 2048) throw new Error('Enter the verification code from your email.');
    body.Code = code.trim();
  }
  let response;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    response = await fetcher(`https://cognito-idp.${match[1]}.amazonaws.com/`, {
      method: 'POST', cache: 'no-store', credentials: 'omit', signal: controller.signal,
      headers: { 'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': `AWSCognitoIdentityProviderService.${operation}` },
      body: JSON.stringify(body),
    });
  } catch { throw new Error('Email verification is unavailable. Check your connection and try again.'); }
  finally { clearTimeout(timeout); }
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error('The verification response could not be confirmed. Try again later.'); }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('The verification response could not be confirmed. Try again later.');
  if (!response.ok) {
    const type = String(payload.__type || '').split('#').at(-1);
    const messages = {
      CodeMismatchException: 'That code is incorrect. Check your email and try again.',
      ExpiredCodeException: 'That code has expired. Request a new verification code.',
      LimitExceededException: 'Too many attempts. Wait before trying again.',
      TooManyRequestsException: 'Too many attempts. Wait before trying again.',
      NotAuthorizedException: 'Your verification session is unavailable. Sign in again.',
      CodeDeliveryFailureException: 'The verification email could not be delivered. Try again later.',
    };
    // Never echo provider messages, which may contain submitted secrets.
    throw new Error(messages[type] || 'Email verification could not be completed. Try again later.');
  }
  if (operation === 'GetUserAttributeVerificationCode' && (payload.CodeDeliveryDetails?.DeliveryMedium !== 'EMAIL' || payload.CodeDeliveryDetails?.AttributeName !== 'email')) {
    throw new Error('Email code delivery could not be confirmed. Try again later.');
  }
  return payload;
}
