import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {root, tooling, publicDir, chapterBounds, qaFrames} from './paths.mjs';
import {timecode} from './captions.mjs';

const read = async (name) => JSON.parse(await fs.readFile(path.join(tooling, name), 'utf8'));
const story = await read('src/story.json');
const captions = await read('src/captions.json');
test('story covers exactly 180 seconds without gaps or overlaps', () => {
  let position = 0;
  for (const scene of story) {assert.equal(scene.start, position); assert.ok(scene.end > scene.start); position = scene.end;}
  assert.equal(position, 180);
});
test('all seven required chapter boundaries are exact', () => {
  assert.deepEqual([...new Set(story.map((scene) => scene.chapter))], [0, 1, 2, 3, 4, 5, 6]);
  chapterBounds.slice(0, -1).forEach((start, chapter) => assert.equal(story.find((scene) => scene.chapter === chapter).start, start));
});
test('English captions are continuous, bounded and readable', () => {
  let position = 0;
  for (const cue of captions) {assert.equal(cue.start, position); assert.ok(cue.end > cue.start); assert.ok(cue.text.length / (cue.end - cue.start) < 32); position = cue.end;}
  assert.equal(position, 180);
});
test('caption timecodes preserve millisecond precision', () => {
  assert.equal(timecode(180), '00:03:00.000'); assert.equal(timecode(0.1), '00:00:00.100');
});
test('chapter starts, middles and last frames are scheduled for visual QA', () => {
  assert.equal(qaFrames.length, 21); assert.equal(qaFrames[0], 0); assert.equal(qaFrames.at(-1), 5399);
});
test('Remotion packages use one exact version and compatible React pins', async () => {
  const pkg = await read('package.json');
  for (const [name, version] of Object.entries(pkg.dependencies)) if (name.includes('remotion')) assert.equal(version, '4.0.535');
  assert.equal(pkg.dependencies.react, pkg.dependencies['react-dom']);
});
test('all public UI assets have crop provenance and matching hashes', async () => {
  const manifest = await read('asset-provenance.json');
  assert.ok(manifest.assets.length >= 7);
  for (const asset of manifest.assets) {
    assert.match(asset.path, /^docs\/images\/demo-refresh-[\w-]+\.png$/);
    assert.equal(asset.inspectedBeforeUse, true); assert.equal(asset.crop.length, 4);
    const bytes = await fs.readFile(path.join(root, asset.path));
    assert.equal(createHash('sha256').update(bytes).digest('hex'), asset.sha256);
  }
  assert.ok(manifest.assets.find((asset) => asset.id === 'resolved').mask);
});
test('both repository diagrams are present, attributed and hash matched', async () => {
  const manifest = await read('asset-provenance.json');
  assert.equal(manifest.diagrams.length, 2);
  const recorded = new Map([
    ['tools/demo-video/recorded-diagrams/architecture.svg', '494a72260ac466a6faffd31dc651187aa19b4b682660ff386ef00aafe53e8a30'],
    ['tools/demo-video/recorded-diagrams/data-model-overview.svg', '1b8d16d20e91f3a75f5462d9dd86ab22d72ffef6a1de1bfbd4207dbd7e2800da'],
  ]);
  for (const diagram of manifest.diagrams) {
    assert.equal(recorded.get(diagram.path), diagram.sha256);
    assert.equal(diagram.recordedFrom, diagram.path.endsWith('architecture.svg')
      ? 'HEAD:docs/diagrams/architecture.svg' : 'HEAD:docs/diagrams/data-model-overview.svg');
    assert.match(diagram.label, /predates evidence-driven recovery/);
    const bytes = await fs.readFile(path.join(root, diagram.path));
    assert.equal(createHash('sha256').update(bytes).digest('hex'), diagram.sha256);
    const served = await fs.readFile(path.join(publicDir, path.basename(diagram.path)));
    assert.equal(createHash('sha256').update(served).digest('hex'), diagram.sha256);
  }
});
test('public manifests contain no local user paths or private capture roots', async () => {
  const manifest = await fs.readFile(path.join(tooling, 'asset-provenance.json'), 'utf8');
  assert.doesNotMatch(manifest, /[A-Za-z]:[\\/]|Users[\\/]|Mario Caesar|019c8af3-/);
});
test('business verdicts and historical provider labels are explicit', () => {
  assert.ok(story.some((scene) => scene.kind === 'blocked'));
  assert.ok(story.some((scene) => scene.kind === 'pass'));
  assert.ok(story.some((scene) => scene.kind === 'approval'));
  assert.match(story.find((scene) => scene.asset === 'gemini-report-summary').eyebrow, /RECORDED GEMINI/);
  assert.match(story.find((scene) => scene.start === 156).summary, /did not repair data/);
});
