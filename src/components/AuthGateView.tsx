import { useState } from 'react';
import { KeyRound, Lock, ShieldCheck } from 'lucide-react';
import { certaintyApi } from '../api/client';
import type { AuthStatusResponse, AuthenticatedUserRecord } from '../types/certainty';

interface AuthGateViewProps {
  authStatus: AuthStatusResponse;
  sessionNotice: string | null;
  onAuthenticated: (user: AuthenticatedUserRecord) => void;
  onRefreshStatus: () => Promise<void>;
}

export function AuthGateView({
  authStatus,
  sessionNotice,
  onAuthenticated,
  onRefreshStatus,
}: AuthGateViewProps) {
  const isBootstrap = authStatus.needs_owner_bootstrap;

  // Bootstrap OWNER state
  const [ownerUsername, setOwnerUsername] = useState('');
  const [ownerEmail, setOwnerEmail] = useState('');
  const [ownerPassword, setOwnerPassword] = useState('');
  const [ownerConfirmPassword, setOwnerConfirmPassword] = useState('');
  const [bootstrapToken, setBootstrapToken] = useState('');

  // Login state
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  // Password reset token confirmation state
  const [showResetPanel, setShowResetPanel] = useState(false);
  const [resetToken, setResetToken] = useState('');
  const [resetNewPassword, setResetNewPassword] = useState('');
  const [resetSuccessMsg, setResetSuccessMsg] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [cookiesBlockedNotice, setCookiesBlockedNotice] = useState(false);

  const verifySessionOrFallbackAndEnter = async (
    uname: string,
    pw: string,
    user: AuthenticatedUserRecord
  ) => {
    let check = await certaintyApi.getAuthStatus();
    if (!check.authenticated || !check.user) {
      // Browser blocked third-party iframe cookies; seamlessly activate tab-scoped Bearer+CSRF fallback
      const fallbackRes = await certaintyApi.login({
        username: uname,
        password: pw,
        issue_bearer_fallback: true,
      });
      check = await certaintyApi.getAuthStatus();
      if (check.authenticated && check.user) {
        setCookiesBlockedNotice(false);
        onAuthenticated(check.user ?? fallbackRes.user ?? user);
        return;
      }
      setCookiesBlockedNotice(true);
      setErrorMsg(
        'Credentials were accepted by the server, but your browser blocked session persistence inside this embedded preview.'
      );
      await onRefreshStatus();
      return;
    }
    setCookiesBlockedNotice(false);
    onAuthenticated(check.user ?? user);
  };

  const handleBootstrapSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    setCookiesBlockedNotice(false);
    if (ownerPassword !== ownerConfirmPassword) {
      setErrorMsg('Passwords do not match.');
      return;
    }
    const cleanUser = ownerUsername.trim();
    const rawPw = ownerPassword;
    setSubmitting(true);
    try {
      const res = await certaintyApi.bootstrapOwner({
        username: cleanUser,
        password: rawPw,
        email: ownerEmail.trim() || undefined,
        bootstrap_token: bootstrapToken.trim() || undefined,
      });
      await verifySessionOrFallbackAndEnter(cleanUser, rawPw, res.user);
      setOwnerPassword('');
      setOwnerConfirmPassword('');
      setBootstrapToken('');
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Initial OWNER enrollment failed');
      await onRefreshStatus();
    } finally {
      setSubmitting(false);
    }
  };

  const handleLoginSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    setResetSuccessMsg(null);
    setCookiesBlockedNotice(false);
    const cleanUser = username.trim();
    const rawPw = password;
    setSubmitting(true);
    try {
      const res = await certaintyApi.login({
        username: cleanUser,
        password: rawPw,
      });
      await verifySessionOrFallbackAndEnter(cleanUser, rawPw, res.user);
      setPassword('');
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Authentication failed');
    } finally {
      setSubmitting(false);
    }
  };

  const handleResetConfirm = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    setResetSuccessMsg(null);
    setSubmitting(true);
    try {
      const res = await certaintyApi.confirmPasswordReset({
        reset_token: resetToken.trim(),
        new_password: resetNewPassword,
      });
      setResetToken('');
      setResetNewPassword('');
      setShowResetPanel(false);
      setResetSuccessMsg(
        `Password reset completed for ${res.username} (${res.sessions_revoked} session(s) revoked). Sign in with your new password.`
      );
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Password reset failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#0B111B] text-slate-100 flex items-center justify-center p-4 sm:p-6">
      <div className="w-full max-w-md space-y-5">
        <div className="text-center space-y-1.5">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded border border-slate-800 bg-[#0E1522] text-xs font-mono text-sky-400">
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>RESEARCH &amp; PAPER SIMULATION ONLY</span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-50">
            The Certainty Stack
          </h1>
          <p className="text-xs text-slate-400">
            Server-enforced session authentication · Argon2id + HttpOnly cookies · Live trading disabled
          </p>
        </div>

        {sessionNotice ? (
          <div
            role="status"
            className="px-4 py-3 rounded-lg border border-amber-800/70 bg-amber-950/30 text-xs text-amber-200"
          >
            {sessionNotice}
          </div>
        ) : null}

        {resetSuccessMsg ? (
          <div
            role="status"
            className="px-4 py-3 rounded-lg border border-emerald-800/70 bg-emerald-950/30 text-xs text-emerald-200"
          >
            {resetSuccessMsg}
          </div>
        ) : null}

        {errorMsg ? (
          <div
            role="alert"
            className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 space-y-2"
          >
            <div>{errorMsg}</div>
            {cookiesBlockedNotice && typeof window !== 'undefined' ? (
              <div className="pt-1">
                <a
                  href={window.location.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-sky-600 hover:bg-sky-500 text-white font-semibold transition-colors"
                >
                  Open Workspace in New Tab
                </a>
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-6 space-y-5">
          {isBootstrap ? (
            <>
              <div className="space-y-1 border-b border-slate-800 pb-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-slate-100">
                  <KeyRound className="w-4 h-4 text-amber-400" />
                  <h2>Initial OWNER Enrollment</h2>
                </div>
                <p className="text-xs text-slate-400">
                  No users exist in DuckDB yet. Create the initial <span className="font-mono text-amber-300">OWNER</span>{' '}
                  principal. No default passwords exist.
                </p>
              </div>

              <form onSubmit={(e) => void handleBootstrapSubmit(e)} className="space-y-3.5 text-xs">
                <div>
                  <label htmlFor="bootstrap-username" className="block text-slate-300 mb-1">
                    Owner Username
                  </label>
                  <input
                    id="bootstrap-username"
                    type="text"
                    required
                    minLength={3}
                    maxLength={32}
                    autoComplete="username"
                    value={ownerUsername}
                    onChange={(e) => setOwnerUsername(e.target.value)}
                    placeholder="e.g., stack_owner"
                    className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-sky-500"
                  />
                </div>

                <div>
                  <label htmlFor="bootstrap-email" className="block text-slate-300 mb-1">
                    Email (optional)
                  </label>
                  <input
                    id="bootstrap-email"
                    type="email"
                    autoComplete="email"
                    value={ownerEmail}
                    onChange={(e) => setOwnerEmail(e.target.value)}
                    placeholder="owner@example.com"
                    className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-sky-500"
                  />
                </div>

                <div>
                  <label htmlFor="bootstrap-password" className="block text-slate-300 mb-1">
                    Strong Password (min 12 chars, 3 char classes)
                  </label>
                  <input
                    id="bootstrap-password"
                    type="password"
                    required
                    minLength={12}
                    autoComplete="new-password"
                    value={ownerPassword}
                    onChange={(e) => setOwnerPassword(e.target.value)}
                    className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
                  />
                </div>

                <div>
                  <label htmlFor="bootstrap-password-confirm" className="block text-slate-300 mb-1">
                    Confirm Password
                  </label>
                  <input
                    id="bootstrap-password-confirm"
                    type="password"
                    required
                    minLength={12}
                    autoComplete="new-password"
                    value={ownerConfirmPassword}
                    onChange={(e) => setOwnerConfirmPassword(e.target.value)}
                    className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
                  />
                </div>

                {authStatus.env_mode === 'production' ? (
                  <div>
                    <label htmlFor="bootstrap-token" className="block text-slate-300 mb-1">
                      Production Bootstrap Token
                    </label>
                    <input
                      id="bootstrap-token"
                      type="password"
                      required
                      value={bootstrapToken}
                      onChange={(e) => setBootstrapToken(e.target.value)}
                      placeholder="CERTAINTY_BOOTSTRAP_TOKEN"
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-sky-500"
                    />
                  </div>
                ) : null}

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full px-4 py-2.5 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg transition-colors"
                >
                  {submitting ? 'Enrolling OWNER...' : 'Enroll Initial OWNER & Sign In'}
                </button>
              </form>
            </>
          ) : (
            <>
              <div className="space-y-1 border-b border-slate-800 pb-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-slate-100">
                  <Lock className="w-4 h-4 text-sky-400" />
                  <h2>Sign In to Workspace</h2>
                </div>
                <p className="text-xs text-slate-400">
                  Authenticate with your assigned account. Permissions are enforced server-side by role.
                </p>
              </div>

              {!showResetPanel ? (
                <form onSubmit={(e) => void handleLoginSubmit(e)} className="space-y-3.5 text-xs">
                  <div>
                    <label htmlFor="login-username" className="block text-slate-300 mb-1">
                      Username
                    </label>
                    <input
                      id="login-username"
                      type="text"
                      required
                      autoComplete="username"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
                    />
                  </div>

                  <div>
                    <label htmlFor="login-password" className="block text-slate-300 mb-1">
                      Password
                    </label>
                    <input
                      id="login-password"
                      type="password"
                      required
                      autoComplete="current-password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
                    />
                  </div>

                  <button
                    type="submit"
                    disabled={submitting}
                    className="w-full px-4 py-2.5 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg transition-colors"
                  >
                    {submitting ? 'Verifying Credentials...' : 'Sign In'}
                  </button>

                  <div className="pt-1 flex justify-end">
                    <button
                      type="button"
                      onClick={() => {
                        setErrorMsg(null);
                        setShowResetPanel(true);
                      }}
                      className="text-xs text-sky-400 hover:text-sky-300"
                    >
                      Have a single-use password reset token?
                    </button>
                  </div>
                </form>
              ) : (
                <form onSubmit={(e) => void handleResetConfirm(e)} className="space-y-3.5 text-xs">
                  <div>
                    <label htmlFor="reset-token-input" className="block text-slate-300 mb-1">
                      Single-Use Reset Token
                    </label>
                    <input
                      id="reset-token-input"
                      type="text"
                      required
                      minLength={10}
                      value={resetToken}
                      onChange={(e) => setResetToken(e.target.value)}
                      placeholder="Paste reset token issued by OWNER"
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-sky-500"
                    />
                  </div>

                  <div>
                    <label htmlFor="reset-new-password" className="block text-slate-300 mb-1">
                      New Password (min 12 chars, 3 char classes)
                    </label>
                    <input
                      id="reset-new-password"
                      type="password"
                      required
                      minLength={12}
                      autoComplete="new-password"
                      value={resetNewPassword}
                      onChange={(e) => setResetNewPassword(e.target.value)}
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
                    />
                  </div>

                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setErrorMsg(null);
                        setShowResetPanel(false);
                      }}
                      className="px-3 py-2 text-xs font-medium text-slate-300 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg"
                    >
                      Back to Sign In
                    </button>
                    <button
                      type="submit"
                      disabled={submitting}
                      className="flex-1 px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg"
                    >
                      {submitting ? 'Applying Reset...' : 'Apply Password Reset'}
                    </button>
                  </div>
                </form>
              )}
            </>
          )}
        </div>

        <div className="text-center text-[11px] font-mono text-slate-500">
          Mode: {authStatus.env_mode.toUpperCase()} · CSRF Double-Submit · Risk Gate Locked
        </div>
      </div>
    </div>
  );
}
