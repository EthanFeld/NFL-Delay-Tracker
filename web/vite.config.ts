import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  return {
    // Set VITE_BASE_PATH to the GitHub Pages project path, e.g. /NFL-Delay-Tracker/.
    base: env.VITE_BASE_PATH || '/',
    plugins: [react()],
  };
});
