
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./**/templates/**/*.html",
    "./static/**/*.js",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Poppins", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      colors: {
        brand: {
          DEFAULT: "#4349E8",
          dark: "#3338D0",
          soft: "#EEEFFD",
        },
        canvas: "#EEF0FA",
        ink: "#16161D",
      },
    },
  },
  plugins: [],
};
