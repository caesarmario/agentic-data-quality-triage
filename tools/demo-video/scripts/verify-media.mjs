import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {output, tooling} from './paths.mjs';

const require = createRequire(import.meta.url);
const ffprobe = require('ffprobe-static').path;
const result = spawnSync(ffprobe, ['-v', 'error', '-show_format', '-show_streams', '-of', 'json', output], {encoding: 'utf8', windowsHide: true});
if (result.status !== 0) throw new Error(result.stderr);
const probe = JSON.parse(result.stdout);
const video = probe.streams.filter((stream) => stream.codec_type === 'video');
const audio = probe.streams.filter((stream) => stream.codec_type === 'audio');
const bytes = await fs.readFile(output);
const checks = {
  oneVideoStream: video.length === 1,
  h264: video[0]?.codec_name === 'h264',
  resolution: video[0]?.width === 1280 && video[0]?.height === 720,
  fps: video[0]?.r_frame_rate === '30/1',
  frameCount: Number(video[0]?.nb_frames) === 5400,
  duration: Math.abs(Number(probe.format.duration) - 180) < 0.001,
  noAudio: audio.length === 0,
  uploadSize: bytes.length <= 9_500_000,
  yuv420p: video[0]?.pix_fmt === 'yuv420p',
};
const report = {
  artifact: 'data/acceptance/repository-refresh/datasentry-demo.mp4',
  bytes: bytes.length,
  decimalMB: Number((bytes.length / 1e6).toFixed(3)),
  sha256: createHash('sha256').update(bytes).digest('hex'),
  durationSeconds: Number(probe.format.duration),
  width: video[0]?.width, height: video[0]?.height, codec: video[0]?.codec_name,
  pixelFormat: video[0]?.pix_fmt, fps: video[0]?.r_frame_rate, frames: video[0]?.nb_frames,
  audioStreams: audio.length, checks,
  scope: 'Local media QA only. No new Airflow/platform acceptance, services, browser UI actions, provider calls or remediation.',
};
await fs.writeFile(path.join(tooling, 'media-verification.json'), JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report, null, 2));
if (Object.values(checks).some((passed) => !passed)) process.exitCode = 1;
