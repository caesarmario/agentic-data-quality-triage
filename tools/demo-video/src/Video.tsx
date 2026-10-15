import React from 'react';
import {AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame} from 'remotion';
import story from './story.json';
import captions from './captions.json';

const ink = '#142b25';
const green = '#087b61';
const paper = '#f3f0e7';
const chapters = ['Problem', 'Pipeline', 'Discord', 'Evidence', 'Lineage + ERD', 'Guardrails', 'Quickstart'];
type Scene = (typeof story)[number];

const DiagramDetail: React.FC<{asset: string; seconds: number}> = ({asset, seconds}) => {
  const architecture = asset === 'architecture.svg';
  const first = seconds < (architecture ? 26 : 125);
  const [x, y, width, height] = architecture
    ? (first ? [40, 184, 610, 145] : [688, 184, 632, 145])
    : (first ? [35, 184, 748, 215] : [815, 184, 431, 215]);
  const scale = Math.min(1170 / width, 310 / height);
  const originalWidth = architecture ? 1360 : 1280;
  const originalHeight = architecture ? 1080 : 840;
  const label = architecture ? (first ? 'DETAIL 1 / SYNTHETIC INPUT + S3 LANDING' : 'DETAIL 2 / WAREHOUSE TRANSFORM + DETERMINISTIC CHECKS')
    : (first ? 'DETAIL 1 / CLICKHOUSE: WAREHOUSE + QUALITY EVIDENCE' : 'DETAIL 2 / POSTGRESQL: TRANSACTIONAL CONTROL');
  return <div style={{backgroundColor: '#f7f8f2', height: 368, padding: 10, border: '1px solid #c6d5cc'}}>
    <div style={{fontSize: 17, color: green, letterSpacing: 1, marginBottom: 13}}>{label}</div>
    <div style={{width: width * scale, height: height * scale, margin: '0 auto', position: 'relative', overflow: 'hidden'}}>
      <Img src={staticFile(asset)} style={{position: 'absolute', maxWidth: 'none', width: originalWidth * scale, height: originalHeight * scale, left: -x * scale, top: -y * scale}} />
    </div>
  </div>;
};

const Terminal: React.FC<{lines: string[]; small?: boolean}> = ({lines, small}) => (
  <div style={{background: ink, color: '#e1eee3', padding: '23px 26px', borderLeft: `5px solid ${green}`, fontFamily: 'Consolas, monospace', fontSize: small ? 19 : 24, lineHeight: 1.75, whiteSpace: 'pre-wrap'}}>
    {lines.map((line, i) => <div key={i}>{line || '\u00a0'}</div>)}
  </div>
);

const Documentary: React.FC<{scene: Scene}> = ({scene}) => {
  const seconds = useCurrentFrame() / 30;
  if (scene.kind === 'opening') return <div style={{marginTop: 52}}>
    <div style={{fontFamily: 'Georgia, serif', fontSize: 78, letterSpacing: -2, lineHeight: 1.08, whiteSpace: 'pre-line', maxWidth: 1080}}>{scene.title}</div>
    <p style={{fontSize: 27, lineHeight: 1.5, whiteSpace: 'pre-line', color: '#456157', marginTop: 32}}>{scene.summary}</p>
    <div style={{marginTop: 24, color: green, fontSize: 18, letterSpacing: 2}}>EVIDENCE / CONTEXT / DECISION</div>
  </div>;
  if (scene.kind === 'diagram') return <>
    <h1 style={{fontFamily: 'Georgia, serif', fontWeight: 400, fontSize: 38, margin: '17px 0 6px'}}>{scene.title}</h1>
    <div style={{fontSize: 20, color: '#456157', marginBottom: 12}}>{scene.summary}</div>
    <DiagramDetail asset={scene.asset!} seconds={seconds} />
  </>;
  if (scene.kind === 'wide-ui') return <>
    <h1 style={{fontFamily: 'Georgia, serif', fontWeight: 400, fontSize: 38, margin: '17px 0 8px'}}>{scene.title}</h1>
    <div style={{fontSize: 21, color: '#456157', marginBottom: 14}}>{scene.summary}</div>
    <div style={{height: 350, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 8}}>
      <Img src={staticFile(`demo-refresh-${scene.asset}.png`)} style={{width: '100%', height: scene.extraAsset ? 280 : 340, objectFit: 'contain'}} />
      {scene.extraAsset && <Img src={staticFile(`demo-refresh-${scene.extraAsset}.png`)} style={{width: '100%', height: 45, objectFit: 'contain'}} />}
    </div>
  </>;
  if (scene.kind === 'commands') return <>
    <h1 style={{fontFamily: 'Georgia, serif', fontSize: 43, fontWeight: 400, margin: '22px 0 12px'}}>Reproduce locally. Inspect the retained run.</h1>
    <div style={{fontSize: 21, color: '#456157', marginBottom: 18}}>From the repository root, after reviewing infra/.env. Commands shown, not executed.</div>
    <Terminal lines={[
      'docker compose --env-file infra/.env -f infra/docker-compose.yml config --quiet',
      'docker compose --env-file infra/.env -f infra/docker-compose.yml build dq-runner',
      'docker compose --env-file infra/.env -f infra/docker-compose.yml up -d',
      '', 'Airflow :8080  /  Streamlit :8501  /  DAG 91 for application acceptance',
    ]} small />
  </>;
  const hasImage = scene.kind === 'ui';
  return <div style={{display: 'flex', gap: 42, marginTop: 24, height: 447}}>
    <div style={{width: hasImage ? 409 : 535, flexShrink: 0}}>
      <h1 style={{fontFamily: 'Georgia, serif', fontSize: hasImage ? 43 : 48, fontWeight: 400, letterSpacing: -1, whiteSpace: 'pre-line', margin: '0 0 24px', lineHeight: 1.1}}>{scene.title}</h1>
      <div style={{fontSize: 23, lineHeight: 1.48, color: '#456157'}}>{scene.summary}</div>
    </div>
    <div style={{flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', justifyContent: 'center'}}>
      {hasImage ? <Img src={staticFile(`demo-refresh-${scene.asset}.png`)} style={{width: '100%', height: '100%', objectFit: 'contain'}} /> : <EvidencePanel kind={scene.kind} />}
    </div>
  </div>;
};

const EvidencePanel: React.FC<{kind: string}> = ({kind}) => {
  if (kind === 'pipeline') return <Terminal lines={['01 / Airflow orchestrates', '02 / S3 + ClickHouse retain facts', '03 / dbt + Python check quality', '04 / Guarded tools investigate', '05 / Human decision is separate']} small />;
  if (kind === 'lineage') return <div style={{borderLeft: `3px solid ${green}`, paddingLeft: 30}}>
    {['dq.raw_orders', 'stg_orders', 'fct_orders_daily'].map((text, i) => <React.Fragment key={text}>
      {i > 0 && <div style={{fontSize: 26, color: green, padding: '12px 0'}}>↓</div>}
      <div style={{fontFamily: 'Consolas, monospace', fontSize: 28}}>{text}</div>
    </React.Fragment>)}
    <p style={{fontSize: 18, color: '#456157'}}>Source: warehouse/dbt models and repository diagram</p>
  </div>;
  if (kind === 'report') return <Terminal lines={['RETAINED REPORT', 'RPT-966426C2 / 2026-10-06', '', '3 Gemini calls / no fallback', 'Approval: recorded', 'Execution: not_started']} small />;
  if (kind === 'approval') return <Terminal lines={['RECORDED DECISIONS / 2026-10-08', '', 'APPROVED -> not_started', 'REJECTED -> not_started', '', 'No remediation DagRun dispatched']} small />;
  if (kind === 'blocked' || kind === 'pass') {
    const blocked = kind === 'blocked';
    return <div style={{borderTop: `6px solid ${blocked ? '#af542e' : green}`, padding: '24px 0'}}>
      <div style={{color: blocked ? '#af542e' : green, fontSize: 22, letterSpacing: 2}}>ACCEPTANCE RECORD / {blocked ? 'BLOCKED' : 'PASS'}</div>
      <div style={{fontFamily: 'Georgia, serif', fontSize: 83, margin: '15px 0'}}>{blocked ? '24' : '25'} <span style={{color: '#6f8378', fontSize: 45}}>/ 25</span></div>
      <div style={{fontSize: 22, lineHeight: 1.6}}>{blocked ? 'Orchestration: success\nBusiness result: blocked' : 'Read-only verification\nReason + confirmation still required'}</div>
      <div style={{fontFamily: 'Consolas, monospace', fontSize: 14, marginTop: 22, color: '#456157'}}>request: {blocked ? 'c0e73abd-3875-4f30-8da9-0805bc004f02' : '021417a4-5374-41c2-89fe-05d1b92c1bb5'}</div>
    </div>;
  }
  if (kind === 'quickstart') return <Terminal lines={['Create infra/.env only if absent.', 'Review credentials locally.', '', 'EXTERNAL_LLM_ENABLED=false', 'COPILOT_EXTERNAL_LLM_ENABLED=false', '', 'Provider keys: blank for first run']} small />;
  if (kind === 'commands') return <Terminal lines={['Follow README local setup:', '', '1. Validate Compose configuration', '2. Build dq-runner; start stack', '3. Check init logs and task state', '', 'Airflow    localhost:8080', 'Streamlit  localhost:8501']} small />;
  return null;
};

export const Demo: React.FC = () => {
  const frame = useCurrentFrame();
  const seconds = frame / 30;
  const scene = story.find((item) => seconds >= item.start && seconds < item.end) ?? story[story.length - 1];
  const cue = captions.find((item) => seconds >= item.start && seconds < item.end);
  // A small deterministic chapter entrance avoids a black or unreadable first frame.
  const local = frame - scene.start * 30;
  const translateY = interpolate(local, [0, 15], [5, 0], {extrapolateRight: 'clamp'});
  return <AbsoluteFill style={{backgroundColor: paper, color: ink, fontFamily: 'Trebuchet MS, sans-serif'}}>
    <div style={{position: 'absolute', inset: 0, backgroundImage: 'radial-gradient(#54766924 0.7px, transparent 0.7px)', backgroundSize: '17px 17px', opacity: 0.3}} />
    <div style={{height: 7, backgroundColor: '#d5dfd6'}}><div style={{height: '100%', width: `${seconds / 180 * 100}%`, background: green}} /></div>
    <div style={{padding: '24px 44px 0', position: 'relative'}}>
      <div style={{display: 'flex', gap: 22, borderBottom: '1px solid #c8d4cb', paddingBottom: 18, fontSize: 15}}>
        <div style={{fontWeight: 700, color: green, marginRight: 13, letterSpacing: 1.8}}>DATASENTRY</div>
        {chapters.map((chapter, index) => <div key={chapter} style={{color: index === scene.chapter ? ink : '#65796e', fontWeight: index === scene.chapter ? 700 : 400}}>{String(index + 1).padStart(2, '0')} {chapter}</div>)}
        <div style={{marginLeft: 'auto', color: '#65796e'}}>SILENT / 03:00</div>
      </div>
      <div style={{fontSize: 15, letterSpacing: 2, color: green, marginTop: 22}}>{scene.eyebrow}</div>
      <div style={{transform: `translateY(${translateY}px)`}}><Documentary scene={scene} /></div>
    </div>
    <div style={{position: 'absolute', left: 44, right: 44, bottom: 103, fontSize: 14, color: '#456157', borderTop: '1px solid #c8d4cb', paddingTop: 10}}>{scene.tag}</div>
    <div style={{position: 'absolute', left: 0, right: 0, bottom: 0, height: 88, backgroundColor: ink, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 70px', color: '#f7f4eb', fontSize: 25, lineHeight: 1.3, textAlign: 'center'}}>{cue?.text}</div>
  </AbsoluteFill>;
};
