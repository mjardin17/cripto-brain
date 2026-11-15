import { useCallback, useEffect, useState } from 'react';
import { KeyRound, RefreshCw, ShieldAlert, UserPlus, X } from 'lucide-react';
import { certaintyApi } from '../api/client';
import type {
  AdminUserRecord,
  AuthenticatedUserRecord,
  PasswordResetTokenResponse,
  SecurityAuditLogRecord,
  SessionRecord,
  UserRole,
} from '../types/certainty';
import {
  describeUserRole,
  formatTimestampShort,
  summarizeAuditOutcome,
} from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface AdminAccessViewProps {
  currentUser: AuthenticatedUserRecord;
}

const ALL_ROLES: UserRole[] = ['READ_ONLY', 'ANALYST', 'OPERATOR', 'OWNER'];

export function AdminAccessView({ currentUser }: AdminAccessViewProps) {
  const [users, setUsers] = useState<AdminUserRecord[]>([]);
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [auditLogs, setAuditLogs] = useState<SecurityAuditLogRecord[]>([]);
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [auditFilter, setAuditFilter] = useState<'ALL' | 'SUCCESS' | 'DENIED'>('ALL');
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [issuedReset, setIssuedReset] = useState<PasswordResetTokenResponse | null>(null);

  // Create user form
  const [newUsername, setNewUsername] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [newRole, setNewRole] = useState<UserRole>('ANALYST');
  const [newPassword, setNewPassword] = useState('');
  const [mustRotate, setMustRotate] = useState(true);

  const loadAdminData = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const [usersRes, sessionsRes, auditsRes] = await Promise.all([
        certaintyApi.adminListUsers(),
        certaintyApi.adminListSessions(includeRevoked),
        certaintyApi.adminListAuditLogs(150),
      ]);
      setUsers(usersRes.items);
      setSessions(sessionsRes.items);
      setAuditLogs(auditsRes.items);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to load access control state');
    } finally {
      setLoading(false);
    }
  }, [includeRevoked]);

  useEffect(() => {
    void loadAdminData();
  }, [loadAdminData]);

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusyId('create-user');
    setErrorMsg(null);
    setNotice(null);
    try {
      const res = await certaintyApi.adminCreateUser({
        username: newUsername.trim(),
        password: newPassword,
        role: newRole,
        email: newEmail.trim() || undefined,
        must_rotate_password: mustRotate,
      });
      setNewUsername('');
      setNewEmail('');
      setNewPassword('');
      setNotice(`Created ${res.user.role} user "${res.user.username}".`);
      await loadAdminData();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to create user');
    } finally {
      setBusyId(null);
    }
  };

  const handleRoleChange = async (userId: string, username: string, role: UserRole) => {
    setBusyId(`role-${userId}`);
    setErrorMsg(null);
    setNotice(null);
    try {
      await certaintyApi.adminUpdateUserRole(userId, role);
      setNotice(`Updated "${username}" role to ${role} and rotated active sessions.`);
      await loadAdminData();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to update user role');
    } finally {
      setBusyId(null);
    }
  };

  const handleToggleActive = async (u: AdminUserRecord) => {
    setBusyId(`active-${u.user_id}`);
    setErrorMsg(null);
    setNotice(null);
    try {
      if (u.is_active) {
        await certaintyApi.adminDeactivateUser(u.user_id);
        setNotice(`Deactivated "${u.username}" and revoked all active sessions.`);
      } else {
        await certaintyApi.adminReactivateUser(u.user_id);
        setNotice(`Reactivated "${u.username}".`);
      }
      await loadAdminData();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to update account status');
    } finally {
      setBusyId(null);
    }
  };

  const handleIssueReset = async (u: AdminUserRecord) => {
    setBusyId(`reset-${u.user_id}`);
    setErrorMsg(null);
    setNotice(null);
    try {
      const tok = await certaintyApi.adminIssueResetToken(u.user_id, 30);
      setIssuedReset(tok);
      setNotice(`Issued 30-minute single-use password reset token for "${u.username}".`);
      await loadAdminData();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to issue reset token');
    } finally {
      setBusyId(null);
    }
  };

  const handleRevokeSession = async (sessionId: string) => {
    setBusyId(`sess-${sessionId}`);
    setErrorMsg(null);
    setNotice(null);
    try {
      await certaintyApi.adminRevokeSession(sessionId, 'Revoked via OWNER Access Console');
      setNotice(`Revoked session ${sessionId}.`);
      await loadAdminData();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to revoke session');
    } finally {
      setBusyId(null);
    }
  };

  const filteredLogs = auditLogs.filter((entry) => {
    if (auditFilter === 'ALL') return true;
    return entry.outcome.toUpperCase() === auditFilter;
  });

  return (
    <div className="space-y-6">
      {notice ? (
        <div
          role="status"
          className="px-4 py-3 rounded-lg border border-sky-800/70 bg-sky-950/30 text-xs text-sky-200 flex items-center justify-between gap-4"
        >
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="text-sky-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}

      {errorMsg ? (
        <div
          role="alert"
          className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 flex items-center justify-between gap-4"
        >
          <span>{errorMsg}</span>
          <button type="button" onClick={() => setErrorMsg(null)} className="text-rose-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}

      {issuedReset ? (
        <div className="p-4 rounded-xl border border-amber-700/70 bg-amber-950/25 text-xs space-y-2">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 font-semibold text-amber-200">
              <KeyRound className="w-4 h-4" />
              <span>Single-Use Password Reset Token (Shown Once)</span>
            </div>
            <button
              type="button"
              onClick={() => setIssuedReset(null)}
              aria-label="Dismiss reset token"
              className="text-amber-300 hover:text-white"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="text-slate-300">
            User: <span className="font-mono text-slate-100">{issuedReset.username}</span> · Expires:{' '}
            <span className="font-mono text-slate-100">{formatTimestampShort(issuedReset.expires_at)}</span>
          </div>
          <div
            className="p-2.5 rounded bg-[#0B111B] border border-amber-800/60 font-mono text-amber-300 break-all select-all"
            data-testid="issued-reset-token"
          >
            {issuedReset.reset_token}
          </div>
        </div>
      ) : null}

      {/* Top Row: Create User + Users Directory */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        <section className="xl:col-span-4 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <UserPlus className="w-4 h-4 text-sky-400" />
              <h2 className="text-sm font-semibold text-slate-100">Provision User Account</h2>
            </div>
            <ProvenanceLabel provenance="LIVE" detail="OWNER Only" />
          </div>

          <form onSubmit={(e) => void handleCreateUser(e)} className="space-y-3 text-xs">
            <div>
              <label htmlFor="admin-new-username" className="block text-slate-300 mb-1">
                Username
              </label>
              <input
                id="admin-new-username"
                type="text"
                required
                minLength={3}
                maxLength={32}
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                placeholder="e.g., risk_analyst_1"
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
              />
            </div>

            <div>
              <label htmlFor="admin-new-email" className="block text-slate-300 mb-1">
                Email (optional)
              </label>
              <input
                id="admin-new-email"
                type="email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                placeholder="analyst@example.com"
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
              />
            </div>

            <div>
              <label htmlFor="admin-new-role" className="block text-slate-300 mb-1">
                Assigned RBAC Role
              </label>
              <select
                id="admin-new-role"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value as UserRole)}
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
              >
                {ALL_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="admin-new-password" className="block text-slate-300 mb-1">
                Initial Password (min 12 chars, 3 char classes)
              </label>
              <input
                id="admin-new-password"
                type="password"
                required
                minLength={12}
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
              />
            </div>

            <label className="flex items-center gap-2 text-slate-300 cursor-pointer">
              <input
                type="checkbox"
                checked={mustRotate}
                onChange={(e) => setMustRotate(e.target.checked)}
                className="rounded border-slate-700 bg-[#0B111B]"
              />
              <span>Require password rotation on next change</span>
            </label>

            <button
              type="submit"
              disabled={busyId !== null}
              className="w-full px-4 py-2 font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg transition-colors"
            >
              {busyId === 'create-user' ? 'Creating User...' : 'Create User'}
            </button>
          </form>
        </section>

        <section className="xl:col-span-8 border border-slate-800 bg-[#111827] rounded-xl overflow-hidden flex flex-col">
          <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                User Directory &amp; Role Assignment ({users.length})
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Role updates or deactivations immediately revoke target user sessions in DuckDB.
              </p>
            </div>
            <button
              type="button"
              onClick={() => void loadAdminData()}
              disabled={loading}
              className="px-3 py-1.5 text-xs font-medium text-slate-200 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg inline-flex items-center gap-1.5"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              Refresh
            </button>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-slate-800 bg-[#0E1522] text-slate-400 font-medium">
                  <th className="py-2.5 px-4">Principal</th>
                  <th className="py-2.5 px-3">Role</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3">Last Login</th>
                  <th className="py-2.5 px-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {users.map((u) => {
                  const isSelf = u.user_id === currentUser.user_id;
                  const roleBadge = describeUserRole(u.role);
                  return (
                    <tr key={u.user_id} className="hover:bg-slate-800/30">
                      <td className="py-2.5 px-4">
                        <div className="font-mono font-semibold text-slate-100">
                          {u.username}{' '}
                          {isSelf ? <span className="text-[11px] text-sky-400">(you)</span> : null}
                        </div>
                        <div className="text-[11px] text-slate-400">
                          {u.email || u.user_id} · created by {u.created_by}
                        </div>
                      </td>
                      <td className="py-2.5 px-3">
                        <select
                          aria-label={`Role for ${u.username}`}
                          value={u.role}
                          disabled={isSelf || busyId !== null}
                          onChange={(e) =>
                            void handleRoleChange(u.user_id, u.username, e.target.value as UserRole)
                          }
                          className={`px-2 py-1 rounded border text-[11px] font-mono bg-[#0B111B] disabled:opacity-60 ${roleBadge.toneClass}`}
                        >
                          {ALL_ROLES.map((r) => (
                            <option key={r} value={r}>
                              {r}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="py-2.5 px-3 font-mono">
                        <span className={u.is_active ? 'text-emerald-400' : 'text-rose-400'}>
                          {u.is_active ? 'ACTIVE' : 'DISABLED'}
                        </span>
                        {u.locked_until ? (
                          <div className="text-[11px] text-amber-400">LOCKED</div>
                        ) : null}
                      </td>
                      <td className="py-2.5 px-3 font-mono text-slate-400 whitespace-nowrap">
                        {formatTimestampShort(u.last_login_at)}
                      </td>
                      <td className="py-2.5 px-4 text-right whitespace-nowrap space-x-1.5">
                        <button
                          type="button"
                          disabled={busyId !== null}
                          onClick={() => void handleIssueReset(u)}
                          className="px-2 py-1 text-[11px] font-medium bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 rounded"
                        >
                          Reset Token
                        </button>
                        <button
                          type="button"
                          disabled={isSelf || busyId !== null}
                          onClick={() => void handleToggleActive(u)}
                          className={`px-2 py-1 text-[11px] font-medium rounded disabled:opacity-50 ${
                            u.is_active
                              ? 'bg-rose-900/70 hover:bg-rose-800 text-rose-100 border border-rose-800'
                              : 'bg-emerald-900/70 hover:bg-emerald-800 text-emerald-100 border border-emerald-800'
                          }`}
                        >
                          {u.is_active ? 'Deactivate' : 'Reactivate'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      {/* Active Sessions Table */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-100">
            Server-Tracked Sessions ({sessions.length})
          </h2>
          <label className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
            <input
              type="checkbox"
              checked={includeRevoked}
              onChange={(e) => setIncludeRevoked(e.target.checked)}
              className="rounded border-slate-700 bg-[#0B111B]"
            />
            <span>Include revoked sessions</span>
          </label>
        </div>

        <div className="overflow-x-auto max-h-64 overflow-y-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="sticky top-0 bg-[#0E1522] border-b border-slate-800 text-slate-400">
              <tr>
                <th className="py-2 px-4">Session ID</th>
                <th className="py-2 px-3">User / Role</th>
                <th className="py-2 px-3">Source IP</th>
                <th className="py-2 px-3">Expires</th>
                <th className="py-2 px-3">State</th>
                <th className="py-2 px-4 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/80">
              {sessions.map((s) => (
                <tr key={s.session_id} className="hover:bg-slate-800/30">
                  <td className="py-2 px-4 font-mono text-slate-200">
                    {s.session_id}
                    {s.session_id === currentUser.session_id ? (
                      <span className="ml-1.5 text-[11px] text-sky-400">(current)</span>
                    ) : null}
                  </td>
                  <td className="py-2 px-3 font-mono text-slate-300">
                    {s.username} · {s.role_snapshot}
                  </td>
                  <td className="py-2 px-3 font-mono text-slate-400">{s.ip_address}</td>
                  <td className="py-2 px-3 font-mono text-slate-400 whitespace-nowrap">
                    {formatTimestampShort(s.expires_at)}
                  </td>
                  <td className="py-2 px-3 font-mono">
                    {s.revoked_at ? (
                      <span className="text-rose-400" title={s.revoked_reason ?? ''}>
                        REVOKED
                      </span>
                    ) : (
                      <span className="text-emerald-400">ACTIVE</span>
                    )}
                  </td>
                  <td className="py-2 px-4 text-right">
                    {!s.revoked_at ? (
                      <button
                        type="button"
                        disabled={busyId !== null || s.session_id === currentUser.session_id}
                        onClick={() => void handleRevokeSession(s.session_id)}
                        className="px-2.5 py-1 text-[11px] font-medium bg-rose-900/70 hover:bg-rose-800 disabled:opacity-40 text-rose-100 rounded border border-rose-800"
                      >
                        Revoke
                      </button>
                    ) : (
                      <span className="font-mono text-[11px] text-slate-500">Closed</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Immutable Security Audit Log */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <ShieldAlert className="w-4 h-4 text-amber-400" />
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                Immutable Security Audit Log ({filteredLogs.length})
              </h2>
              <p className="text-xs text-slate-400">
                Append-only DuckDB security ledger (auth attempts, denials, role changes, CSRF blocks, rate limits). No delete controls exist.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-1 p-1 bg-[#0B111B] border border-slate-800 rounded-lg text-xs">
            {(['ALL', 'SUCCESS', 'DENIED'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setAuditFilter(mode)}
                className={`px-2.5 py-1 rounded font-medium transition-colors ${
                  auditFilter === mode
                    ? 'bg-slate-800 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {mode}
              </button>
            ))}
          </div>
        </div>

        <div className="overflow-x-auto max-h-80 overflow-y-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="sticky top-0 bg-[#0E1522] border-b border-slate-800 text-slate-400">
              <tr>
                <th className="py-2 px-4">Timestamp</th>
                <th className="py-2 px-3">Event Type</th>
                <th className="py-2 px-3">Outcome</th>
                <th className="py-2 px-3">Principal / IP</th>
                <th className="py-2 px-3">Endpoint</th>
                <th className="py-2 px-4">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/80 font-mono">
              {filteredLogs.map((row) => {
                const outcome = summarizeAuditOutcome(row.outcome);
                return (
                  <tr key={row.audit_id} className="hover:bg-slate-800/30">
                    <td className="py-2 px-4 text-slate-400 whitespace-nowrap">
                      {formatTimestampShort(row.created_at)}
                    </td>
                    <td className="py-2 px-3 text-slate-200">{row.event_type}</td>
                    <td className={`py-2 px-3 font-semibold ${outcome.toneClass}`}>
                      {outcome.label}
                    </td>
                    <td className="py-2 px-3 text-slate-300">
                      {row.username || 'anonymous'} {row.role ? `(${row.role})` : ''} · {row.ip_address}
                    </td>
                    <td className="py-2 px-3 text-slate-400">
                      {row.method} {row.endpoint}
                    </td>
                    <td className="py-2 px-4 text-slate-400 max-w-xs truncate" title={JSON.stringify(row.detail)}>
                      {JSON.stringify(row.detail)}
                    </td>
                  </tr>
                );
              })}
              {filteredLogs.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-slate-500 font-sans">
                    No audit records matching filter.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
