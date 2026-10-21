// Offline CI-only auth and requester fixtures. No real tokens, keys or emails.
import { test, expect } from '@playwright/test';

const authority = 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_oR7CIzxQG';
const client = '5tno54tskp2tk6lhh6c8jh9ev4';
const requestId = '00000000-0000-4000-8000-000000000001';
const requester = { request_id: requestId, name: 'CI-only Researcher', email: 'ci-requester@example.org', organization: 'CI University',
  use_case: 'Offline CI-only environmental analysis <script>not code</script>', status: 'PENDING_APPROVAL', actionable: true,
  expires_at: '2099-01-01T00:00:00Z' };

async function setup(page, { loggedIn = true, canApprove = true, failure = false } = {}) {
  if (loggedIn) await page.addInitScript(({ authority, client }) => {
    sessionStorage.setItem(`oidc.user:${authority}:${client}`, JSON.stringify({
      id_token: 'CI-only-not-a-real-token', access_token: 'CI-only-not-a-real-token', token_type: 'Bearer',
      profile: { sub: 'ci-only-subject', email: 'ci-approver@example.org', email_verified: true }, expires_at: Date.now() / 1000 + 3600,
    }));
  }, { authority, client });
  const decisions = [];
  let completed = false;
  await page.route('**/air-quality/internal/v1/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/decision')) {
      decisions.push(route.request().postDataJSON());
      if (failure) return route.fulfill({ status: 503, json: { error: 'CI delivery failed. No usable key issued.' } });
      completed = true;
      return route.fulfill({ json: { message: decisions.at(-1).decision === 'reject' ? 'Request rejected. No key was issued' : 'Read-only key emailed after approval' } });
    }
    expect(route.request().headers().authorization).toBe('Bearer CI-only-not-a-real-token');
    return route.fulfill({ json: { requests: completed ? [{ ...requester, status: 'ACTIVE', actionable: false }] : [requester],
      viewer: { email: 'ci-approver@example.org', roles: ['ApiApprover'], can_approve: canApprove } } });
  });
  return decisions;
}

test('email link requires sign-in and cannot auto-approve on page load', async ({ page }) => {
  const decisions = await setup(page, { loggedIn: false });
  await page.goto(`/team?request_id=${requestId}`);
  await expect(page.getByRole('button', { name: 'Continue securely' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Confirm approval', exact: true })).toHaveCount(0);
  expect(decisions).toEqual([]);
});

test('request details are escaped and approval requires an explicit second confirmation', async ({ page }) => {
  const decisions = await setup(page);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(`/team?request_id=${requestId}`);
  await expect(page.getByText('ci-requester@example.org', { exact: true })).toBeVisible();
  await expect(page.getByText(requester.use_case, { exact: true })).toBeVisible();
  expect(decisions).toEqual([]);
  await expect(page.getByRole('button', { name: 'API users', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Record review', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('ci-requester@example.org');
  expect(decisions).toEqual([]);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(decisions).toEqual([]);
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click();
  await expect(page.getByText('Read-only key emailed after approval', { exact: true })).toBeVisible();
  expect(decisions).toEqual([{ request_id: requestId, decision: 'approve' }]);
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('rejection also requires confirmation and does not request a key', async ({ page }) => {
  const decisions = await setup(page);
  await page.goto(`/team?request_id=${requestId}`);
  await page.getByRole('button', { name: 'Reject', exact: true }).click();
  expect(decisions).toEqual([]);
  await page.getByRole('button', { name: 'Confirm rejection', exact: true }).click();
  await expect(page.getByText('Request rejected. No key was issued', { exact: true })).toBeVisible();
  expect(decisions).toEqual([{ request_id: requestId, decision: 'reject' }]);
});

test('failed approval is not displayed as success and user cannot bypass server permissions', async ({ page }) => {
  const decisions = await setup(page, { failure: true });
  await page.goto(`/team?request_id=${requestId}`);
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click();
  await expect(page.getByText('CI delivery failed. No usable key issued.', { exact: true })).toBeVisible();
  expect(decisions).toHaveLength(1);
  await expect(page.getByText('Read-only key emailed after approval', { exact: true })).toHaveCount(0);
});

test('backend-denied approver has no approval controls and mobile view stays within screen', async ({ page }) => {
  const decisions = await setup(page, { canApprove: false });
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto(`/team?request_id=${requestId}`);
  await expect(page.getByText('Not actionable', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  expect(decisions).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
