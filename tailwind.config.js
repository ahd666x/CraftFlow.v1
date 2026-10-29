/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './**/templates/**/*.html',
    './storefront/static/storefront/js/*.js',
  ],
  // کلاس‌هایی که فقط در ویجت‌های فرم پایتون (accounts/forms.py) ساخته می‌شوند
  // و اسکنر محتوای Tailwind قالب‌ها آن‌ها را نمی‌بیند.
  safelist: ['accent-primary-600'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Vazirmatn', 'Tahoma', 'system-ui', 'sans-serif'],
      },
      colors: {
        primary: {
          50: 'rgb(var(--primary-50) / <alpha-value>)',
          100: 'rgb(var(--primary-100) / <alpha-value>)',
          200: 'rgb(var(--primary-200) / <alpha-value>)',
          300: 'rgb(var(--primary-300) / <alpha-value>)',
          400: 'rgb(var(--primary-400) / <alpha-value>)',
          500: 'rgb(var(--primary-500) / <alpha-value>)',
          600: 'rgb(var(--primary-600) / <alpha-value>)',
          700: 'rgb(var(--primary-700) / <alpha-value>)',
          800: 'rgb(var(--primary-800) / <alpha-value>)',
          900: 'rgb(var(--primary-900) / <alpha-value>)',
          950: 'rgb(var(--primary-950) / <alpha-value>)',
        },
        secondary: {
          50: 'rgb(var(--secondary-50) / <alpha-value>)',
          100: 'rgb(var(--secondary-100) / <alpha-value>)',
          200: 'rgb(var(--secondary-200) / <alpha-value>)',
          300: 'rgb(var(--secondary-300) / <alpha-value>)',
          400: 'rgb(var(--secondary-400) / <alpha-value>)',
          500: 'rgb(var(--secondary-500) / <alpha-value>)',
          600: 'rgb(var(--secondary-600) / <alpha-value>)',
          700: 'rgb(var(--secondary-700) / <alpha-value>)',
          800: 'rgb(var(--secondary-800) / <alpha-value>)',
          900: 'rgb(var(--secondary-900) / <alpha-value>)',
          950: 'rgb(var(--secondary-950) / <alpha-value>)',
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
        cream: 'rgb(var(--cream) / <alpha-value>)',
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
