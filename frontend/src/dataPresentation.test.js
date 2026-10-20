import assert from 'node:assert/strict';
import test from 'node:test';
import { formatReadingValue, formatSummaryReadingValue, observationTime } from './dataPresentation.js';

test('summary cards expand scientific notation and display at most two decimal places', () => {
  const cases = [
    ['4.23256e2', '423.26'], ['4.54308e+3', '4,543.08'],
    ['3.615586', '3.62'], ['2.141', '2.14'], ['10.255', '10.26'],
    ['1.005', '1.01'], ['-2.141', '-2.14'], ['1.2e-8', '0'],
    ['-0.00001', '0'], ['0', '0'], ['420.00', '420'],
    ['1e21', '1,000,000,000,000,000,000,000'],
    ['123456789012345678.125', '123,456,789,012,345,678.13'],
  ];
  for (const [input, expected] of cases) {
    assert.equal(formatSummaryReadingValue(input), expected);
  }
});

test('summary rounding never turns missing or invalid readings into zero', () => {
  for (const value of [null, undefined, '', '   ', 'NaN', 'Infinity', '-Infinity', NaN, Infinity, 'error', '0x10', '4,543.08']) {
    assert.equal(formatSummaryReadingValue(value), 'N/A');
  }
});

test('missing, invalid and non-finite measurements never become numeric zero', () => {
  for (const value of [null, undefined, '', '   ', 'NaN', 'Infinity', '-Infinity', NaN, Infinity, 'error', '0x10']) {
    assert.equal(formatReadingValue(value, 'Concentration'), 'N/A');
  }
});
test('individual reading precision, zero, negatives and scientific notation survive formatting', () => {
  assert.equal(formatReadingValue('1234.123456789012345'), '1,234.123456789012345');
  assert.equal(formatReadingValue('0'), '0');
  assert.equal(formatReadingValue('-2.00001'), '-2.00001');
  assert.equal(formatReadingValue('1.2e-8'), '1.2e-8');
  assert.equal(formatReadingValue('.125'), '0.125');
});
test('diagnostic status remains text, not a made-up number', () => {
  assert.equal(formatReadingValue('normal', 'Status'), 'normal');
});
test('UTC observation timestamps keep offsets and normalize only legacy naive UTC', () => {
  assert.equal(observationTime({ timestamp_iso: '2026-10-07T07:00:00' }), '2026-10-07T07:00:00Z');
  assert.equal(observationTime({ timestamp_iso: '2026-10-07T07:00:00+00:00' }), '2026-10-07T07:00:00+00:00');
  assert.equal(observationTime({ timestamp_iso: '2026-10-07T00:00:00-07:00' }), '2026-10-07T00:00:00-07:00');
});
