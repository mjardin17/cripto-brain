import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import express, { type Request, type Response } from 'express';

const PORT = Number(process.env.PORT || 3000);
const FASTAPI_HOST = process.env.FASTAPI_HOST || '127.0.0.1';
const FASTAPI_PORT = Number(process.env.FASTAPI_PORT || 8010);
const FASTAPI_ORIGIN = `http://${FASTAPI_HOST}:${FASTAPI_PORT}`;

let pythonProc: ChildProcess | null = null;

function resolvePythonExecutable(): string {
  const venvPython = path.resolve(process.cwd(), '.venv/bin/python');
  if (fs.existsSync(venvPython)) {
    return venvPython;
  }
  return 'python3';
}

function startFastApiBackend(): void {
  const pythonBin = resolvePythonExecutable();
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
  });
}

async function waitForFastApi(timeoutMs = 15000): Promise<void> {
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
    const text = await upstream.text();
    const contentType = upstream.headers.get('content-type') || 'application/json';
    res.status(upstream.status).type(contentType).send(text);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Backend unavailable';
    res.status(503).json({
      error: 'FastAPI backend unreachable',
      detail: message,
      provenance: 'UNAVAILABLE',
    });
  }
}

async function main() {
  startFastApiBackend();
  await waitForFastApi();

  const app = express();
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

  const shutdown = () => {
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
