import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {root} from './paths.mjs';

const diagrams = [
  {name: 'architecture.svg', source: 'tools/demo-video/recorded-diagrams/architecture.svg',
    sha256: '494a72260ac466a6faffd31dc651187aa19b4b682660ff386ef00aafe53e8a30',
    recordedFrom: 'HEAD:docs/diagrams/architecture.svg', videoRegions: [[40,184,610,145],[688,184,632,145]]},
  {name: 'data-model-overview.svg', source: 'tools/demo-video/recorded-diagrams/data-model-overview.svg',
    sha256: '1b8d16d20e91f3a75f5462d9dd86ab22d72ffef6a1de1bfbd4207dbd7e2800da',
    recordedFrom: 'HEAD:docs/diagrams/data-model-overview.svg', videoRegions: [[35,184,748,215],[815,184,431,215]]},
];

export async function prepareRecordedDiagrams(destination) {
  // Validate every frozen source before publishing any file.
  const verified = await Promise.all(diagrams.map(async (diagram) => {
    const bytes = await fs.readFile(path.join(root, diagram.source));
    if (/<script\b/i.test(bytes.toString())) throw new Error('Scripted SVG not permitted');
    if (createHash('sha256').update(bytes).digest('hex') !== diagram.sha256) {
      throw new Error(`Recorded diagram hash changed: ${diagram.source}`);
    }
    return {diagram, bytes};
  }));
  await fs.mkdir(destination, {recursive: true});
  for (const {diagram, bytes} of verified) {
    await fs.writeFile(path.join(destination, diagram.name), bytes);
  }
  return diagrams.map((diagram) => ({
    path: diagram.source, sha256: diagram.sha256, recordedFrom: diagram.recordedFrom,
    label: 'Recorded repository diagram; predates evidence-driven recovery; not a UI capture',
    videoRegions: diagram.videoRegions,
    treatment: 'Frozen recorded SVG bytes; guided detail views preserve original labels.',
  }));
}
