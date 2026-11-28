/*
 * Waveform reduction — pure functions over sample arrays, no DOM.
 */

/** Max-abs per column, normalised to the global max (zeros if silent). */
export function peaksForWidth(channel: Float32Array, columns: number): Float32Array {
  const out = new Float32Array(columns);
  if (columns <= 0 || channel.length === 0) return out;
  const win = Math.ceil(channel.length / columns);
  let max = 0;
  for (let c = 0; c < columns; c++) {
    const end = Math.min(channel.length, (c + 1) * win);
    let m = 0;
    for (let i = c * win; i < end; i++) m = Math.max(m, Math.abs(channel[i] ?? 0));
    out[c] = m;
    max = Math.max(max, m);
  }
  if (max === 0) return out;
  for (let c = 0; c < columns; c++) out[c] = (out[c] ?? 0) / max;
  return out;
}

/** Average several channels into one (length of the longest). */
export function mergeChannels(buffers: Float32Array[]): Float32Array {
  const len = buffers.reduce((n, b) => Math.max(n, b.length), 0);
  const out = new Float32Array(len);
  if (buffers.length === 0) return out;
  for (const b of buffers) for (let i = 0; i < b.length; i++) out[i] = (out[i] ?? 0) + (b[i] ?? 0);
  for (let i = 0; i < len; i++) out[i] = (out[i] ?? 0) / buffers.length;
  return out;
}

/**
 * Abs-max envelope with one value per `1 / rateHz` seconds, normalised to
 * its max (zeros if silent). Drawn later with `columnPeaks` at any zoom.
 */
export function envelope(channel: Float32Array, sampleRate: number, rateHz: number): Float32Array {
  const win = Math.max(1, Math.round(sampleRate / rateHz));
  const out = new Float32Array(Math.ceil(channel.length / win));
  let max = 0;
  for (let c = 0; c < out.length; c++) {
    const end = Math.min(channel.length, (c + 1) * win);
    let m = 0;
    for (let i = c * win; i < end; i++) m = Math.max(m, Math.abs(channel[i] ?? 0));
    out[c] = m;
    max = Math.max(max, m);
  }
  if (max > 0) for (let c = 0; c < out.length; c++) out[c] = (out[c] ?? 0) / max;
  return out;
}

/**
 * One peak per pixel column: the max of `env` (sampled at `rateHz`) over the
 * seconds each column covers, starting at `fromS`. Outside `env` is silence.
 */
export function columnPeaks(
  env: ArrayLike<number>,
  rateHz: number,
  fromS: number,
  pxPerS: number,
  columns: number,
): Float32Array {
  const out = new Float32Array(Math.max(0, columns));
  for (let c = 0; c < out.length; c++) {
    const first = Math.floor((fromS + c / pxPerS) * rateHz);
    const last = Math.max(first + 1, Math.ceil((fromS + (c + 1) / pxPerS) * rateHz));
    let m = 0;
    for (let i = Math.max(0, first); i < Math.min(env.length, last); i++)
      m = Math.max(m, env[i] ?? 0);
    out[c] = m;
  }
  return out;
}
