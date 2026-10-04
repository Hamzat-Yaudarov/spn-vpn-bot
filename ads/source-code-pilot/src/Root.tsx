import React from 'react';
import {Composition} from 'remotion';
import {Film, Cover} from './Video';
import './style.css';

export const Root: React.FC = () => <>
  <Composition id="SourceCode" component={Film} durationInFrames={1200} fps={30} width={1080} height={1920}/>
  <Composition id="Cover" component={Cover} durationInFrames={1} fps={30} width={1080} height={1920}/>
</>;
