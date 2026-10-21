import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';

// Real Python handlers and conditional transactions, using only Moto + fake
// inbox. The browser has no team session, and every network route is intercepted.
const contract = JSON.parse(execFileSync('python3', ['../scripts/quality/email_approval_contract.py'], { encoding: 'utf8' }));
const path = '/air-quality/v1/access-requests/email-decision';

async function setup(page) {
  expect(contract.synthetic_ci_only).toBe(true);
  const posts = [];
  let consumed = false;
  await page.route('**/*', route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== 'http://127.0.0.1:4175' || url.pathname !== path) return route.abort();
    let result;
    if (request.method() === 'POST') {
      posts.push(new URLSearchParams(request.postData()));
      result = posts.at(-1).get('token') === contract.approve_token && !consumed ? contract.approved : contract.used;
      consumed = true;
    } else {
      const token = url.searchParams.get('token');
      result = consumed ? contract.used : token === contract.approve_token ? contract.approve_preview
        : token === contract.reject_token ? contract.reject_preview : contract.invalid;
    }
    return route.fulfill({ status: result.statusCode, headers: result.headers, body: result.body });
  });
  return posts;
}

test('email approval needs no session, previews safely, and submits only on confirmation', async ({ page }) => {
  const posts = await setup(page);
  await page.goto(`${path}?token=${contract.approve_token}`);
  await expect(page.getByRole('heading', { name: 'API access decision' })).toBeVisible();
  await expect(page.getByText('No sign-in is required.', { exact: false })).toBeVisible();
  await expect(page.getByText('researcher@example.org', { exact: true })).toBeVisible();
  await expect(page.locator('img,script')).toHaveCount(0);
  expect(posts).toHaveLength(0);
  await page.reload();
  expect(posts).toHaveLength(0);
  await page.getByRole('button', { name: 'Confirm: Approve access', exact: true }).click();
  await expect(page.getByText('Request approved.', { exact: false })).toBeVisible();
  expect(posts).toHaveLength(1);
  expect([...posts[0].keys()]).toEqual(['token']);
  expect(posts[0].get('token')).toBe(contract.approve_token);
  await expect(page.getByRole('button')).toHaveCount(0);
  await page.goto(`${path}?token=${contract.approve_token}`);
  await expect(page.getByText('This approval link is invalid, expired or already used.')).toBeVisible();
  expect(posts).toHaveLength(1);
});

test('reject preview labels its decision clearly, and invalid links disclose no requester', async ({ page }) => {
  const posts = await setup(page);
  await page.goto(`${path}?token=${contract.reject_token}`);
  await expect(page.getByRole('button', { name: 'Confirm: Reject access', exact: true })).toBeVisible();
  await expect(page.getByText('No API key will be issued.', { exact: true })).toBeVisible();
  expect(posts).toHaveLength(0);
  await page.goto(`${path}?token=invalid`);
  await expect(page.getByText('This approval link is invalid, expired or already used.')).toBeVisible();
  await expect(page.getByText('researcher@example.org', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button')).toHaveCount(0);
});
