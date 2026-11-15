import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
import fs from 'node:fs';
import path from 'node:path';
import express, { type Request, type Response } from 'express';

const PORT = Number(process.env.PORT || 3000);
const FASTAPI_HOST = process.env.FASTAPI_HOST || '127.0.0.1';
const FASTAPI_PORT = Number(process.env.FASTAPI_PORT || 8010);
const FASTAPI_ORIGIN = `http://${FASTAPI_HOST}:${FASTAPI_PORT}`;

let pythonProc: ChildProcess | null = null;
let shuttingDown = false;

async function pythonHasBackendDeps(pythonBin: string): Promise<boolean> {
  try {
    await execFileAsync(
      pythonBin,
      ['-c', 'import uvicorn, fastapi, duckdb, argon2, httpx, pydantic, itsdangerous'],
      { cwd: process.cwd(), timeout: 10000, maxBuffer: 1024 * 1024, windowsHide: true }
    );
    return true;
  } catch {
    return false;
  }
}

async function ensurePythonEnvironment(): Promise<string> {
  const venvPython = path.resolve(process.cwd(), '.venv/bin/python');
  if (fs.existsSync(venvPython) && await pythonHasBackendDeps(venvPython)) {
    return venvPython;
  }
  if (await pythonHasBackendDeps('python3')) {
    return 'python3';
  }
  const reqFile = path.resolve(process.cwd(), 'requirements.txt');
  console.log('[server] Python backend dependencies missing; bootstrapping .venv from requirements.txt...');
  if (!fs.existsSync(venvPython)) {
    await execFileAsync('python3', ['-m', 'venv', '.venv'], {
      cwd: process.cwd(),
      timeout: 120000,
      maxBuffer: 5 * 1024 * 1024,
      windowsHide: true,
    });
  }
  await execFileAsync(venvPython, ['-m', 'pip', 'install', '-q', '-r', reqFile], {
    cwd: process.cwd(),
    timeout: 300000,
    maxBuffer: 10 * 1024 * 1024,
    windowsHide: true,
  });
  return venvPython;
}

async function startFastApiBackend(): Promise<void> {
  const pythonBin = await ensurePythonEnvironment();
  pythonProc = spawn(
    pythonBin,
    [
      '-m',
      'uvicorn',
      'backend.main:app',
      '--host',
      FASTAPI_HOST,
      '--port',
      String(FASTAPI_PORT),
      '--log-level',
      'warning',
    ],
    {
      cwd: process.cwd(),
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  );

  pythonProc.stdout?.on('data', (chunk: Buffer) => {
    const text = chunk.toString().trim();
    if (text) console.log(`[fastapi] ${text}`);
  });
  pythonProc.stderr?.on('data', (chunk: Buffer) => {
    const text = chunk.toString().trim();
    if (text) console.error(`[fastapi] ${text}`);
  });
  pythonProc.on('exit', (code, signal) => {
    console.error(`[fastapi] exited with code=${code} signal=${signal}`);
    if (!shuttingDown && code !== 0) {
      setTimeout(() => {
        if (!shuttingDown) {
          console.log('[fastapi] Restarting FastAPI backend...');
          void startFastApiBackend().catch((err) => {
            console.error('[fastapi] Backend restart bootstrap failed:', err);
          });
        }
      }, 1500);
    }
  });
}

async function waitForFastApi(timeoutMs = 30000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${FASTAPI_ORIGIN}/api/health?probe_venues=false`);
      if (resp.ok) return;
    } catch {
      // Backend still booting
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  console.warn('[server] FastAPI backend did not respond before startup timeout; continuing with proxy retries.');
}

async function proxyApiRequest(req: Request, res: Response): Promise<void> {
  const targetUrl = `${FASTAPI_ORIGIN}${req.originalUrl}`;
  const headers: Record<string, string> = {};
  if (req.headers['content-type']) {
    headers['content-type'] = String(req.headers['content-type']);
  }
  if (req.headers['accept']) {
    headers['accept'] = String(req.headers['accept']);
  }
  if (req.headers['cookie']) {
    headers['cookie'] = String(req.headers['cookie']);
  }
  if (req.headers['x-csrf-token']) {
    headers['x-csrf-token'] = String(req.headers['x-csrf-token']);
  }
  if (req.headers['origin']) {
    headers['origin'] = String(req.headers['origin']);
  }
  if (req.headers['referer']) {
    headers['referer'] = String(req.headers['referer']);
  }
  if (req.headers['user-agent']) {
    headers['user-agent'] = String(req.headers['user-agent']);
  }

  // Forward verified socket IP and transport protocol to FastAPI trusted local proxy
  const clientIp = req.ip || req.socket.remoteAddress || '127.0.0.1';
  headers['x-forwarded-for'] = clientIp;
  headers['x-forwarded-proto'] = req.protocol || 'http';

  const hasBody = !['GET', 'HEAD'].includes(req.method.toUpperCase());
  const body = hasBody && req.body !== undefined ? JSON.stringify(req.body) : undefined;
  if (hasBody && body && !headers['content-type']) {
    headers['content-type'] = 'application/json';
  }

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body,
    });
    const getSetCookie = (upstream.headers as unknown as { getSetCookie?: () => string[] }).getSetCookie;
    const setCookies = typeof getSetCookie === 'function' ? getSetCookie.call(upstream.headers) : [];
    if (setCookies.length > 0) {
      res.setHeader('Set-Cookie', setCookies);
    } else {
      const singleSetCookie = upstream.headers.get('set-cookie');
      if (singleSetCookie) {
        res.setHeader('Set-Cookie', singleSetCookie);
      }
    }
    const retryAfter = upstream.headers.get('retry-after');
    if (retryAfter) {
      res.setHeader('Retry-After', retryAfter);
    }
    const cacheControl = upstream.headers.get('cache-control');
    if (cacheControl) {
      res.setHeader('Cache-Control', cacheControl);
    }
    const text = await upstream.text();
    const contentType = upstream.headers.get('content-type') || 'application/json';
    res.status(upstream.status).type(contentType).send(text);
  } catch (err) {
    const raw = err instanceof Error ? err.message : 'Backend unavailable';
    const message = raw
      .replace(/AIza[0-9A-Za-z_-]+/g, '[REDACTED_KEY]')
      .replace(/Bearer\s+\S+/gi, 'Bearer [REDACTED]');
    res.status(503).json({
      error: 'FastAPI backend unreachable',
      detail: message,
      provenance: 'UNAVAILABLE',
    });
  }
}

async function main() {
  // Bind the public app port before Python dependency installation/backend startup.
  // Hosting platforms may return an HTML warmup page when no process is listening;
  // that HTML must never be mistaken for an API response.
  const app = express();
  app.disable('x-powered-by');
  if (process.env.CERTAINTY_TRUST_UPSTREAM_PROXY === 'true') {
    app.set('trust proxy', 1);
  }
  app.use((req, res, next) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (req.path.startsWith('/api')) {
      res.setHeader('X-Frame-Options', 'DENY');
    }
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
    if (process.env.NODE_ENV === 'production' || process.env.CERTAINTY_ENV === 'production') {
      res.setHeader('Strict-Transport-Security', 'max-age=63072000; includeSubDomains');
    }
    next();
  });
  app.use(express.json({ limit: '2mb' }));

  app.use('/api', proxyApiRequest);

  const isProd = process.env.NODE_ENV === 'production';
  if (!isProd) {
    const { createServer: createViteServer } = await import('vite');
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distDir = path.resolve(process.cwd(), 'dist');
    app.use(express.static(distDir));
    app.get('*', (_req, res) => {
      res.sendFile(path.join(distDir, 'index.html'));
    });
  }

  const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`[server] The Certainty Stack listening on http://0.0.0.0:${PORT}`);
  });

  // Start the Python service only after the HTTP listener is bound. If dependency
  // bootstrap fails, Express remains available and /api requests return its JSON 503.
  try {
    void startFastApiBackend()
      .then(() => waitForFastApi())
      .catch((err) => {
        console.error('[server] FastAPI backend bootstrap failed; frontend remains available:', err);
      });
  } catch (err) {
    console.error('[server] FastAPI backend bootstrap failed; frontend remains available:', err);
  }

  const shutdown = () => {
    shuttingDown = true;
    server.close();
    if (pythonProc && !pythonProc.killed) {
      pythonProc.kill('SIGTERM');
    }
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('[server] Fatal startup error:', err);
  if (pythonProc && !pythonProc.killed) pythonProc.kill('SIGTERM');
  process.exit(1);
});
