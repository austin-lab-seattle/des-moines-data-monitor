import test from 'node:test';
import assert from 'node:assert/strict';
import { SELF_SERVICE_SCOPE, TEAM_LOGIN_SCOPE, canVerifyTeamEmail, teamEmailOperation, teamEmailVerified } from './teamEmailVerification.js';

const authority = 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example';
const user = { access_token: 'CI-access-not-real', id_token: 'CI-id-not-real', scope: `openid ${SELF_SERVICE_SCOPE}`, profile: { email: 'ci@example.org' } };
test('verification state and self-service scope are explicit, not assumed', () => {
  assert.equal(teamEmailVerified(user), false);
  assert.equal(teamEmailVerified({ profile: { email_verified: 'false' } }), false);
  assert.equal(teamEmailVerified({ profile: { email_verified: true } }), true);
  assert.equal(canVerifyTeamEmail(user), true);
  for (const value of [{ ...user, scope: 'openid email' }, { ...user, expired: true }, { ...user, access_token: '' }]) assert.equal(canVerifyTeamEmail(value), false);
  assert.ok(TEAM_LOGIN_SCOPE.split(' ').includes(SELF_SERVICE_SCOPE));
});
test('only own email and access token are sent to Cognito; code is sent in the POST body', async () => {
  const calls = [];
  const fetcher = async (url, options) => { calls.push({ url, options, body: JSON.parse(options.body) }); return { ok: true, json: async () => ({ CodeDeliveryDetails: { AttributeName: 'email', DeliveryMedium: 'EMAIL' } }) }; };
  await teamEmailOperation(authority, user, 'GetUserAttributeVerificationCode', undefined, fetcher);
  await teamEmailOperation(authority, user, 'VerifyUserAttribute', ' 123456 ', fetcher);
  assert.equal(calls[0].url, 'https://cognito-idp.us-west-2.amazonaws.com/');
  assert.deepEqual(calls[0].body, { AccessToken: 'CI-access-not-real', AttributeName: 'email' });
  assert.deepEqual(calls[1].body, { AccessToken: 'CI-access-not-real', AttributeName: 'email', Code: '123456' });
  for (const call of calls) { assert.equal(call.options.method, 'POST'); assert.equal(call.options.credentials, 'omit'); assert.equal(call.options.cache, 'no-store'); }
});
test('invalid scope, provider, operation or empty code never sends a request', async () => {
  const fetcher = () => { throw new Error('must not call fetch'); };
  for (const [provider, session, operation, code] of [
    [authority, { ...user, scope: '' }, 'GetUserAttributeVerificationCode'],
    ['https://example.org/pool', user, 'GetUserAttributeVerificationCode'],
    [authority, user, 'AdminUpdateUserAttributes'],
    [authority, user, 'VerifyUserAttribute', ' '],
  ]) await assert.rejects(teamEmailOperation(provider, session, operation, code, fetcher), error => !error.message.includes('must not call fetch'));
});
test('network failures, malformed success and missing delivery confirmation never claim success', async () => {
  for (const fetcher of [
    async () => { throw new Error('CI-access-not-real 123456'); },
    async () => ({ ok: true, json: async () => { throw new Error('invalid JSON'); } }),
    async () => ({ ok: true, json: async () => null }),
    async () => ({ ok: true, json: async () => ({}) }),
    async () => ({ ok: true, json: async () => ({ CodeDeliveryDetails: { AttributeName: 'phone_number', DeliveryMedium: 'SMS' } }) }),
  ]) await assert.rejects(teamEmailOperation(authority, user, 'GetUserAttributeVerificationCode', undefined, fetcher), error => !/CI-access|123456/.test(error.message));
});
test('wrong, expired, delivery and session errors never echo provider secrets', async () => {
  for (const [type, expected] of [['CodeMismatchException', /incorrect/], ['ExpiredCodeException', /expired/], ['CodeDeliveryFailureException', /could not be delivered/], ['NotAuthorizedException', /Sign in again/], ['TooManyRequestsException', /Too many/], ['Unknown', /could not be completed/]]) {
    await assert.rejects(teamEmailOperation(authority, user, 'VerifyUserAttribute', '123456', async () => ({ ok: false, json: async () => ({ __type: type, message: 'CI-access-not-real 123456' }) })), error => expected.test(error.message) && !/CI-access|123456/.test(error.message));
  }
});
