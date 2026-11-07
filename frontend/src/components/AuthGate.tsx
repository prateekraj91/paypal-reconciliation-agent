import React, { useState } from 'react';
import { api } from '../api/client';
import { Lock, KeyRound, ShieldAlert, ArrowRight, Eye, EyeOff, Building2 } from 'lucide-react';

interface AuthGateProps {
  onAuthenticated: () => void;
  tenantId: string;
}

export const AuthGate: React.FC<AuthGateProps> = ({ onAuthenticated, tenantId }) => {
  const [tokenInput, setTokenInput] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [isVerifying, setIsVerifying] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = tokenInput.trim();
    if (!trimmed) {
      setErrorMessage('Please enter your operator admin bearer token.');
      return;
    }

    setIsVerifying(true);
    setErrorMessage(null);

    try {
      // Set key in volatile memory only
      api.setApiKey(trimmed);
      // Verify credential against protected summary endpoint
      await api.getDashboardSummary(tenantId);
      onAuthenticated();
    } catch (err: any) {
      api.clearApiKey();
      console.error('Authentication verification error:', err);
      if (err.message && err.message.includes('401')) {
        setErrorMessage('Authentication failed (401 Unauthorized): Invalid admin bearer credential.');
      } else {
        setErrorMessage(err.message || 'Failed to authenticate with backend.');
      }
    } finally {
      setIsVerifying(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#070a12] text-slate-100 flex flex-col justify-center items-center px-4 py-12 relative overflow-hidden">
      {/* Background radial gradient */}
      <div className="absolute w-[600px] h-[600px] bg-blue-600/10 rounded-full blur-3xl pointer-events-none -top-20 -left-20" />
      <div className="absolute w-[500px] h-[500px] bg-indigo-600/10 rounded-full blur-3xl pointer-events-none -bottom-20 -right-20" />

      <div className="max-w-md w-full relative z-10 space-y-6">
        {/* Brand Card */}
        <div className="text-center space-y-2">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-blue-950/60 border border-blue-800/60 text-blue-400 text-xs font-mono font-medium mb-2">
            <Lock size={14} className="w-3.5 h-3.5" />
            Operator Access Control
          </div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Reconciliation Agent
          </h1>
          <p className="text-sm text-slate-400">
            Reconciliation you can prove. Enter your administrative bearer token to access the workspace.
          </p>
        </div>

        {/* Auth Form Card */}
        <div className="bg-slate-900/80 backdrop-blur-xl border border-slate-800 rounded-2xl p-6 shadow-2xl space-y-5">
          {errorMessage && (
            <div className="p-3.5 rounded-xl bg-rose-950/50 border border-rose-800/60 text-rose-300 text-xs flex items-start gap-2.5">
              <ShieldAlert size={16} className="w-4 h-4 text-rose-400 flex-shrink-0 mt-0.5" />
              <div className="leading-relaxed">{errorMessage}</div>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2 flex items-center justify-between">
                <span className="flex items-center gap-1.5">
                  <KeyRound size={14} className="w-3.5 h-3.5 text-blue-400" />
                  Admin Bearer Token
                </span>
                <span className="text-[11px] font-mono text-slate-500 normal-case">
                  Memory-only (no storage)
                </span>
              </label>
              <div className="relative">
                <input
                  type={showToken ? 'text' : 'password'}
                  required
                  autoFocus
                  placeholder="Paste ADMIN_API_KEY..."
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  className="input-text w-full pl-3.5 pr-10 py-2.5 bg-slate-950 border border-slate-700/80 focus:border-blue-500 rounded-xl text-sm font-mono text-slate-100 placeholder-slate-600 focus:outline-none transition-colors"
                />
                <button
                  type="button"
                  onClick={() => setShowToken(!showToken)}
                  className="absolute right-3 top-3 text-slate-500 hover:text-slate-300 transition-colors bg-transparent border-0 p-0 cursor-pointer"
                  tabIndex={-1}
                >
                  {showToken ? <EyeOff size={16} className="w-4 h-4" /> : <Eye size={16} className="w-4 h-4" />}
                </button>
              </div>
            </div>

            <div className="pt-2">
              <button
                type="submit"
                disabled={isVerifying || !tokenInput.trim()}
                className="btn btn-primary w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-xl font-medium text-sm transition-all shadow-lg shadow-blue-900/20 disabled:opacity-50"
              >
                {isVerifying ? (
                  <span>Verifying Credential...</span>
                ) : (
                  <>
                    <span>Authenticate & Access Workspace</span>
                    <ArrowRight size={16} className="w-4 h-4" />
                  </>
                )}
              </button>
            </div>
          </form>

          {/* Security Architecture Notice */}
          <div className="pt-4 border-t border-slate-800 text-[11px] text-slate-400 space-y-2 leading-relaxed">
            <div className="font-semibold text-slate-300 flex items-center gap-1">
              <Building2 size={12} className="w-3 h-3 text-slate-400" />
              Security Architecture Notice:
            </div>
            <p>
              • <strong>In-Memory Volatile Storage:</strong> Your credential is held solely in browser application memory. It is <em>never</em> written to localStorage, sessionStorage, cookies, or logs, and will be immediately purged on logout or page refresh.
            </p>
            <p>
              • <strong>Local Operator Mode:</strong> The backend validates against the server's configured <code className="text-slate-300 font-mono">ADMIN_API_KEY</code>. This single-tenant administrative token is designed for local hackathon operation and does not provide multi-user RBAC.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};

