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

export function changeChartZoom(base, current, direction, anchor = 0.5, visible = current || base) {
  const range = current || base;
  const width = (range.end - range.start) * (direction === 'in' ? 0.5 : 2);
  if (width >= base.end - base.start) return null;
  const span = Math.max(width, hour);
  if (span >= base.end - base.start) return null;
  if (span === range.end - range.start) return current;
  const target = visible.start + (visible.end - visible.start - span) * anchor;
  const start = Math.min(Math.max(target, base.start), base.end - span);
  return { start, end: start + span };
}

export function chartBucketMinutes(zoom) {
  return zoom && zoom.end - zoom.start <= 4 * day ? 15 : 60;
}

export function chartUsesClockLabels(window) {
  return Boolean(window && window.end - window.start < day);
}

// Use the space available for the labels. Tick spacing can be coarser than
// the underlying averages, but never finer than their sampling interval.
export function chartWindowTicks(window, bucketMinutes, plotWidth) {
  if (!window || !(plotWidth > 0)) return undefined;
  const labelSpace = chartUsesClockLabels(window) ? 46 : 80;
  const intervals = Math.max(1, Math.floor(plotWidth / labelSpace));
  const target = Math.max(bucketMinutes * minute, (window.end - window.start) / intervals);
  const steps = [15 * minute, 30 * minute, hour, 2 * hour, 3 * hour, 6 * hour,
    12 * hour, day, 2 * day, 3 * day, 7 * day];
  const step = steps.find(width => width >= target) ?? Math.ceil(target / (7 * day)) * 7 * day;
  const first = Math.ceil(window.start / step) * step;
  return Array.from({ length: Math.max(0, Math.floor((window.end - first) / step) + 1) }, (_, index) => first + index * step);
}

// A null breaks the line across empty buckets. Keep these display-only points
// out of downloads, and draw a marker where an isolated average has no line.
export function chartPlotPoints(series, bucketMinutes) {
  const width = bucketMinutes * minute;
  const points = series.map(point => ({ ...point, timeMs: Date.parse(point.t) }));
  return points.flatMap((point, i) => {
    const previous = points[i - 1]?.timeMs;
    const next = points[i + 1]?.timeMs;
    const hasPrevious = point.timeMs - previous === width;
    const hasNext = next - point.timeMs === width;
    const sample = { ...point, isolated: !hasPrevious && !hasNext };
    return i && !hasPrevious ? [{ timeMs: previous + width, v: null }, sample] : [sample];
  });
}
