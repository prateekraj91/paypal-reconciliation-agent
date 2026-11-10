/** @type {import('tailwindcss').Config} */

// Strictly monochrome: black, white and one neutral grey scale. `theme.colors` is
// replaced, not extended, so no chromatic utility (blue-*, red-*, ...) can be generated.
// Safety states are told apart by icon, label, border style and fill, never by hue.
const neutral = {
  50: '#fafafa',
  100: '#f5f5f5',
  200: '#e5e5e5',
  300: '#d4d4d4',
  400: '#a3a3a3',
  500: '#858585', // lifted from #737373 so muted text keeps WCAG AA on black and #111
  600: '#525252',
  700: '#404040',
  800: '#262626',
  900: '#171717',
  950: '#0a0a0a',
};

export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    colors: {
      transparent: 'transparent',
      current: 'currentColor',
      inherit: 'inherit',
      black: '#000000',
      white: '#ffffff',
      neutral,
      // Tailwind's preflight reads colors.gray.400 for input placeholders and falls back
      // to a blue-tinted grey when it is missing; pin it to the neutral scale instead.
      gray: { 400: neutral[400] },
    },
    // Default ring colour falls back to blue-500 otherwise.
    ringColor: ({ theme }) => ({ DEFAULT: '#ffffff', ...theme('colors') }),
    ringOffsetColor: ({ theme }) => theme('colors'),
    extend: {
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
    },
  },
  plugins: [],
};
