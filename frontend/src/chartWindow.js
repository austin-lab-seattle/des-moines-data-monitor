const minute = 60000;
const hour = 60 * minute;
const day = 24 * hour;

export function defaultChartWindow(now = Date.now()) {
  const end = Math.floor(now / hour) * hour + hour;
  return { start: end - 14 * day, end };
}

export function chartWindowParams(window, bucketMinutes) {
  const width = bucketMinutes * minute;
  const start = Math.floor(window.start / width) * width;
  const end = Math.ceil(window.end / width) * width;
  return {
    start: new Date(start).toISOString(),
    end: new Date(end - 1).toISOString().replace('.999Z', '.999999Z'),
  };
}

export function changeChartZoom(base, current, direction, anchor = 0.5) {
  const range = current || base;
  const width = (range.end - range.start) * (direction === 'in' ? 0.5 : 2);
  if (width >= base.end - base.start) return null;
  const span = Math.max(width, hour);
  if (span >= base.end - base.start) return null;
  if (span === range.end - range.start) return current;
  const target = range.start + (range.end - range.start - span) * anchor;
  const start = Math.min(Math.max(target, base.start), base.end - span);
  return { start, end: start + span };
}

export function chartBucketMinutes(zoom) {
  return zoom && zoom.end - zoom.start <= 4 * day ? 15 : 60;
}
