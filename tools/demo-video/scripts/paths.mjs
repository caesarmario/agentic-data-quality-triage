import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const tooling = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const root = path.resolve(tooling, '../..');
export const cache = path.join(tooling, '.cache');
export const output = path.join(root, 'data/acceptance/repository-refresh/datasentry-demo.mp4');
export const publicDir = path.join(cache, 'public');
export const fps = 30;
export const durationInFrames = 5400;
export const chapterBounds = [0, 20, 45, 75, 110, 130, 165, 180];
export const qaFrames = [...new Set(chapterBounds.slice(0, -1).flatMap((start, i) => [
  start * fps,
  Math.floor((start + chapterBounds[i + 1]) / 2 * fps),
  chapterBounds[i + 1] * fps - 1,
]))];
