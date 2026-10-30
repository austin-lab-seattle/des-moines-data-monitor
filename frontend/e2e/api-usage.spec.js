import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
const contract = JSON.parse(execFileSync('python3', ['../tests/usage_scenarios.py', '--contract'], { encoding: 'utf8' }));
const authority = 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_oR7CIzxQG';
const client = '5tno54tskp2tk6lhh6c8jh9ev4';

async function setup(page, { failHistory = false } = {}) {
  await page.addInitScript(({ authority, client }) => {
    sessionStorage.setItem(`oidc.user:${authority}:${client}`, JSON.stringify({
      id_token: 'CI-only-not-a-real-token', access_token: 'CI-only-not-a-real-token', token_type: 'Bearer',
      profile: { sub: 'ci-only-admin', email: 'ci-admin@example.org', email_verified: true }, expires_at: Date.now() / 1000 + 3600,
    }));
  }, { authority, client });
  await page.route('**/air-quality/internal/v1/**', async route => {
    const url = new URL(route.request().url());
    expect(route.request().headers().authorization).toBe('Bearer CI-only-not-a-real-token');
    if (url.pathname.endsWith('/api-users')) {
      if (url.searchParams.has('user_id') && failHistory) return route.fulfill({ status: 503, json: { error: 'Usage history temporarily unavailable' } });
      return route.fulfill({ json: { ...contract, history: url.searchParams.has('user_id') ? contract.history : null } });
    }
    return route.fulfill({ json: { requests: [], viewer: { email: 'ci-admin@example.org', roles: ['Admin'], can_approve: true } } });
  });
  await page.goto('/team');
  await page.getByRole('button', { name: 'API users', exact: true }).click();
}

test('API users show durable combined lifetime, Pacific daily usage, last use and honest history gaps', async ({ page }) => {
  await setup(page);
  const row = page.locator('tr').filter({ hasText: 'researcher@example.org' }).first();
  await expect(row).toContainText('1 active, 1 revoked keys');
  await expect(row).toContainText('3 MB');
  await expect(row).toContainText('Since');
  await expect(row).toContainText('PDT');
  await expect(page.locator('tr').filter({ hasText: 'untracked@example.org' }).first()).toContainText('Not recorded');
  await row.getByRole('button', { name: 'Usage history' }).click();
  await expect(page.getByRole('heading', { name: /Daily usage for researcher@example.org/ })).toBeVisible();
  await expect(page.locator('.usage-history tbody tr').first()).toContainText('Not recorded');
  await expect(page.locator('.usage-history tbody tr').nth(6)).toContainText('3,000');
  await expect(page.locator('.usage-history tbody tr').nth(6)).toContainText('(partial)');
  await expect(page.locator('main')).toContainText('not completed downloads');
});

test('history failure never appears as a zero-usage day', async ({ page }) => {
  await setup(page, { failHistory: true });
  await page.locator('tr').filter({ hasText: 'researcher@example.org' }).first().getByRole('button', { name: 'Usage history' }).click();
  await expect(page.getByRole('alert')).toContainText('Usage history temporarily unavailable');
  await expect(page.locator('.usage-history').first().locator('tbody tr')).toHaveCount(0);
});
