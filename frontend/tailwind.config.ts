import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        shield: {
          navy: "#0f1b3d",
          blue: "#0070e0",
          slateink: "#1e293b",
        },
      },
      boxShadow: {
        card: "0 1px 3px rgba(15,27,61,0.08), 0 8px 24px rgba(15,27,61,0.06)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
} satisfies Config;
