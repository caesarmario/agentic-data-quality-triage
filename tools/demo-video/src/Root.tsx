import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {Demo} from './Video';

const Root: React.FC = () => <Composition id="DataSentryDemo" component={Demo}
  width={1280} height={720} fps={30} durationInFrames={5400} />;

registerRoot(Root);
