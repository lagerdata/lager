// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

// Decoder for LSCP/1 oscilloscope capture frames.
//
// Mirrors protocol/src/lscp.rs and lager/measurement/scope/lscp.py. Samples
// are exposed as an Int16Array view over the received ArrayBuffer, so a
// capture costs one allocation for the buffer and nothing per sample. The
// renderer maps counts straight to pixels and never converts to volts.

const MAGIC = 0x5043534c; // "LSCP" little-endian
const VERSION = 1;
const HEADER_SIZE = 64;
const CHANNEL_DESC_SIZE = 16;

export const FLAG_TRIGGERED = 1 << 0;
export const FLAG_STREAMING = 1 << 1;

const COUPLING = ['DC', 'AC', 'GND'];

export class LscpError extends Error {}

export class CaptureFrame {
  constructor(fields) {
    Object.assign(this, fields);
  }

  get triggered() {
    return (this.flags & FLAG_TRIGGERED) !== 0;
  }

  get streaming() {
    return (this.flags & FLAG_STREAMING) !== 0;
  }

  get sampleRateHz() {
    return this.sampleIntervalNs > 0 ? 1e9 / this.sampleIntervalNs : 0;
  }

  get durationS() {
    return (this.samplesPerChannel * this.sampleIntervalNs) / 1e9;
  }

  channelIndex(label) {
    const wanted = String(label).toUpperCase();
    return this.channels.findIndex((c) => c.channel === wanted);
  }

  // Raw ADC counts for a channel. A subarray view, so no copy.
  counts(channel) {
    const index =
      typeof channel === 'number' ? channel : this.channelIndex(channel);
    if (index < 0 || index >= this.channels.length) {
      throw new LscpError(`no such channel: ${channel}`);
    }
    const n = this.samplesPerChannel;
    return this.samples.subarray(index * n, (index + 1) * n);
  }

  // Volts for a channel. Allocates a Float64Array, unlike counts().
  volts(channel) {
    const index =
      typeof channel === 'number' ? channel : this.channelIndex(channel);
    if (index < 0 || index >= this.channels.length) {
      throw new LscpError(`no such channel: ${channel}`);
    }
    const { scaleVPerCount, offsetV } = this.channels[index];
    const raw = this.counts(index);
    const out = new Float64Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) {
      out[i] = raw[i] * scaleVPerCount + offsetV;
    }
    return out;
  }

  // Seconds relative to the trigger, so t=0 is the trigger point.
  timeAxis() {
    const intervalS = this.sampleIntervalNs / 1e9;
    const start = -this.preTriggerSamples * intervalS;
    const out = new Float64Array(this.samplesPerChannel);
    for (let i = 0; i < out.length; i += 1) {
      out[i] = start + i * intervalS;
    }
    return out;
  }

  overflowed(index) {
    return (this.overflowMask & (1 << index)) !== 0;
  }
}

export function decode(buffer) {
  const bytes =
    buffer instanceof ArrayBuffer ? buffer : buffer.buffer.slice(
      buffer.byteOffset,
      buffer.byteOffset + buffer.byteLength,
    );

  if (bytes.byteLength < HEADER_SIZE) {
    throw new LscpError(
      `frame too short: need ${HEADER_SIZE} bytes, got ${bytes.byteLength}`,
    );
  }

  const view = new DataView(bytes);
  const magic = view.getUint32(0, true);
  if (magic !== MAGIC) {
    throw new LscpError(
      `bad magic 0x${magic.toString(16)}, expected 0x${MAGIC.toString(16)}`,
    );
  }

  const version = view.getUint16(4, true);
  if (version !== VERSION) {
    throw new LscpError(
      `unsupported LSCP version ${version}, this build speaks ${VERSION}`,
    );
  }

  const flags = view.getUint16(6, true);
  // Sequence and timestamp exceed 2^53 only after ~104 days of uptime, so
  // Number is safe here and avoids BigInt in the render path.
  const seq = Number(view.getBigUint64(8, true));
  const captureMonoNs = Number(view.getBigUint64(16, true));
  const sampleIntervalNs = view.getFloat64(24, true);
  const preTriggerSamples = view.getUint32(32, true);
  const postTriggerSamples = view.getUint32(36, true);
  const samplesPerChannel = view.getUint32(40, true);
  const channelCount = view.getUint8(44);
  const resolutionBits = view.getUint8(45);
  const overflowMask = view.getUint16(46, true);

  const descriptorsEnd = HEADER_SIZE + channelCount * CHANNEL_DESC_SIZE;
  if (bytes.byteLength < descriptorsEnd) {
    throw new LscpError(
      `frame too short: need ${descriptorsEnd} bytes, got ${bytes.byteLength}`,
    );
  }

  const channels = [];
  for (let i = 0; i < channelCount; i += 1) {
    const at = HEADER_SIZE + i * CHANNEL_DESC_SIZE;
    channels.push({
      channel: String.fromCharCode(65 + view.getUint8(at)),
      rangeCode: view.getUint8(at + 1),
      coupling: COUPLING[view.getUint8(at + 2)] || 'DC',
      scaleVPerCount: view.getFloat32(at + 4, true),
      offsetV: view.getFloat32(at + 8, true),
    });
  }

  const expected = samplesPerChannel * channelCount * 2;
  const payloadBytes = bytes.byteLength - descriptorsEnd;
  if (payloadBytes !== expected) {
    throw new LscpError(
      `payload length mismatch: expected ${expected} bytes, got ${payloadBytes}`,
    );
  }

  const samples = new Int16Array(
    bytes,
    descriptorsEnd,
    samplesPerChannel * channelCount,
  );

  return new CaptureFrame({
    seq,
    captureMonoNs,
    sampleIntervalNs,
    preTriggerSamples,
    postTriggerSamples,
    samplesPerChannel,
    resolutionBits,
    overflowMask,
    flags,
    channels,
    samples,
  });
}
