import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import sharp from 'sharp';
import {root, tooling, publicDir} from './paths.mjs';

// Coordinates refer to inspected originals; never copy raw private captures.
export const captures = [
  {id: 'discord-inbox', source: 'discord-new-server-live-20261014.jpg', crop: [455, 468, 521, 545]},
  {id: 'help', source: 'datasentry-help-english-desktop.jpg', crop: [455, 520, 484, 431]},
  {id: 'resolved', source: 'discord-resolved-dq-20261012.jpg', crop: [456, 293, 520, 531], mask: [12, 337, 230, 17]},
  {id: 'workspace', source: 'discord-grounded-followup-20261012.jpg', crop: [456, 669, 328, 202]},
  {id: 'gemini-answer', source: 'discord-grounded-followup-20261012.jpg', crop: [918, 560, 354, 340]},
  {id: 'gemini-investigation', source: 'discord-grounded-followup-20261012.jpg', crop: [456, 190, 328, 332]},
  {id: 'gemini-report-summary', source: 'streamlit-recorded-gemini-investigation-20261015.jpg', crop: [399, 149, 775, 187]},
  {id: 'gemini-report-reasoning', source: 'streamlit-recorded-gemini-recommendation-20261015.jpg', crop: [399, 60, 775, 158]},
  {id: 'gemini-report-recommendation', source: 'streamlit-recorded-gemini-provenance-20261015.jpg', crop: [399, 117, 775, 204]},
  {id: 'gemini-report-provenance', source: 'streamlit-recorded-gemini-provenance-20261015.jpg', crop: [399, 458, 775, 35]},
  {id: 'streamlit-light', source: 'streamlit-english-light-20261013.jpg', crop: [379, 177, 890, 339]},
  {id: 'streamlit-dark', source: 'streamlit-english-dark-20261013.jpg', crop: [379, 177, 890, 339]},
  {id: 'incident-queue', source: 'streamlit-english-light-20261013.jpg', crop: [379, 540, 890, 540]},
];

const recordedDiagrams = [
  {name: 'architecture.svg', source: 'tools/demo-video/recorded-diagrams/architecture.svg',
    sha256: '494a72260ac466a6faffd31dc651187aa19b4b682660ff386ef00aafe53e8a30',
    recordedFrom: 'HEAD:docs/diagrams/architecture.svg', videoRegions: [[40,184,610,145],[688,184,632,145]]},
  {name: 'data-model-overview.svg', source: 'tools/demo-video/recorded-diagrams/data-model-overview.svg',
    sha256: '1b8d16d20e91f3a75f5462d9dd86ab22d72ffef6a1de1bfbd4207dbd7e2800da',
    recordedFrom: 'HEAD:docs/diagrams/data-model-overview.svg', videoRegions: [[35,184,748,215],[815,184,431,215]]},
];

export const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const captureRoot = process.argv.find((arg) => arg.startsWith('--captures='))?.slice(11);
const manifestPath = path.join(tooling, 'asset-provenance.json');
let previous = null;
try { previous = JSON.parse(await fs.readFile(manifestPath, 'utf8')); } catch {}
await fs.mkdir(publicDir, {recursive: true});
const assets = [];
for (const entry of captures) {
  const name = `demo-refresh-${entry.id}.png`;
  const destination = path.join(root, 'docs/images', name);
  let sourceSha256 = previous?.assets.find((item) => item.id === entry.id)?.sourceSha256;
  if (captureRoot) {
    const source = await fs.readFile(path.join(captureRoot, entry.source));
    sourceSha256 = hash(source);
    const [left, top, width, height] = entry.crop;
    let cropped = sharp(source).extract({left, top, width, height});
    if (entry.mask) {
      const [x, y, w, h] = entry.mask;
      const redaction = Buffer.from(`<svg width="${w}" height="${h}"><rect width="100%" height="100%" fill="#101010"/><text x="2" y="12" font-family="sans-serif" font-size="11" fill="#b6b6b6">[operator redacted]</text></svg>`);
      // Materialize the crop so composite offsets apply to cropped coordinates.
      cropped = sharp(await cropped.png().toBuffer()).composite([{input: redaction, left: x, top: y}]);
    }
    await cropped.png({compressionLevel: 9}).toFile(destination);
  }
  const bytes = await fs.readFile(destination);
  const metadata = await sharp(bytes).metadata();
  if (!sourceSha256) throw new Error(`Missing original provenance for ${name}; supply --captures=...`);
  await fs.writeFile(path.join(publicDir, name), bytes);
  assets.push({...entry, sourceSha256, path: `docs/images/${name}`, sha256: hash(bytes), width: metadata.width, height: metadata.height,
    treatment: entry.mask ? 'Crop; opaque operator-name redaction only. Evidence unchanged.' : 'Crop only. Original evidence text preserved.',
    label: entry.id.startsWith('gemini-') ? 'Recorded Gemini analysis' : 'Recorded UI',
    inspectedBeforeUse: true});
}
const diagrams = [];
for (const diagram of recordedDiagrams) {
  if (process.argv.includes('--captures-only')) continue;
  const bytes = await fs.readFile(path.join(root, diagram.source));
  if (bytes.toString().includes('<script')) throw new Error('Scripted SVG not permitted');
  const actual = hash(bytes);
  if (actual !== diagram.sha256) throw new Error(`Recorded diagram hash changed: ${diagram.source}`);
  await fs.writeFile(path.join(publicDir, diagram.name), bytes);
  diagrams.push({path: diagram.source, sha256: actual, recordedFrom: diagram.recordedFrom,
    label: 'Recorded repository diagram; predates evidence-driven recovery; not a UI capture',
    videoRegions: diagram.videoRegions,
    treatment: 'Frozen recorded SVG bytes; guided detail views preserve original labels.'});
}
await fs.writeFile(manifestPath, JSON.stringify({
  schemaVersion: 1,
  policy: 'Actual retained UI only. No mockups, synthesized UI, new provider call, or source repair. Cross-session evidence is not one autonomous run.',
  privateSource: 'Owner-retained visualization directory, supplied via --captures; originals intentionally excluded.',
  assets, diagrams,
  documentarySources: ['docs/gemini_triage_acceptance.md', 'docs/discord_workspace_acceptance.md', 'docs/incident_resolution.md'],
}, null, 2) + '\n');
console.log(`Prepared ${assets.length} sanitized captures and ${diagrams.length} repository diagrams.`);
