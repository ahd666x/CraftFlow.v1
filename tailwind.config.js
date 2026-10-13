/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './**/templates/**/*.html',
    './storefront/static/storefront/js/*.js',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Vazirmatn', 'Tahoma', 'system-ui', 'sans-serif'],
      },
      colors: {
        primary: {
          50: '#f5f0eb', 100: '#e8ddd3', 200: '#d4c2ab', 300: '#b8a082',
          400: '#9a8261', 500: '#7d6849', 600: '#6b543d', 700: '#574534',
          800: '#45382c', 900: '#332a23', 950: '#261f1a',
        },
        secondary: {
          50: '#fdf8f0', 100: '#f9efd8', 200: '#f2dbb0', 300: '#e8c680',
          400: '#ddb050', 500: '#d4a030', 600: '#c49128', 700: '#a37522',
          800: '#825d1e', 900: '#664a1b', 950: '#3d2a10',
        },
        accent: {
          50: '#f4f6f3', 100: '#e8ebe3', 200: '#d1d7c7', 300: '#b3c0a1',
          400: '#90a378', 500: '#768a5a', 600: '#5f7048', 700: '#4d593b',
          800: '#414833', 900: '#383d2e', 950: '#1f2017',
        },
        surface: {
          50: '#faf7f2', 100: '#f3ebe3', 200: '#e6d5c4', 300: '#d4b899',
          400: '#c09770', 500: '#ad7d52', 600: '#9a6644', 700: '#815139',
          800: '#6b4433', 900: '#583a2d', 950: '#2f1d16',
        },
        cream: '#faf7f2',
        sage: {
          50: '#f3f6f1', 100: '#e3eadf', 200: '#c9d6c2', 300: '#a8b9a0',
          400: '#8aa07f', 500: '#6f8764', 600: '#5a7151', 700: '#495c42',
          800: '#3d4c38', 900: '#343e31', 950: '#1e231a',
        },
      },
      boxShadow: {
        'elevation-1': '0 1px 3px 0 rgb(0 0 0 / 0.08), 0 1px 2px -1px rgb(0 0 0 / 0.08)',
        'elevation-2': '0 4px 6px -1px rgb(0 0 0 / 0.08), 0 2px 4px -2px rgb(0 0 0 / 0.08)',
        'elevation-3': '0 10px 15px -3px rgb(0 0 0 / 0.08), 0 4px 6px -4px rgb(0 0 0 / 0.08)',
        'elevation-4': '0 20px 25px -5px rgb(0 0 0 / 0.08), 0 8px 10px -6px rgb(0 0 0 / 0.08)',
        'glow': '0 0 40px -10px rgb(120 140 100 / 0.35)',
      },
      borderRadius: {
        'default': '12px', 'lg': '16px', 'xl': '20px', '2xl': '24px', '3xl': '28px', 'full': '9999px',
      },
      fontSize: {
        'xs': ['0.75rem', { lineHeight: '1.5' }],
        'sm': ['0.875rem', { lineHeight: '1.6' }],
        'base': ['1rem', { lineHeight: '1.75' }],
        'lg': ['1.125rem', { lineHeight: '1.75' }],
        'xl': ['1.25rem', { lineHeight: '1.7' }],
        '2xl': ['1.5rem', { lineHeight: '1.6' }],
        '3xl': ['1.875rem', { lineHeight: '1.4' }],
        '4xl': ['2.25rem', { lineHeight: '1.3' }],
        '5xl': ['3rem', { lineHeight: '1.2' }],
        '6xl': ['3.75rem', { lineHeight: '1.1' }],
      },
      spacing: {
        '18': '4.5rem',
        '22': '5.5rem',
        '30': '7.5rem',
      },
      zIndex: {
        'dropdown': '1000', 'sticky': '1020', 'overlay': '1040',
        'modal': '1060', 'popover': '1080', 'toast': '1100',
      },
      animation: {
        'fade-in': 'fadeIn 0.6s ease-out forwards',
        'slide-up': 'slideUp 0.7s ease-out forwards',
        'slide-down': 'slideDown 0.5s ease-out forwards',
        'ken-burns': 'kenBurns 20s ease-in-out infinite',
        'float': 'float 6s ease-in-out infinite',
        'float-delayed': 'float 6s ease-in-out 3s infinite',
        'pulse-slow': 'pulse 4s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'scale-in': 'scaleIn 0.4s ease-out forwards',
        'kenburns': 'kenburns 14s ease-in-out infinite alternate',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(24px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideDown: {
          '0%': { opacity: '0', transform: 'translateY(-16px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        kenBurns: {
          '0%, 100%': { transform: 'scale(1)' },
          '50%': { transform: 'scale(1.08)' },
        },
        float: {
          '0%, 100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-12px)' },
        },
        scaleIn: {
          '0%': { opacity: '0', transform: 'scale(0.95)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        kenburns: {
          '0%': { transform: 'scale(1)' },
          '100%': { transform: 'scale(1.08)' },
        },
      },
      transitionDuration: {
        '400': '400ms',
        '600': '600ms',
        '800': '800ms',
      },
      transitionTimingFunction: {
        'spring': 'cubic-bezier(0.34, 1.56, 0.64, 1)',
        'expo': 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [],
}
