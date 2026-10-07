import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const instruments = {
  'BC-MA200': { name: 'Black carbon', columns: ['BC1'], value: '120' },
  'CO2-LICOR': { name: 'Carbon dioxide', columns: ['CO2_(umol_mol-1)', 'H2O_(mmol_mol-1)', 'CO2_Absorption'], value: '420' },
  'NEPH-PM25': { name: 'Fine particles', columns: ['PM2.5 (µg/m³)', 'BScat (10^-4 m^-1)'], value: '5.123' },
  'NO2-CAPS': { name: 'Nitrogen dioxide', columns: ['Concentration', 'Loss', 'Span', 'LastBaseline', 'HHMMSS'], value: '10.255' },
  SMPS: { name: 'Particle number', columns: ['Total Concentration (#/cm³)', 'Geo. Std. Dev'], value: '5497.34' },
};

async function mockApi(page, { missingBlackCarbon = false, no2Error = false, uploadError = false } = {}) {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/air-quality/**', async route => {
    const url = new URL(route.request().url());
    const id = url.searchParams.get('instrument');
    const instrument = instruments[id];
    if (url.pathname.endsWith('/summary') && url.searchParams.get('view') === 'latest-upload') {
      await route.fulfill(uploadError ? { status: 503, json: { error: 'Upload information unavailable' } }
        : { json: { instrument_id: 'NO2-CAPS', uploaded_at: '2026-10-07T07:10:00+00:00' } });
      return;
    }
    if (url.pathname.endsWith('/observations') && instrument) {
      if (id === 'BC-MA200' && missingBlackCarbon) {
        await route.fulfill({ status: 404, json: { error: 'Records not found' } });
        return;
      }
      if (id === 'NO2-CAPS' && no2Error) {
        await route.fulfill({ status: 503, json: { error: 'Temporarily unavailable' } });
        return;
      }
      const count = Number(url.searchParams.get('limit'));
      const rows = Array.from({ length: count }, (_, index) => ({
        row_key: `${id}-${index}`,
        timestamp: `2026-10-07 00:06:${String(59 - index).padStart(2, '0')}`,
        timestamp_iso: `2026-10-07T07:06:${String(59 - index).padStart(2, '0')}${id === 'NO2-CAPS' ? '' : '+00:00'}`,
        values: { [instrument.columns[0]]: instrument.value, HHMMSS: '000659' },
        status: 'normal',
      }));
      await route.fulfill({ json: { instrument_id: id, columns: instrument.columns, rows } });
      return;
    }
    if (url.pathname.endsWith('/timeseries') && instrument) {
      // Include a legacy HHMMSS option: the public UI must still hide it.
      await route.fulfill({ json: {
        instrument_id: id,
        measurement: url.searchParams.get('measurement') || instrument.columns[0], measurements: instrument.columns,
        series: [{ t: '2026-10-07T07:00:00Z', v: 9000 }],
      } });
      return;
    }
    // The public page must not depend on the slow inventory summary.
    await route.fulfill({ status: 503, json: { error: 'Unexpected API route' } });
  });
  return errors;
}

test('Conditions shows pollutant readings and hides internal counters and aircraft', async ({ page }) => {
  const errors = await mockApi(page);
  const requests = [];
  page.on('request', request => requests.push(request.url()));
  await page.goto('/');
  for (const instrument of Object.values(instruments)) {
    await expect(page.getByRole('button', { name: `View latest readings from ${instrument.name}` }))
      .toContainText(Number(instrument.value).toLocaleString('en-US', { maximumFractionDigits: 3 }));
  }
  for (const id of Object.keys(instruments)) {
    await expect(page.locator('.pollutant-instrument-id', { hasText: id })).toBeVisible();
  }
  await expect(page.getByRole('navigation')).not.toContainText('Aircraft');
  await expect(page.locator('main')).not.toContainText(/Raw rows|Cleaned rows|Clean observations|Latest source|completed upload/);
  expect(requests.some(value => {
    const url = new URL(value);
    return url.pathname.endsWith('/summary') && url.searchParams.get('view') !== 'latest-upload' || url.pathname.endsWith('/flights');
  })).toBe(false);
  expect(errors).toEqual([]);
});

test('each pollutant opens eight labeled readings in Conditions', async ({ page }) => {
  const errors = await mockApi(page);
  await page.goto('/');
  for (const instrument of Object.values(instruments)) {
    await page.getByRole('button', { name: `View latest readings from ${instrument.name}` }).click();
    const dialog = page.getByRole('dialog', { name: instrument.name });
    await expect(dialog.locator('tbody tr')).toHaveCount(8);
    await expect(dialog.getByRole('columnheader', { name: 'Recorded (Pacific time)' })).toBeVisible();
    await expect(dialog).not.toContainText('HHMMSS');
    await expect(dialog.locator('tbody tr').first()).toContainText('12:06:59 AM');
    await expect(dialog).toContainText(Object.keys(instruments).find(id => instruments[id] === instrument));
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    expect(new URL(page.url()).pathname).toBe('/');
  }
  expect(errors).toEqual([]);
});

test('NO2 aggregation selects concentration and hides the clock field', async ({ page }) => {
  const errors = await mockApi(page);
  await page.goto('/');
  await page.locator('.chart-controls select').nth(0).selectOption('NO2-CAPS');
  const selector = page.locator('.chart-controls select').nth(1);
  await expect(selector).toHaveValue('Concentration');
  await expect(selector.locator('option')).toHaveText(['NO₂ concentration (ppb)', 'Loss', 'Span', 'LastBaseline']);
  expect(errors).toEqual([]);
});

test('recent values use individual Silver observations, not hourly chart aggregates', async ({ page }) => {
  const errors = await mockApi(page);
  await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/timeseries')),
    page.goto('/'),
  ]);
  const requests = [];
  page.on('request', request => requests.push(new URL(request.url())));
  await page.getByRole('button', { name: 'View latest readings from Nitrogen dioxide' }).click();
  const dialog = page.getByRole('dialog', { name: 'Nitrogen dioxide' });
  await expect(dialog.locator('tbody tr')).toHaveCount(8);
  await expect(dialog.locator('tbody tr').first().locator('td').nth(1)).toHaveText('10.255');
  await expect(dialog).toContainText('Not hourly averages');
  await expect(dialog.locator('tbody')).not.toContainText('9,000');
  const observation = requests.find(url => url.pathname.endsWith('/observations'));
  expect(observation?.searchParams.get('instrument')).toBe('NO2-CAPS');
  expect(observation?.searchParams.get('limit')).toBe('8');
  expect(observation?.searchParams.get('order')).toBe('desc');
  expect(requests.some(url => url.pathname.endsWith('/timeseries'))).toBe(false);
  expect(errors).toEqual([]);
});

test('unavailable instruments do not blank the public page', async ({ page }) => {
  const errors = await mockApi(page, { missingBlackCarbon: true, no2Error: true });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'View latest readings from Carbon dioxide' })).toContainText('420');
  await expect(page.getByRole('button', { name: 'View latest readings from Black carbon' })).toContainText('No reading available');
  await expect(page.getByRole('button', { name: 'View latest readings from Nitrogen dioxide' })).toContainText('Temporarily unavailable');
  await page.getByRole('button', { name: 'View latest readings from Black carbon' }).click();
  await expect(page.getByRole('dialog')).toContainText('No readings are available');
  expect(errors).toEqual([]);
});

test('secondary aggregation choices survive the public dashboard cleanup', async ({ page }) => {
  const errors = await mockApi(page);
  await page.goto('/');
  for (const id of ['CO2-LICOR', 'NEPH-PM25', 'SMPS']) {
    await page.locator('.chart-controls select').nth(0).selectOption(id);
    const selector = page.locator('.chart-controls select').nth(1);
    await expect(selector).toHaveValue(instruments[id].columns[0]);
    for (const column of instruments[id].columns) {
      await expect(selector.locator(`option[value="${column}"]`)).toHaveCount(1);
    }
    await selector.selectOption(instruments[id].columns[1]);
    await expect.poll(() => page.locator('.chart-controls select').nth(1).inputValue())
      .toBe(instruments[id].columns[1]);
  }
  expect(errors).toEqual([]);
});

test('production aircraft URLs do not expose the experimental map', async ({ page }) => {
  const errors = await mockApi(page);
  for (const path of ['/aircraft', '/lab/aircraft']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'Air monitoring conditions' })).toBeVisible();
    await expect(page.getByRole('navigation')).not.toContainText('Aircraft');
    await expect(page.locator('.flights-map')).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test('public API rewrites use canonical endpoints instead of retired aliases', async () => {
  const config = JSON.parse(await readFile(new URL('../vercel.json', import.meta.url), 'utf8'));
  const rewrites = config.rewrites.filter(rule => rule.source.startsWith('/air-quality/'));
  expect(rewrites.length).toBeGreaterThan(0);
  for (const rule of rewrites) {
    const target = new URL(rule.destination);
    expect(target.protocol).toBe('https:');
    expect(target.pathname).toBe(rule.source);
  }
});

test('mobile Conditions keeps instrument names and popup within the screen', async ({ page }) => {
  const errors = await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByRole('complementary', { name: 'Latest upload' })).toContainText('12:10:00 AM');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  await expect(page.locator('.pollutant-instrument-id', { hasText: 'NO2-CAPS' })).toBeVisible();
  await page.getByRole('button', { name: 'View latest readings from Nitrogen dioxide' }).click();
  await expect(page.getByRole('dialog').locator('tbody tr')).toHaveCount(8);
  const width = await page.getByRole('dialog').evaluate(element => element.getBoundingClientRect().width);
  expect(width).toBeLessThanOrEqual(390);
  expect(errors).toEqual([]);
});

test('blue Conditions hero shows real upload time separately from reading time in both themes', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  const widget = page.getByRole('complementary', { name: 'Latest upload' });
  await expect(widget.locator('time')).toHaveAttribute('datetime', '2026-10-07T07:10:00+00:00');
  await expect(widget).toContainText('12:10:00 AM');
  await expect(widget).toContainText('NO2-CAPS');
  await expect(widget).not.toContainText('12:06:59 AM');
  const background = () => page.locator('.conditions-hero').evaluate(element => getComputedStyle(element).backgroundImage);
  expect(await background()).toContain('linear-gradient');
  await page.getByRole('button', { name: 'Use dark theme' }).click();
  expect(await background()).toContain('linear-gradient');
  await expect(widget).toContainText('12:10:00 AM');
});

test('failed upload metadata does not replace instrument readings or invent an upload', async ({ page }) => {
  await mockApi(page, { uploadError: true });
  await page.goto('/');
  const widget = page.getByRole('complementary', { name: 'Latest upload' });
  await expect(widget).toContainText('Temporarily unavailable');
  await expect(widget.locator('time')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'View latest readings from Carbon dioxide' })).toContainText('420');
});

test('Observations pagination uses a forward arrow and the applied filters', async ({ page }) => {
  const errors = await mockApi(page);
  const observationRequests = [];
  await page.route('**/air-quality/v1/observations?**', async route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('limit') !== '100') return route.fallback();
    observationRequests.push(url);
    const cursor = Number(url.searchParams.get('cursor'));
    await route.fulfill({ json: {
      instrument_id: 'NO2-CAPS', columns: ['Concentration'], next_cursor: cursor === 0 ? 1 : null,
      rows: [{ row_key: `row-${cursor}`, timestamp: '2026-10-07 00:00:00', values: { Concentration: cursor ? '9.87654321' : '10.123456789' }, status: 'normal' }],
    } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Observations', exact: true }).click();
  const nextPage = page.getByRole('button', { name: 'Next Page', exact: true });
  await expect(nextPage).toBeVisible();
  await expect(nextPage.locator('.lucide-arrow-right')).toHaveCount(1);
  await expect(nextPage.locator('.lucide-refresh-cw')).toHaveCount(0);
  // Edited-but-not-applied filter controls must not change a paginated query.
  await page.getByLabel('Start Time (filter)', { exact: true }).fill('2026-10-06T00:00');
  await nextPage.click();
  await expect(page.locator('.review-table tbody')).toContainText('9.87654321');
  expect(observationRequests.at(-1).searchParams.get('cursor')).toBe('1');
  expect(observationRequests.at(-1).searchParams.has('start')).toBe(false);
  expect(errors).toEqual([]);
});

test('changing the Observations instrument clears old rows before the next response', async ({ page }) => {
  await mockApi(page);
  let completeCo2;
  const gate = new Promise(resolve => { completeCo2 = resolve; });
  await page.route('**/air-quality/v1/observations?**', async route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('instrument') === 'CO2-LICOR' && url.searchParams.get('limit') === '100') await gate;
    return route.fallback();
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Observations', exact: true }).click();
  await expect(page.locator('.review-table tbody')).toContainText('10.255');
  await page.locator('.filter-card select').selectOption('CO2-LICOR');
  await expect(page.locator('.review-table tbody')).not.toContainText('10.255');
  completeCo2();
  await expect(page.locator('.review-table tbody')).toContainText('420');
});
