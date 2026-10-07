import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { formatReadingValue } from '../src/dataPresentation.js';

// Exercise real Python handlers against CI-only Silver data. Independent
// expected values in that script make a wrong backend mean fail this suite.
const contract = JSON.parse(execFileSync('python3', ['../scripts/quality/accuracy_contract.py'], { encoding: 'utf8' }));
const names = { 'BC-MA200': 'Black carbon', 'CO2-LICOR': 'Carbon dioxide', 'NEPH-PM25': 'Fine particles', 'NO2-CAPS': 'Nitrogen dioxide', SMPS: 'Particle number' };

test('all five instruments display real handler observations and export independently verified means', async ({ page }) => {
  expect(contract.synthetic_ci_only).toBe(true);
  await page.route('**/air-quality/**', async route => {
    const url = new URL(route.request().url());
    const item = contract.instruments[url.searchParams.get('instrument')];
    const json = url.pathname.endsWith('/timeseries') ? item?.timeseries
      : url.pathname.endsWith('/observations') ? (url.searchParams.get('limit') === '1' ? item?.latest : item?.observations) : null;
    await route.fulfill(json ? { json } : { status: 503, json: { error: 'Unexpected route' } });
  });
  await page.goto('/');
  for (const [id, item] of Object.entries(contract.instruments)) {
    const card = page.getByRole('button', { name: `View latest readings from ${names[id]}` });
    await expect(card.locator('.pollutant-value')).toHaveText(formatReadingValue(item.expectedLatest));
    await card.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.locator('tbody tr')).toHaveCount(2);
    await expect(dialog.locator('tbody tr').first().locator('td').nth(1)).toHaveText(formatReadingValue(item.expectedLatest));
    await expect(dialog.locator('tbody tr').first()).toContainText('12:00:05 AM');
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await page.locator('.chart-controls select').nth(0).selectOption(id);
    await expect(page.locator('.chart-controls select').nth(1)).toHaveValue(item.column);
    await expect(page.getByRole('button', { name: 'Chart CSV', exact: true })).toBeEnabled();
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Chart CSV', exact: true }).click();
    const csv = await readFile(await (await downloadPromise).path(), 'utf8');
    expect(csv.trim().split('\n')[1]).toBe(`2026-10-07T07:00:00Z,${item.expectedMean}`);
  }
});

test('a mismatched backend response never plots values under the selected instrument', async ({ page }) => {
  await page.route('**/air-quality/**', route => route.fulfill({ json: contract.instruments['NO2-CAPS'].timeseries }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('does not match the selected instrument');
  await expect(page.locator('.chart-plot')).toHaveCount(0);
});
