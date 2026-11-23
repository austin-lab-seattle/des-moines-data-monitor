import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { Buffer } from 'node:buffer';
import { readFile } from 'node:fs/promises';
import { gunzipSync } from 'node:zlib';

const contract = JSON.parse(execFileSync('python3', ['../tests/research_export_scenarios.py', '--contract'], { encoding: 'utf8' }));
const authority = 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_oR7CIzxQG';
const client = '5tno54tskp2tk6lhh6c8jh9ev4';

async function setup(page, { enabled = true, failAccess = false, revokeDownload = false, delayedList = false } = {}) {
  await page.addInitScript(({ authority, client }) => {
    sessionStorage.setItem(`oidc.user:${authority}:${client}`, JSON.stringify({
      id_token: 'CI-only-team-token', access_token: 'CI-only-access-token', token_type: 'Bearer',
      profile: { sub: 'researcher-one', email: 'researcher-one@example.org', email_verified: true },
      expires_at: Date.now() / 1000 + 3600,
    }));
  }, { authority, client });
  const submissions = [];
  const jobs = new Map();
  await page.route('https://private.invalid/**', route => {
    expect(route.request().headers().authorization).toBeUndefined();
    const job = [...jobs.values()].find(item => item.download.url === route.request().url());
    return route.fulfill({ body: Buffer.from(job.gzip_base64, 'base64'), contentType: 'application/gzip',
      headers: { 'Content-Disposition': `attachment; filename="${job.ready.filename}"` } });
  });
  await page.route('**/air-quality/internal/v1/**', route => {
    const url = new URL(route.request().url());
    expect(route.request().headers().authorization).toBe('Bearer CI-only-team-token');
    if (url.pathname.endsWith('/research/access')) return route.fulfill(failAccess
      ? { status: 503, json: { error: 'Research access temporarily unavailable.' } }
      : { json: enabled ? contract.access : contract.denied_access });
    if (url.pathname.endsWith('/research/exports') && route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); submissions.push(body);
      if (!enabled) return route.fulfill({ status: 403, json: { error: 'Access not enabled.' } });
      const item = body.include_flagged ? contract.included : contract.excluded;
      jobs.set(item.ready.job_id, item);
      return route.fulfill({ status: 202, json: item.pending });
    }
    if (url.pathname.endsWith('/research/exports')) return route.fulfill({ json: {
      exports: delayedList ? [] : [...jobs.values()].map(item => item.ready),
    } });
    const parts = url.pathname.split('/');
    const download = parts.at(-1) === 'download';
    const id = parts.at(download ? -2 : -1);
    const item = jobs.get(id);
    if (item) return route.fulfill(download && revokeDownload
      ? { status: 403, json: { error: 'Full-history access has been revoked.' } }
      : { json: download ? item.download : item.ready });
    return route.fulfill({ json: { requests: [], viewer: contract.access.viewer } });
  });
  await page.goto('/team?view=research');
  return { submissions, jobs };
}

test('approved team member can export full history with precise flagged annotations using an explicit toggle', async ({ page }) => {
  expect(contract.synthetic_ci_only).toBe(true);
  const { submissions } = await setup(page);
  await expect(page.getByRole('heading', { name: 'Research data', exact: true })).toBeVisible();
  await expect(page.getByText('Full-history access enabled', { exact: true })).toBeVisible();
  const include = page.getByRole('checkbox', { name: 'Include flagged records' });
  await expect(include).not.toBeChecked();
  await page.getByRole('button', { name: 'Prepare CSV (.gz)', exact: true }).click();
  const first = page.getByRole('row').filter({ hasText: 'Excluded' });
  await expect(first).toContainText('Ready', { timeout: 10000 });
  expect(submissions[0]).toEqual({ instrument: 'NO2-CAPS', include_flagged: false });
  const firstDownload = page.waitForEvent('download');
  await first.getByRole('button', { name: 'Download CSV', exact: true }).click();
  const file = await firstDownload;
  const csv = gunzipSync(await readFile(await file.path())).toString();
  expect(csv).not.toContain('-1.234567891');
  expect(csv).toContain('2.987654321');
  await include.check();
  await page.getByRole('button', { name: 'Prepare CSV (.gz)', exact: true }).click();
  const second = page.getByRole('row').filter({ hasText: 'Included' });
  await expect(second).toContainText('Ready', { timeout: 10000 });
  const secondDownload = page.waitForEvent('download');
  await second.getByRole('button', { name: 'Download CSV', exact: true }).click();
  const included = gunzipSync(await readFile(await (await secondDownload).path())).toString();
  expect(included).toContain('-1.234567891');
  expect(included).toContain('CI-only quality concern');
  expect(included).toContain('_research_timestamp_utc');
});

test('a signed-in viewer sees access status without receiving research export controls', async ({ page }) => {
  const { submissions } = await setup(page, { enabled: false });
  await expect(page.getByText('Full-history access not enabled', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Prepare CSV (.gz)', exact: true })).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: 'Include flagged records' })).toHaveCount(0);
  expect(submissions).toEqual([]);
});

test('access failure is not represented as an approval or a denied membership', async ({ page }) => {
  await setup(page, { failAccess: true });
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
  await expect(page.getByText('Full-history access enabled', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Full-history access not enabled', { exact: true })).toHaveCount(0);
});

test('revocation prevents downloading even when an old session still shows a prepared export', async ({ page }) => {
  await setup(page, { revokeDownload: true });
  await page.getByRole('button', { name: 'Prepare CSV (.gz)', exact: true }).click();
  const download = page.getByRole('button', { name: 'Download CSV', exact: true });
  await expect(download).toBeVisible({ timeout: 10000 });
  await download.click();
  await expect(page.getByRole('alert')).toContainText('revoked');
});

test('a newly queued export remains visible before the jobs index catches up', async ({ page }) => {
  await setup(page, { delayedList: true });
  await page.getByRole('button', { name: 'Prepare CSV (.gz)', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Download CSV', exact: true })).toBeVisible({ timeout: 10000 });
});
