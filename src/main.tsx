import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { initializeStoreFromServer } from './store/useAppStore'

// Trigger store local hydration and remote synchronization asynchronously,
// without blocking the initial render of the application shell.
initializeStoreFromServer()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// Register Service Worker for PWA / background persistence
// Only on localhost — LAN devices use a self-signed cert which browsers
// refuse to use for SW registration (SecurityError). LAN users still benefit
// from normal HTTP cache; install via "Add to Home Screen" for full offline support.
if ('serviceWorker' in navigator && location.hostname === 'localhost') {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js', { scope: '/' })
      .then((reg) => {
        console.log('[PWA] Service worker registered:', reg.scope)
      })
      .catch((err) => {
        console.warn('[PWA] Service worker registration failed:', err)
      })
  })
}
