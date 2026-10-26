import { defineConfig } from 'vite'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'

// Resolve cert paths relative to this config file, not the cwd, so it works
// regardless of how the dev server is launched (systemd, npm, etc.).
const certPath = (f: string) => fileURLToPath(new URL(`./certs/${f}`, import.meta.url))

// Self-signed dev cert generated with proper IP SAN entries (see certs/san.cnf).
// basic-ssl only covered localhost/127.0.0.1, so accessing via a LAN/VPN
// IP gave a domain mismatch: Chrome lets you click through, but Firefox then
// rejects same-origin fetch()/XHR with "NetworkError when attempting to fetch
// resource". A cert whose SAN lists the actual IPs (as IP Address entries, not
// DNS) fixes it for both browsers. Regenerate with:
//   openssl req -x509 -newkey rsa:2048 -nodes \
//     -keyout certs/dev-key.pem -out certs/dev-cert.pem -days 825 -config certs/san.cnf
//
// Optional: a fresh clone has no cert (the key is git-ignored), and reading it
// unconditionally made even `npm run build` fail there. Without one the dev
// server runs plain HTTP, which works on localhost (browsers treat it as secure,
// so the Secure session cookie is kept) but not from another device.
const devHttps = existsSync(certPath('dev-key.pem')) && existsSync(certPath('dev-cert.pem'))
  ? { key: readFileSync(certPath('dev-key.pem')), cert: readFileSync(certPath('dev-cert.pem')) }
  : undefined

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    // Problem: agent worktrees live inside the project (.claude/worktrees),
    //   so this server watched them. Every `vite build` there wrote a
    //   dist/index.html, and each one fully reloaded the open app, ending
    //   any chat turn that was running at the time.
    // Fix: never watch .claude; nothing the app serves lives there.
    watch: {
      ignored: ['**/.claude/**']
    },
    https: devHttps,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:3000',
        changeOrigin: true
      }
    }
  }
})
