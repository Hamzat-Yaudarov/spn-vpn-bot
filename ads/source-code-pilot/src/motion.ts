import {Easing, interpolate} from 'remotion';

// This is the exact implementation displayed in the film.
// The normalized progress value is deliberately the only input.
export const smoothX = (progress: number) =>
  interpolate(progress, [0, 1], [420, 0], {
    easing: Easing.out(Easing.cubic),
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

export const sharpX = (progress: number) => (progress < 0.22 ? 420 : 0);

export const codeLines = [
  'interpolate(progress,',
  '  [0, 1], [420, 0], {',
  '    easing: Easing.out(',
  '      Easing.cubic',
  '    )',
  '  });',
];
