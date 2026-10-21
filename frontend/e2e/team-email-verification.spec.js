// Offline fixtures only. Intercept every Cognito call; no real emails or codes.
import { test, expect } from '@playwright/test';
const authority = 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_oR7CIzxQG';
const client = '5tno54tskp2tk6lhh6c8jh9ev4';

async function setup(page, { scope = true, sendFails = false, verifyError = '' } = {}) {
  await page.addInitScript(({ authority, client, scope }) => {
    sessionStorage.setItem(`oidc.user:${authority}:${client}`, JSON.stringify({
      id_token: 'CI-id-not-real', access_token: 'CI-access-not-real', token_type: 'Bearer',
      scope: scope ? 'openid email profile aws.cognito.signin.user.admin' : 'openid email profile',
      profile: { sub: 'ci-only', email: 'ci-unverified@example.org', email_verified: false }, expires_at: Date.now() / 1000 + 3600,
    }));
  }, { authority, client, scope });
  const calls = [], privateCalls = [];
  await page.route('https://cognito-idp.us-west-2.amazonaws.com/**', async route => {
    const target = route.request().headers()['x-amz-target'] || '';
    const body = route.request().postDataJSON();
    calls.push({ target, body });
    if (target.endsWith('GetUserAttributeVerificationCode')) return route.fulfill(sendFails
      ? { status: 400, json: { __type: 'CodeDeliveryFailureException' } }
      : { json: { CodeDeliveryDetails: { DeliveryMedium: 'EMAIL', AttributeName: 'email' } } });
    if (target.endsWith('VerifyUserAttribute')) return route.fulfill(verifyError ? { status: 400, json: { __type: verifyError } } : { json: {} });
    throw new Error('Unexpected Cognito request in offline verification test');
  });
  await page.route('**/air-quality/internal/v1/**', route => { privateCalls.push(route.request().url()); return route.fulfill({ status: 403, json: { error: 'Must not call before verified session' } }); });
  return { calls, privateCalls };
}

test('verification is opt-in, own-account only, and requires fresh sign-in before approvals', async ({ page }, testInfo) => {
  const { calls, privateCalls } = await setup(page);
  await page.goto('/team?request_id=00000000-0000-4000-8000-000000000001');
  await expect(page.getByRole('heading', { name: 'Verify your team email' })).toBeVisible();
  await expect(page.getByText('ci-unverified@example.org', { exact: true })).toBeVisible();
  expect(calls).toEqual([]); expect(privateCalls).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('team-email-verification-offline.png'), fullPage: true });
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Send verification code', exact: true }).click();
  await expect(page.getByText('Verification code sent. Check your account email.')).toBeVisible();
  expect(calls).toHaveLength(1);
  expect(calls[0].body).toEqual({ AccessToken: 'CI-access-not-real', AttributeName: 'email' });
  await expect(page.getByRole('button', { name: /Request another code in/ })).toBeDisabled();
  await page.getByLabel('Email verification code', { exact: true }).fill('123456');
  await page.getByRole('button', { name: 'Verify email', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Email verified', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Sign in again', exact: true })).toBeVisible();
  expect(calls[1].body).toEqual({ AccessToken: 'CI-access-not-real', AttributeName: 'email', Code: '123456' });
  expect(privateCalls).toEqual([]);
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
});
for (const [type, message] of [['CodeMismatchException', 'That code is incorrect.'], ['ExpiredCodeException', 'That code has expired.']]) {
  test(`${type} does not unlock approvals or claim success`, async ({ page }) => {
    const { privateCalls } = await setup(page, { verifyError: type });
    await page.goto('/team');
    await page.getByRole('button', { name: 'Send verification code', exact: true }).click();
    await page.getByLabel('Email verification code', { exact: true }).fill('000000');
    await page.getByRole('button', { name: 'Verify email', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText(message);
    await expect(page.getByRole('heading', { name: 'Email verified', exact: true })).toHaveCount(0);
    expect(privateCalls).toEqual([]);
  });
}
test('delivery failure never claims a sent code', async ({ page }) => {
  const { calls } = await setup(page, { sendFails: true });
  await page.goto('/team');
  await page.getByRole('button', { name: 'Send verification code', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('could not be delivered');
  await expect(page.getByLabel('Email verification code', { exact: true })).toHaveCount(0);
  expect(calls).toHaveLength(1);
});
test('old session without the self-service scope sends no email', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { calls, privateCalls } = await setup(page, { scope: false });
  await page.goto('/team');
  await expect(page.getByRole('button', { name: 'Sign in again', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send verification code', exact: true })).toHaveCount(0);
  expect(calls).toEqual([]); expect(privateCalls).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
