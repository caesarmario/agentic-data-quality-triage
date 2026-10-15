import fs from 'node:fs/promises';
import path from 'node:path';
import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';
import {tooling, root, cache, publicDir, output, qaFrames} from './paths.mjs';
import {writeCaptions} from './captions.mjs';

const mode = process.argv[2] ?? 'frames';
if (!['frames', 'video', 'poster'].includes(mode)) throw new Error('Expected frames, video or poster');
const manifest = JSON.parse(await fs.readFile(path.join(tooling, 'asset-provenance.json'), 'utf8'));
if (mode !== 'poster' && manifest.diagrams.length !== 2) throw new Error('Both architecture and data-model diagrams are required. Run prepare:assets again.');
await fs.mkdir(cache, {recursive: true});
await writeCaptions();
const serveUrl = await bundle({entryPoint: path.join(tooling, 'src/Root.tsx'), outDir: path.join(cache, 'bundle'), publicDir,
  onProgress: (value) => {if (value === 100) console.log('Composition bundled.');}});
const composition = await selectComposition({serveUrl, id: 'DataSentryDemo'});
if (composition.durationInFrames !== 5400 || composition.fps !== 30) throw new Error('Unexpected composition timing');
if (mode === 'poster') {
  await renderStill({serveUrl, composition, frame: 90, imageFormat: 'png', output: path.join(root, 'docs/images/demo-refresh-poster.png'), logLevel: 'error'});
  console.log('Rendered docs/images/demo-refresh-poster.png');
} else if (mode === 'frames') {
  const story = JSON.parse(await fs.readFile(path.join(tooling, 'src/story.json'), 'utf8'));
  const frames = [...new Set([...qaFrames, ...story.map((scene) => Math.floor((scene.start + scene.end) / 2 * 30))])].sort((a, b) => a - b);
  const directory = path.join(cache, 'qa-frames');
  await fs.mkdir(directory, {recursive: true});
  for (const frame of frames) {
    await renderStill({serveUrl, composition, frame, imageFormat: 'png', output: path.join(directory, `frame-${String(frame).padStart(4, '0')}.png`), logLevel: 'error'});
    console.log(`QA frame ${frame} / ${(frame / 30).toFixed(3)}s`);
  }
  await renderStill({serveUrl, composition, frame: 90, imageFormat: 'png', output: path.join(root, 'docs/images/demo-refresh-poster.png'), logLevel: 'error'});
  await fs.writeFile(path.join(cache, 'qa-frames.json'), JSON.stringify({chapterStartMidEndFrames: qaFrames, allRenderedFrames: frames}, null, 2) + '\n');
} else {
  await fs.mkdir(path.dirname(output), {recursive: true});
  let last = -1;
  await renderMedia({serveUrl, composition, codec: 'h264', outputLocation: output,
    pixelFormat: 'yuv420p', videoBitrate: '350k', maxRate: '390k', bufferSize: '780k',
    x264Preset: 'slow', concurrency: 2, imageFormat: 'png', muted: true,
    onProgress: ({progress}) => {const percent = Math.floor(progress * 100); if (percent >= last + 10) {last = percent; console.log(`Render ${percent}%`);}},
    metadata: {title: 'DataSentry - recorded evidence walkthrough', comment: '180s silent; recorded UI; no new provider calls; separate evidence sessions'},
    logLevel: 'warn'});
  console.log('Rendered data/acceptance/repository-refresh/datasentry-demo.mp4');
}
