const numericReading = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
const summaryNumberFormat = new Intl.NumberFormat('en-US', {
  notation: 'standard', maximumFractionDigits: 2,
});

// Card-only display rounding. Never use this for stored readings or exports.
export const formatSummaryReadingValue = (value) => {
  if (value == null) return 'N/A';
  const text = String(value).trim();
  if (!numericReading.test(text) || !Number.isFinite(Number(text))) return 'N/A';
  const formatted = summaryNumberFormat.format(text);
  return formatted === '-0' ? '0' : formatted;
};

// Preserve the supplied decimal precision. Formatting must not manufacture a
// zero from a blank field or round individual observations to chart precision.
export const formatReadingValue = (value, column = '') => {
  if (value == null || String(value).trim() === '') return 'N/A';
  const text = String(value).trim();
  if (/(status|state|error)/i.test(column)) return text;
  if (!numericReading.test(text) || !Number.isFinite(Number(text))) return 'N/A';
  if (/e/i.test(text)) return text;
  const [integer, fraction] = text.split('.');
  const grouped = (integer || '0').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return fraction == null ? grouped : `${grouped}.${fraction}`;
};

export const observationTime = row => {
  const iso = row?.timestamp_iso;
  return iso ? (/(Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`) : row?.timestamp;
};
