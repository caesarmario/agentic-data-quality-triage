# DataSentry Recorded Demo Tooling

Isolated Remotion source for a 180-second, 1280x720, 30 fps silent English-captioned walkthrough. This is a documentary composition, not a simulated application or a continuous autonomous repair recording.

See [the delivery and regeneration guide](../../docs/demo-video.md) for chapter timing, privacy treatment, media checks and limitations.

## Files

- `src/Root.tsx`: fixed composition contract.
- `src/Video.tsx`: editorial layout and deterministic frame-driven motion.
- `src/story.json`: chapter scenes, evidence labels and narrative.
- `src/captions.json`: source for burned-in English captions and WebVTT.
- `captions.en.vtt`: generated sidecar captions.
- `asset-provenance.json`: relative asset sources, crop/mask coordinates and SHA-256 hashes.
- `evidence-manifest.json`: documentary claims, source sections and retained run identifiers.
- `media-verification.json`: measured output contract and artifact hash.
- `scripts/`: asset preparation, local source checks, rendering and media verification.

## Recorded Diagram Provenance

The existing walkthrough is a recorded documentary composition and predates the evidence-driven recovery workflow. Its architecture and data-model views are frozen copies of the original `HEAD` SVGs in `recorded-diagrams/`; their recorded SHA-256 values remain the provenance boundary. They are intentionally separate from the regenerated current diagrams in `docs/diagrams/`.

`npm run prepare:assets` verifies the pinned hashes and copies these archived SVGs into the Remotion public assets. It must not substitute current repository diagrams or imply that recovery behavior, controls, or runtime were present in the recorded video. The existing movie and its public URL are unchanged.

## Local Commands

From this directory, with Node.js 22+ and npm:

```powershell
npm.cmd ci --cache .cache/npm --no-fund --no-audit
npm.cmd run prepare:assets
npm.cmd run check
npm.cmd run frames
npm.cmd run render
npm.cmd run verify
```

The committed sanitized crops suffice for regeneration; no private capture directory is required. Original recropping is opt-in with `--captures` as documented in the guide. Never copy the raw captures into this project.

`node_modules/`, `.cache/` and `out/` are ignored. The MP4 is written to ignored `data/acceptance/repository-refresh/datasentry-demo.mp4`. Remotion's internal headless renderer paints only this local composition; these commands do not automate Discord/Streamlit, start application services, invoke provider APIs or trigger DAGs.
