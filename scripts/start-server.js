import { createServer } from 'vite'
import path from 'path'
import fs from 'fs'
import { spawn } from 'child_process'

// Load env files manually (mirrors Vite's precedence: .env.local > .env)
// This ensures VITE_STORAGE_DIR and other vars are available to the Node process.
const root = process.cwd()
for (const envFile of ['.env', '.env.local']) {
  const envPath = path.join(root, envFile)
  if (fs.existsSync(envPath)) {
    const lines = fs.readFileSync(envPath, 'utf-8').split('\n')
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('#')) continue
      const eqIdx = trimmed.indexOf('=')
      if (eqIdx === -1) continue
      const key = trimmed.slice(0, eqIdx).trim()
      const val = trimmed.slice(eqIdx + 1).trim()
      // Only set if not already defined in the shell environment
      if (!(key in process.env)) process.env[key] = val
    }
  }
}

const args = process.argv
const dirIdx = args.indexOf('--storage-dir')
let storageDir = ''
if (dirIdx !== -1 && args[dirIdx + 1]) {
  storageDir = args[dirIdx + 1]
} else {
  storageDir = process.env.VITE_STORAGE_DIR || 'storage'
}

// Make sure storage dir is resolved and exists
const absoluteStorageDir = path.resolve(process.cwd(), storageDir)
if (!fs.existsSync(absoluteStorageDir)) {
  fs.mkdirSync(absoluteStorageDir, { recursive: true })
}

process.env.VITE_STORAGE_DIR = absoluteStorageDir
console.log(`[Storage Server] Materializing local storage in: ${absoluteStorageDir}`)

// Check if mode is provided
const modeIdx = args.indexOf('--mode')
const mode = (modeIdx !== -1 && args[modeIdx + 1]) ? args[modeIdx + 1] : undefined

// Check if host is provided to expose server (e.g. --host or --host 0.0.0.0)
const hostIdx = args.indexOf('--host')
let host = undefined
if (hostIdx !== -1) {
  if (args[hostIdx + 1] && !args[hostIdx + 1].startsWith('-')) {
    host = args[hostIdx + 1]
  } else {
    host = true
  }
}

// 1. Spawn Python API Backend Server with auto-restart
// Prefer a conda Python when one is present (it may have the optional web-tool dependencies), else system python3
const condaPython = path.join(process.env.HOME || '', 'miniconda3', 'bin', 'python3')
const pythonBin = fs.existsSync(condaPython) ? condaPython : 'python3'

let apiServerProcess = null
let restartCount = 0
const MAX_RESTARTS = 10
const RESTART_DELAY_MS = 2000
const MAX_RESTART_DELAY_MS = 15000

// Uptime after which a process counts as "it worked": a crash later is a new
// incident, not part of a crash loop. Without this the budget is a LIFETIME
// cap — ten restarts across a long session and the backend stays dead while
// Vite keeps serving a UI with nothing behind it.
const HEALTHY_UPTIME_MS = 60_000

function spawnApiServer() {
  const startedAt = Date.now()
  console.log(`[Storage Server] Starting Python API server on port 3000 using ${pythonBin}...`)
  const proc = spawn(pythonBin, [
    path.join(process.cwd(), 'scripts', 'api_server.py'),
    '--storage-dir', absoluteStorageDir,
    '--host', '127.0.0.1',
    '--port', '3000'
  ])

  proc.on('error', (err) => {
    console.error('[Storage Server ERROR] Failed to spawn Python API server:', err)
  })

  proc.on('close', (code, signal) => {
    if (signal === 'SIGTERM' || signal === 'SIGKILL') return // intentional shutdown
    console.error(`[Storage Server] Python API exited (code=${code}, signal=${signal})`)
    if (Date.now() - startedAt > HEALTHY_UPTIME_MS) restartCount = 0
    if (restartCount < MAX_RESTARTS) {
      restartCount++
      // Back off: uvicorn can sit in graceful shutdown holding :3000 (an open
      // SSE stream will do it), and a fixed 2s retry just burns the budget on
      // EADDRINUSE before the port is free.
      const delay = Math.min(RESTART_DELAY_MS * 2 ** (restartCount - 1), MAX_RESTART_DELAY_MS)
      console.log(`[Storage Server] Restarting API server in ${delay}ms (attempt ${restartCount}/${MAX_RESTARTS})...`)
      setTimeout(() => { apiServerProcess = spawnApiServer() }, delay)
    } else {
      console.error('[Storage Server] Max restart attempts reached. API server will not be restarted.')
    }
  })

  proc.stdout.on('data', (data) => process.stdout.write(`[API] ${data}`))
  proc.stderr.on('data', (data) => process.stderr.write(`[API ERR] ${data}`))

  return proc
}

apiServerProcess = spawnApiServer()

// Cleanup handlers
const cleanup = () => {
  if (apiServerProcess && !apiServerProcess.killed) {
    console.log('[Storage Server] Terminating API server...')
    apiServerProcess.kill('SIGTERM')
  }
}

process.on('exit', cleanup)
process.on('SIGINT', () => { cleanup(); process.exit() })
process.on('SIGTERM', () => { cleanup(); process.exit() })
process.on('uncaughtException', (err) => {
  console.error('[Storage Server] Uncaught Exception:', err)
  cleanup()
  process.exit(1)
})


// 2. Start Vite dev server programmatically
try {
  const server = await createServer({
    mode,
    server: {
      host
    }
  })
  await server.listen()
  server.printUrls()
} catch (err) {
  console.error('Failed to start Vite dev server:', err)
  cleanup()
  process.exit(1)
}

