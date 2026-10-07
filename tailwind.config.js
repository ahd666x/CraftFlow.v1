/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    // فقط قالب‌های فروشگاه. عمداً پنل تولید/اینونتوری اینجا نیست، چون آن
    // صفحات Bootstrap هستند و نباید کلاس Tailwind بگیرند.
    './storefront/templates/**/*.html',
    './storefront/static/storefront/css/**/*.css',
    './storefront/static/storefront/js/**/*.js',
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
      },
      boxShadow: {
        'elevation-1': '0 1px 3px 0 rgb(0 0 0 / 0.08), 0 1px 2px -1px rgb(0 0 0 / 0.08)',
        'elevation-2': '0 4px 6px -1px rgb(0 0 0 / 0.08), 0 2px 4px -2px rgb(0 0 0 / 0.08)',
        'elevation-3': '0 10px 15px -3px rgb(0 0 0 / 0.08), 0 4px 6px -4px rgb(0 0 0 / 0.08)',
      },
      borderRadius: {
        'xs': '2px', 'sm': '4px', 'default': '8px', 'md': '10px',
        'lg': '14px', 'xl': '18px', '2xl': '24px', 'full': '9999px',
      },
      fontSize: {
        'xs': ['0.75rem', { lineHeight: '1.5' }],
        'sm': ['0.875rem', { lineHeight: '1.6' }],
        'base': ['1rem', { lineHeight: '1.75' }],
        'lg': ['1.125rem', { lineHeight: '1.75' }],
        'xl': ['1.25rem', { lineHeight: '1.7' }],
        '2xl': ['1.5rem', { lineHeight: '1.6' }],
        '3xl': ['1.875rem', { lineHeight: '1.5' }],
        '4xl': ['2.25rem', { lineHeight: '1.4' }],
      },
      zIndex: {
        'dropdown': '1000', 'sticky': '1020', 'overlay': '1040',
        'modal': '1060', 'popover': '1080', 'toast': '1100',
      },
    },
  },
  plugins: [],
}
