import fs from 'node:fs/promises';
import path from 'node:path';
import {tooling} from './paths.mjs';

export const timecode = (seconds) => {
  const milliseconds = Math.round(seconds * 1000);
  return `${String(Math.floor(milliseconds / 3600000)).padStart(2, '0')}:${String(Math.floor(milliseconds / 60000) % 60).padStart(2, '0')}:${String(Math.floor(milliseconds / 1000) % 60).padStart(2, '0')}.${String(milliseconds % 1000).padStart(3, '0')}`;
};

export async function writeCaptions() {
  const cues = JSON.parse(await fs.readFile(path.join(tooling, 'src/captions.json'), 'utf8'));
  const vtt = 'WEBVTT\n\n' + cues.map((cue, i) => `${i + 1}\n${timecode(cue.start)} --> ${timecode(cue.end)}\n${cue.text}\n`).join('\n');
  await fs.writeFile(path.join(tooling, 'captions.en.vtt'), vtt);
}
