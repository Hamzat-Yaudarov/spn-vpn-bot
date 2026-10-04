import React, {useEffect, useState} from 'react';
import {
  AbsoluteFill, Audio, Sequence, continueRender, delayRender,
  interpolate, spring, staticFile, useCurrentFrame,
} from 'remotion';
import timings from '../public/captions.json';
import {codeLines, sharpX, smoothX} from './motion';

const C = {ink: '#111419', milk: '#F2F0E9', blue: '#3265FF', pale: '#B6C8FF', gray: '#828993'};
const FONT = 'Manrope, sans-serif';
const MONO = 'JetBrains Mono, monospace';
const clamp = (n: number, a = 0, b = 1) => Math.min(b, Math.max(a, n));
const tween = (f: number, a: number, b: number, x = 0, y = 1) =>
  interpolate(f, [a, b], [x, y], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
const ease = (n: number) => 1 - (1 - clamp(n)) ** 3;
const pop = (f: number, delay = 0, damping = 18) => spring({frame: f - delay, fps: 30, config: {damping, stiffness: 125, mass: .85}});
const appear = (f: number, delay = 0) => ({opacity: tween(f, delay, delay + 10), transform: `translateY(${(1 - pop(f, delay)) * 48}px)`});

const FontReady: React.FC = () => {
  const [handle] = useState(() => delayRender('Load bundled Cyrillic fonts'));
  useEffect(() => {
    Promise.all([
      document.fonts.load('800 100px Manrope', 'Исходный код'),
      document.fonts.load('600 48px Manrope', 'Текст'),
      document.fonts.load('400 30px "JetBrains Mono"', 'interpolate'),
    ]).then(() => document.fonts.ready).then(() => continueRender(handle));
  }, [handle]);
  return null;
};

const Grid: React.FC<{dark?: boolean; f: number}> = ({dark = true, f}) => <AbsoluteFill style={{
  opacity: dark ? .10 : .17,
  backgroundImage: `linear-gradient(${dark ? '#8090b6' : '#adb5c1'} 1px, transparent 1px), linear-gradient(90deg, ${dark ? '#8090b6' : '#adb5c1'} 1px, transparent 1px)`,
  backgroundSize: '88px 88px',
  transform: `perspective(1000px) rotateX(55deg) scale(1.8) translateY(${50 + f * .1}px)`,
  maskImage: 'linear-gradient(transparent 10%, black 55%, transparent 95%)',
}}/>;

const Topbar: React.FC<{f: number; light: boolean}> = ({f, light}) => {
  const fg = light ? C.ink : C.milk;
  return <div style={{position: 'absolute', left: 84, right: 104, top: 134, color: fg}}>
    <div style={{display: 'flex', alignItems: 'center', gap: 17, fontSize: 22, fontWeight: 800, letterSpacing: 3}}>
      <div style={{height: 12, width: 12, borderRadius: 4, background: C.blue}}/>
      <span>КОД В КАДРЕ</span>
      <span style={{marginLeft: 'auto', fontFamily: MONO, fontWeight: 400, fontSize: 19, opacity: .5}}>ЭКСПЕРИМЕНТ / 01</span>
    </div>
    <div style={{display: 'flex', gap: 7, marginTop: 24}}>
      {[120, 240, 240, 300, 300].map((d, i, all) => {
        const start = all.slice(0, i).reduce((a, b) => a+b, 0);
        return <div key={i} style={{height: 3, flex: d, background: light ? '#12151A18' : '#ffffff20'}}>
          <div style={{width: `${clamp((f-start)/d)*100}%`, height: '100%', background: C.blue}}/>
        </div>;
      })}
    </div>
  </div>;
};

const Pill: React.FC<{children: React.ReactNode; dark?: boolean; style?: React.CSSProperties}> = ({children, dark, style}) =>
  <div style={{display: 'inline-flex', alignItems: 'center', gap: 12, padding: '13px 20px', borderRadius: 14,
    fontFamily: MONO, fontSize: 24, color: dark ? C.ink : C.milk,
    border: `1px solid ${dark ? '#11141920' : '#ffffff25'}`, background: dark ? '#ffffffaa' : '#20252edd', ...style}}>{children}</div>;

const Arrow: React.FC<{color?: string; size?: number}> = ({color = C.blue, size = 50}) =>
  <svg width={size} height={size} viewBox="0 0 64 64" fill="none"><path d="M10 32h42M35 15l17 17-17 17" stroke={color} strokeWidth="5" strokeLinecap="round" strokeLinejoin="round"/></svg>;

const FileIcon: React.FC<{accent?: string; small?: boolean}> = ({accent = C.blue, small}) => <div style={{
  width: small ? 78 : 148, height: small ? 96 : 178, background: accent, borderRadius: small ? 17 : 28,
  display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'white',
  fontFamily: MONO, fontSize: small ? 34 : 65, fontWeight: 600,
  boxShadow: `8px 9px 0 ${accent}55, 18px 22px 34px #00000030`, position: 'relative',
}}><span style={{transform: 'translateY(5px)'}}>{'{ }'}</span><div style={{position: 'absolute', top: 0, right: 0, width: small ? 20 : 38, height: small ? 20 : 38, background: '#ffffff45', borderBottomLeftRadius: 9}}/></div>;

const Hook: React.FC<{still?: boolean}> = ({still = false}) => {
  const frame = useCurrentFrame();
  const f = still ? 90 : frame;
  const camera = tween(f, 0, 100, 1.10, 1);
  return <AbsoluteFill style={{background: C.ink, color: C.milk, overflow: 'hidden'}}>
    <Grid f={f}/>
    <div style={{position: 'absolute', width: 1000, height: 1000, left: 40, top: 490,
      background: 'radial-gradient(ellipse, #3265FF38 0%, #3265FF0a 40%, transparent 68%)'}}/>
    <div style={{position: 'absolute', top: 285, left: 84, width: 890, transformOrigin: '50% 60%', transform: `scale(${camera})`}}>
      <div style={{fontSize: 87, fontWeight: 800, lineHeight: 1.1, letterSpacing: -5, ...appear(f, 0)}}>У этого видео</div>
      <div style={{fontSize: 87, fontWeight: 800, lineHeight: 1.1, letterSpacing: -5, marginTop: 12, ...appear(f, 7)}}>есть исходный</div>
      <div style={{fontSize: 184, fontWeight: 800, lineHeight: 1.03, letterSpacing: -10, color: C.blue, marginTop: 2,
        display: 'flex'}}>{'код.'.split('').map((char, i) => <span key={i} style={{display: 'inline-block', opacity: tween(f, 14+i*3, 21+i*3),
          transform: `translateY(${(1-pop(f, 14+i*3))*110}px) rotate(${(1-pop(f, 14+i*3))*12}deg)`}}>{char}</span>)}</div>
    </div>
    <div style={{position: 'absolute', left: 145, top: 902, width: 720, height: 395,
      transform: `perspective(1600px) rotateY(${-13+tween(f, 0, 100, 0, 5)}deg) rotateX(9deg) rotateZ(-5deg) translateY(${(1-pop(f, 22))*240 + Math.sin(f/24)*7}px)`,
      opacity: tween(f, 16, 29), border: '2px solid #ffffff29', borderRadius: 38,
      background: 'linear-gradient(130deg, #333d51, #1d2330 60%)', boxShadow: '0 48px 100px #00000070, 14px 14px 0 #0b0e13, 15px 15px 0 #ffffff30', padding: 42}}>
      <div style={{display: 'flex', gap: 10}}>{[0,1,2].map(i => <div key={i} style={{height: 12, width: 12, borderRadius: 8, background: i === 0 ? C.blue : '#556074'}}/>)}
        <span style={{marginLeft: 'auto', color: '#a5aec0', fontFamily: MONO, fontSize: 21}}>video.tsx</span></div>
      <div style={{display: 'flex', alignItems: 'center', gap: 42, marginTop: 44}}><FileIcon/>
        <div style={{fontFamily: MONO, fontSize: 37, lineHeight: 1.75}}><span style={{color: '#789bff'}}>&lt;Video</span><br/><span style={{color: '#c0c9dc'}}>  fps=</span><span style={{color: '#ffffff'}}>{'{30}'}</span><br/><span style={{color: '#789bff'}}> /&gt;</span></div>
      </div>
    </div>
    <Pill style={{position: 'absolute', left: 611, top: 1272, transform: `rotate(5deg) scale(${pop(f, 42)})`, color: C.ink, background: C.milk, border: 'none', boxShadow: '0 16px 50px #0004'}}>
      <span style={{height: 9, width: 9, borderRadius: '50%', background: C.blue}}/>1080 × 1920
    </Pill>
    <div style={{position: 'absolute', left: 88, top: 1377, color: '#939dad', fontSize: 25, letterSpacing: 1, ...appear(f, 48)}}>И ты сейчас на него смотришь.</div>
  </AbsoluteFill>;
};

const Layers: React.FC = () => {
  const f = useCurrentFrame();
  const expand = ease(tween(f, 5, 70));
  const layers = [
    {name: 'ФОН', code: 'background', color: '#E5E2D9', ink: '#7b7971'},
    {name: 'ОБЪЕКТЫ', code: 'objects', color: '#CED9FF', ink: C.blue},
    {name: 'ЗАГОЛОВОК', code: 'title', color: '#3265FF', ink: 'white'},
    {name: 'СУБТИТРЫ', code: 'captions', color: '#111419', ink: 'white'},
  ];
  const selected = f < 51 ? 0 : f < 90 ? 1 : f < 127 ? 2 : 3;
  return <AbsoluteFill style={{background: C.milk, color: C.ink, overflow: 'hidden'}}>
    <Grid dark={false} f={f}/>
    <div style={{position: 'absolute', left: 84, top: 274, fontSize: 98, lineHeight: 1.08, letterSpacing: -5, fontWeight: 800, ...appear(f)}}>
      Одна сцена.<br/><span style={{color: C.blue}}>Четыре слоя.</span>
    </div>
    <div style={{position: 'absolute', left: 88, top: 525, fontSize: 29, color: '#777b85', ...appear(f, 10)}}>Каждый — под твоим контролем.</div>
    {layers.map((layer, i) => {
      const active = i === selected;
      const y = 815 + i*24 + (i-1.5)*154*expand;
      const drift = f > 160 && i === 2 ? Math.sin((f-160)/25)*17 : 0;
      return <React.Fragment key={layer.name}>
        <div style={{position: 'absolute', left: 155+drift, top: y, width: 635, height: 248,
          background: layer.color, borderRadius: 25, border: `2px solid ${i === 0 ? '#c8c5bd' : '#ffffff38'}`,
          transform: `perspective(1600px) rotateX(47deg) rotateZ(-9deg) translateX(${(1-pop(f, i*5))*-120}px)`,
          transformOrigin: '50% 50%', boxShadow: `0 ${active ? 26 : 12}px ${active ? 60 : 35}px #10193022, 0 10px 0 ${i === 3 ? '#05070c' : i === 2 ? '#1546d3' : i === 1 ? '#96adee' : '#c9c5bb'}`,
          opacity: tween(f, i*3, 13+i*3), color: layer.ink, overflow: 'hidden'}}>
          {i === 0 && <div style={{position: 'absolute', inset: 0, backgroundImage: 'radial-gradient(#acaaa1 2px, transparent 2px)', backgroundSize: '25px 25px'}}/>}
          {i === 1 && <div style={{display: 'flex', gap: 27, padding: '47px 52px'}}>{[0,1,2].map(j => <div key={j} style={{width: 146, height: 138, background: j===1 ? C.blue : '#fff9', borderRadius: 24, boxShadow: '9px 10px 0 #7893d340'}}/>)}</div>}
          {i === 2 && <div style={{fontSize: 74, fontWeight: 800, letterSpacing: -4, padding: '49px 45px'}}>Исходный код</div>}
          {i === 3 && <div style={{display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 12, height: '100%', fontSize: 47, fontWeight: 800}}>Это <span style={{background: C.blue, borderRadius: 13, padding: '7px 15px'}}>можно</span> изменить</div>}
        </div>
        <div style={{position: 'absolute', right: 90, top: y+77, color: active ? C.blue : '#747985', fontFamily: MONO, fontSize: 19,
          opacity: expand, transform: `translateX(${(1-expand)*50}px)`, textAlign: 'right'}}>
          <div style={{fontSize: 15, opacity: .5, marginBottom: 7}}>0{i+1}</div>
          <div style={{background: active ? '#ffffff' : '#f2f0e9d9', padding: '10px 13px', borderRadius: 10, border: `1px solid ${active ? '#3265ff44' : '#11141911'}`}}>{layer.name}</div>
        </div>
      </React.Fragment>;
    })}
    <Pill dark style={{position: 'absolute', left: 85, top: 1375, fontSize: 23, ...appear(f, 155)}}>
      <span style={{color: C.blue}}>↗</span> меняем один — остальные на месте
    </Pill>
  </AbsoluteFill>;
};

const CodeWindow: React.FC<{f: number; compact?: boolean}> = ({f, compact = false}) => {
  const visible = clamp(Math.floor((f-15)/4), 0, codeLines.length);
  return <div style={{height: '100%', borderRadius: 27, background: '#191f2b', border: '1px solid #ffffff20', boxShadow: '0 28px 65px #0005', overflow: 'hidden'}}>
    <div style={{height: 65, borderBottom: '1px solid #ffffff12', display: 'flex', alignItems: 'center', padding: '0 27px', gap: 8}}>
      {[0,1,2].map(i => <div key={i} style={{width: 9, height: 9, borderRadius: 8, background: i === 0 ? C.blue : '#4e576a'}}/>)}
      <span style={{fontFamily: MONO, fontSize: 19, marginLeft: 15, color: '#99a6c0'}}>motion.ts</span>
      <span style={{marginLeft: 'auto', color: '#99a6c0', fontFamily: MONO, fontSize: 17}}>REAL SOURCE</span>
    </div>
    <div style={{padding: compact ? '17px 25px' : '22px 28px', fontFamily: MONO, fontSize: compact ? 27 : 31, lineHeight: 1.48}}>
      {codeLines.map((line, i) => <div key={i} style={{whiteSpace: 'pre', display: 'flex', opacity: i < visible ? 1 : .10,
        background: i === 2 || i === 3 ? '#3265ff13' : 'transparent', borderRadius: 6}}>
        <span style={{color: '#586176', display: 'inline-block', width: 47, userSelect: 'none'}}>{i+1}</span>
        <span style={{color: i === 0 ? '#d3ddf5' : i === 1 ? '#d9c694' : i === 2 || i === 3 ? '#89abff' : '#c0cada'}}>{line}</span>
      </div>)}
    </div>
  </div>;
};

const MiniCard: React.FC<{accent?: string; label?: string; width?: number}> = ({accent = C.blue, label = 'КАДР', width = 250}) => <div style={{
  width, height: width*.62, borderRadius: 23, background: accent, padding: 23, color: 'white',
  border: '1px solid #ffffff65', boxShadow: '0 15px 28px #15296230, 0 6px 0 #00000025', display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
}}><div style={{fontFamily: MONO, fontSize: width*.08, opacity: .65}}>{'<Card />'}</div><div style={{fontSize: width*.135, fontWeight: 800, letterSpacing: -1}}>{label}<span style={{float: 'right'}}>↗</span></div></div>;

const Prompt: React.FC = () => {
  const f = useCurrentFrame();
  const p = clamp(((f-128)%76)/45);
  return <AbsoluteFill style={{background: C.ink, color: C.milk, overflow: 'hidden'}}>
    <div style={{position: 'absolute', top: 550, left: -260, width: 1450, height: 1100, background: 'radial-gradient(ellipse, #234eac24, transparent 60%)'}}/>
    <div style={{position: 'absolute', left: 84, top: 280, fontSize: 94, lineHeight: 1.11, letterSpacing: -5, fontWeight: 800, ...appear(f)}}>
      Сначала слова.<br/><span style={{color: '#a8bfff'}}>Потом движение.</span>
    </div>
    <div style={{position: 'absolute', left: 84, right: 104, top: 576, height: 167, padding: '27px 31px',
      background: C.milk, color: C.ink, borderRadius: '26px 26px 26px 7px', ...appear(f, 9)}}>
      <div style={{fontFamily: MONO, fontSize: 18, color: '#7d8390', marginBottom: 12}}>01 / ЗАДАНИЕ</div>
      <div style={{fontSize: 34, lineHeight: 1.3, fontWeight: 700}}>«Пусть карточка плавно<br/>остановится на месте»</div>
    </div>
    <div style={{position: 'absolute', left: 118, top: 748, width: 2, height: 57, background: C.blue, opacity: tween(f, 45, 55)}}/>
    <div style={{position: 'absolute', left: 84, right: 104, top: 798, height: 392, ...appear(f, 47)}}><CodeWindow f={f-45}/></div>
    <div style={{position: 'absolute', left: 84, right: 104, top: 1214, height: 212, border: '1px solid #ffffff25',
      background: '#242a3580', borderRadius: 27, overflow: 'hidden', ...appear(f, 108)}}>
      <div style={{position: 'absolute', left: 27, top: 22, fontFamily: MONO, fontSize: 17, color: '#9aa5b9'}}>03 / ПРЕДПРОСМОТР</div>
      <div style={{position: 'absolute', right: 26, top: 23, fontFamily: MONO, fontSize: 16, color: '#84a7ff'}}>● LIVE</div>
      <div style={{position: 'absolute', left: 332, top: 51, transform: `translateX(${smoothX(p)*.62}px)`}}><MiniCard width={206}/></div>
      <div style={{position: 'absolute', left: 28, bottom: 25, width: 233, height: 5, background: '#ffffff15', borderRadius: 5}}><div style={{height: 5, width: `${clamp(p)*100}%`, background: C.blue, borderRadius: 5}}/></div>
    </div>
  </AbsoluteFill>;
};

const ComparisonTrack: React.FC<{smooth: boolean; f: number; top: number}> = ({smooth, f, top}) => {
  const local = f % 96;
  const progress = clamp((local-8)/40);
  const x = smooth ? smoothX(progress) : sharpX(progress);
  const before = local < 8;
  return <div style={{position: 'absolute', left: 84, right: 104, top, height: 294, borderRadius: 29,
    background: smooth ? '#DFE6FB' : '#E6E3DC', border: `1px solid ${smooth ? '#b4c5f3' : '#d8d4c9'}`, overflow: 'hidden'}}>
    <div style={{position: 'absolute', left: 25, top: 24, fontFamily: MONO, fontSize: 23, color: smooth ? C.blue : '#7c7b78'}}>{smooth ? 'ПОСЛЕ' : 'ДО'}</div>
    <div style={{position: 'absolute', right: 27, top: 24, fontSize: 23, color: smooth ? C.blue : '#7c7b78'}}>{smooth ? 'Мягкая остановка' : 'Резкий скачок'}</div>
    <div style={{position: 'absolute', left: 292, top: 81, height: 145, width: 238, border: `2px dashed ${smooth ? '#7293df88' : '#94908666'}`, borderRadius: 22}}/>
    <div style={{position: 'absolute', left: 293, top: 82, transform: `translateX(${x}px)`, opacity: before ? 0 : 1}}><MiniCard accent={smooth ? C.blue : '#6B7180'} width={235}/></div>
    <svg style={{position: 'absolute', left: 28, bottom: 24}} width="172" height="100" viewBox="0 0 172 100">
      <path d="M4 5V93H170" fill="none" stroke={smooth ? '#a1b5e6' : '#c3beb3'} strokeWidth="2"/>
      <path d={smooth ? 'M4 90C18 23 48 12 167 9' : 'M4 90H47V9H167'} fill="none" stroke={smooth ? C.blue : '#7d8087'} strokeWidth="5" strokeLinecap="round"/>
      <circle cx={4+163*progress} cy={smooth ? 90 - 81*(1-(1-progress)**3) : progress < .26 ? 90 : 9} r="6" fill={smooth ? C.blue : '#7d8087'}/>
    </svg>
    <div style={{position: 'absolute', right: 27, bottom: 24, fontFamily: MONO, fontSize: 18, color: smooth ? '#6281c6' : '#98968f'}}>x: {Math.round(x).toString().padStart(3, '0')}</div>
  </div>;
};

const Motion: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={{background: C.milk, color: C.ink, overflow: 'hidden'}}>
    <div style={{position: 'absolute', left: 84, top: 275, fontSize: 94, lineHeight: 1.1, letterSpacing: -5, fontWeight: 800, ...appear(f)}}>
      Та же карточка.<br/><span style={{color: C.blue}}>Другое ощущение.</span>
    </div>
    <div style={{position: 'absolute', left: 88, top: 525, color: '#7b8089', fontSize: 28, ...appear(f, 10)}}>Меняем только движение.</div>
    <div style={{opacity: tween(f, 5, 17)}}><ComparisonTrack smooth={false} f={f} top={623}/></div>
    <div style={{opacity: tween(f, 25, 38)}}><ComparisonTrack smooth f={f} top={945}/></div>
    <div style={{position: 'absolute', left: 84, right: 104, top: 1270, height: 152, background: C.ink, borderRadius: 23,
      padding: '26px 29px', ...appear(f, 62)}}>
      <div style={{fontFamily: MONO, fontSize: 18, color: '#8190ac', marginBottom: 14}}>ИЗМЕНЕНИЕ В ИСХОДНИКЕ</div>
      <div style={{fontFamily: MONO, fontSize: 30, color: '#aac0ff'}}>easing: Easing.out(Easing.cubic)</div>
    </div>
  </AbsoluteFill>;
};

const Timeline: React.FC<{f: number}> = ({f}) => {
  const colors = ['#3265FF', '#6188FF', '#9CB4FF', '#6188FF', '#3265FF'];
  return <div style={{position: 'relative', width: 824, height: 285, padding: '32px 27px', border: '1px solid #ffffff24',
    borderRadius: 28, background: '#1f2530', boxShadow: '0 25px 65px #0004'}}>
    <div style={{display: 'flex', justifyContent: 'space-between', fontFamily: MONO, fontSize: 20, color: '#818da5', marginBottom: 35}}><span>ТАЙМЛАЙН</span><span>00:40 / 30 FPS</span></div>
    <div style={{display: 'flex', gap: 7, height: 76}}>{colors.map((color,i) => <div key={i} style={{flex: i+2, background: color, borderRadius: 10, opacity: tween(f, i*5, i*5+12),
      transform: `scaleY(${pop(f, i*5)})`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: i===2 ? '#10245a' : 'white', fontFamily: MONO, fontSize: 22}}>0{i+1}</div>)}</div>
    <div style={{display: 'flex', gap: 3, alignItems: 'center', height: 44, marginTop: 17}}>{Array.from({length: 98}, (_,i) => <div key={i} style={{width: 5, height: 6+22*Math.abs(Math.sin(i*2.47)*Math.cos(i*.21)), background: '#7186b5', borderRadius: 2}}/>)}</div>
    <div style={{position: 'absolute', left: 27+tween(f, 0, 83)*760, top: 88, height: 150, width: 2, background: 'white', boxShadow: '0 0 9px #fff7'}}/>
  </div>;
};

const Export: React.FC = () => {
  const f = useCurrentFrame();
  const toFile = ease(tween(f, 66, 98));
  const toNext = ease(tween(f, 173, 204));
  const current = toNext > .5;
  const accent = current ? '#9368FF' : C.blue;
  return <AbsoluteFill style={{background: C.ink, color: C.milk, overflow: 'hidden'}}>
    <Grid f={f}/>
    <div style={{position: 'absolute', left: 84, top: 278, fontSize: 96, lineHeight: 1.1, letterSpacing: -5, fontWeight: 800, ...appear(f)}}>
      {f < 168 ? <>На выходе —<br/><span style={{color: '#a8bfff'}}>обычное видео.</span></> : <>Исходник остаётся.<br/><span style={{color: '#bea6ff'}}>И работает дальше.</span></>}
    </div>
    <div style={{position: 'absolute', left: 84, top: 729, opacity: 1-toFile,
      transform: `translateY(${toFile*90}px) scale(${1-toFile*.18})`}}><Timeline f={f}/></div>
    <div style={{position: 'absolute', left: 156, top: 692, width: 664, height: 592, background: 'linear-gradient(145deg, #303b53, #19202d)',
      borderRadius: 38, border: '2px solid #ffffff24', boxShadow: '0 38px 70px #0006, 11px 12px 0 #0a101c',
      opacity: toFile, transform: `perspective(1300px) rotateY(${-8+toNext*13}deg) rotateZ(${-3+toNext*5}deg) translateY(${(1-toFile)*90}px)`}}>
      <div style={{position: 'absolute', left: 34, top: 28, right: 34, display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontFamily: MONO, fontSize: 20, color: '#a0aec6'}}><span>{current ? 'episode-02.tsx' : 'source-code.mp4'}</span><span>↗</span></div>
      <div style={{position: 'absolute', left: 36, top: 85, right: 36, height: 348, background: current ? '#1d1535' : '#111724', borderRadius: 22, padding: '42px 36px', overflow: 'hidden'}}>
        <div style={{position: 'absolute', right: -20, top: 10, width: 230, height: 230, border: `40px solid ${accent}`, borderRadius: current ? '50%' : 49,
          transform: `rotate(${f*.23}deg)`, opacity: .75, boxShadow: `0 0 100px ${accent}44`}}/>
        <div style={{fontSize: 22, color: '#bbc8df', marginBottom: 22, letterSpacing: 3, fontWeight: 700}}>{current ? 'СЛЕДУЮЩАЯ ТЕМА' : 'ЭКСПЕРИМЕНТ / 01'}</div>
        <div style={{position: 'relative', fontSize: 65, fontWeight: 800, lineHeight: 1.02, letterSpacing: -3}}>{current ? <>AI-агенты.<br/>Кто здесь<br/>управляет?</> : <>У видео<br/>есть<br/><span style={{color: '#93b0ff'}}>исходник.</span></>}</div>
      </div>
      <div style={{position: 'absolute', bottom: 44, left: 36, right: 36, display: 'flex', alignItems: 'center', gap: 23}}>
        <div style={{width: 64, height: 64, background: accent, borderRadius: 18, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 27}}>{current ? '{ }' : '▶'}</div>
        <div><div style={{fontSize: 29, fontWeight: 800}}>{current ? 'Новая тема. Тот же код.' : 'MP4 · 1080 × 1920'}</div><div style={{fontFamily: MONO, marginTop: 7, fontSize: 18, color: '#929fb8'}}>{current ? 'TEXT + COLOR + GRAPHICS' : 'H.264 / 30 FPS / 40 SEC'}</div></div>
      </div>
    </div>
    <div style={{position: 'absolute', left: 84, top: 1358, display: 'flex', gap: 15, ...appear(f, 115)}}>
      <Pill style={{fontSize: 21, borderColor: '#3265ff77', color: '#afc3ff'}}>исходник.tsx</Pill>
      <Arrow size={49} color={toNext > .5 ? '#a77cff' : C.blue}/>
      <Pill style={{fontSize: 21, background: current ? '#9368ff' : '#3265ff', borderColor: 'transparent'}}>следующий ролик</Pill>
    </div>
  </AbsoluteFill>;
};

type Word = {text: string; start: number; end: number};
type Caption = {start: number; end: number; words: Word[]};
const Captions: React.FC<{light: boolean}> = ({light}) => {
  const f = useCurrentFrame();
  const sec = f/30;
  const caption = (timings as Caption[]).find(c => sec >= c.start && sec < c.end);
  if (!caption) return null;
  return <div style={{position: 'absolute', left: 68, right: 110, top: 1518, display: 'flex', justifyContent: 'center'}}>
    <div style={{display: 'flex', justifyContent: 'center', flexWrap: 'wrap', gap: '4px 6px', padding: '13px 15px', borderRadius: 19,
      background: light ? '#111419f2' : '#080b11e8', border: `1px solid ${light ? '#ffffff20' : '#ffffff15'}`,
      boxShadow: '0 12px 40px #0002', maxWidth: 885}}>
      {caption.words.map((w, i) => <span key={i} style={{fontSize: 41, fontWeight: 800, letterSpacing: -.7,
        lineHeight: 1.35, color: 'white', background: sec >= w.start && sec <= w.end+.025 ? C.blue : 'transparent',
        padding: '2px 7px', borderRadius: 9}}>{w.text}</span>)}
    </div>
  </div>;
};

const Wipes: React.FC = () => {
  const f = useCurrentFrame();
  const cut = [120, 360, 600, 900].find(t => Math.abs(f-t) <= 8);
  if (!cut) return null;
  const p = (f-cut+8)/16;
  return <AbsoluteFill style={{background: C.blue, transform: `translateX(${interpolate(p,[0,.5,1],[112,-2,-112])}%) skewX(-8deg)`, width: 1280, left: -100}}/>;
};

export const Film: React.FC = () => {
  const f = useCurrentFrame();
  const light = (f >= 120 && f < 360) || (f >= 600 && f < 900);
  return <AbsoluteFill style={{fontFamily: FONT, background: C.ink}}>
    <FontReady/>
    <Sequence durationInFrames={120}><Hook/></Sequence>
    <Sequence from={120} durationInFrames={240}><Layers/></Sequence>
    <Sequence from={360} durationInFrames={240}><Prompt/></Sequence>
    <Sequence from={600} durationInFrames={300}><Motion/></Sequence>
    <Sequence from={900} durationInFrames={300}><Export/></Sequence>
    <Topbar f={f} light={light}/>
    <Captions light={light}/>
    <Wipes/>
    <Audio src={staticFile('mix.wav')}/>
  </AbsoluteFill>;
};

export const Cover: React.FC = () => <AbsoluteFill style={{fontFamily: FONT, background: C.ink}}>
  <FontReady/><Hook still/><Topbar f={0} light={false}/>
  <div style={{position: 'absolute', left: 88, top: 1550, fontSize: 30, color: '#d9e1f0', fontWeight: 600, lineHeight: 1.5}}>Слова → код → анимация<br/><span style={{color: '#8390a9'}}>Как собран этот ролик</span></div>
</AbsoluteFill>;
