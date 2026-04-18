import { useEffect, useMemo, useState } from 'react';
import { useApi } from '../hooks/useApi';

/** A row from GET /api/test-runs */
interface TestRun {
  id: number;
  git_branch: string | null;
  git_sha: string | null;
  suite: string;
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  errors: number;
  duration_sec: number;
  started_at: string | null;
  finished_at: string | null;
  triggered_by: string;
  python_version: string | null;
  platform: string | null;
  exit_status: string;
  summary: string | null;
}

/** A row from GET /api/test-runs/{id}.results */
interface TestResult {
  id: number;
  nodeid: string;
  module: string | null;
  test_class: string | null;
  test_name: string | null;
  outcome: 'passed' | 'failed' | 'skipped' | 'error';
  duration_sec: number;
  error_message: string | null;
  traceback: string | null;
}

interface RunsResp { runs: TestRun[]; total: number; }
interface SummaryResp { trend: TestRun[]; latest: TestRun | null; count: number; }
interface DetailResp { run: TestRun; results: TestResult[]; }

function StatusPill({ status }: { status: string }) {
  const m: Record<string, string> = {
    passed: 'bg-green-500/20 text-green-400',
    failed: 'bg-red-500/20 text-red-400',
    error: 'bg-red-500/30 text-red-300',
    pending: 'bg-yellow-500/20 text-yellow-400',
  };
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${m[status] || 'bg-gray-700 text-gray-400'}`}>
      {status.toUpperCase()}
    </span>
  );
}

function OutcomeDot({ outcome }: { outcome: string }) {
  const m: Record<string, string> = {
    passed: 'bg-green-400',
    failed: 'bg-red-400',
    skipped: 'bg-yellow-400',
    error: 'bg-red-500',
  };
  return <span className={`inline-block w-2 h-2 rounded-full mr-2 ${m[outcome] || 'bg-gray-500'}`} />;
}

/** Tiny inline SVG bar chart — one bar per run, height = pass %, color by status. */
function PassFailTrend({ runs }: { runs: TestRun[] }) {
  if (!runs.length) return <div className="text-sm text-gray-500">No runs yet.</div>;
  const H = 60, BAR_W = 14, GAP = 4;
  const width = runs.length * (BAR_W + GAP);

  return (
    <svg width={width} height={H + 20} className="overflow-visible">
      {runs.map((r, i) => {
        const pct = r.total > 0 ? r.passed / r.total : 0;
        const h = Math.max(2, pct * H);
        const x = i * (BAR_W + GAP);
        const y = H - h;
        const fill = r.exit_status === 'passed'
          ? '#3fb950'
          : r.exit_status === 'failed'
            ? '#f85149'
            : r.exit_status === 'error'
              ? '#d15704'
              : '#8b949e';
        const date = r.started_at ? new Date(r.started_at).toLocaleString() : '';
        return (
          <g key={r.id}>
            <title>{`Run #${r.id}\n${date}\n${r.passed}/${r.total} passed in ${r.duration_sec}s`}</title>
            <rect x={x} y={y} width={BAR_W} height={h} fill={fill} opacity={0.9} rx={2} />
            {r.failed > 0 && (
              <rect x={x} y={0} width={BAR_W}
                    height={(r.failed / r.total) * H}
                    fill="#f85149" opacity={0.7} rx={2} />
            )}
          </g>
        );
      })}
    </svg>
  );
}

export default function TestsTab() {
  const { data: summary, refetch: refetchSummary } =
    useApi<SummaryResp>('/test-runs/summary?limit=30', 30000);
  const { data: runsResp, refetch: refetchRuns } =
    useApi<RunsResp>('/test-runs?limit=50', 30000);

  const runs = runsResp?.runs || [];
  const trend = summary?.trend || [];
  const latest = summary?.latest || (runs.length ? runs[0] : null);

  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [detail, setDetail] = useState<DetailResp | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [outcomeFilter, setOutcomeFilter] = useState<'all' | 'failed' | 'skipped'>('all');

  // ── Launch a new test run via bot_manager sidecar ─────────
  const [launching, setLaunching] = useState<string | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const latestStartedAt = runs.length ? runs[0].started_at : null;

  const launchRun = async (suite: 'unit' | 'concurrency' | 'integration' | 'all') => {
    if (launching) return;
    setLaunchError(null);
    setLaunching(suite);
    try {
      const res = await fetch('/api/test-runs/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ suite }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setLaunchError(err.detail || `HTTP ${res.status}`);
        setLaunching(null);
        return;
      }
      // Poll for the new run to show up (up to ~90s)
      const deadline = Date.now() + 90_000;
      const poll = setInterval(async () => {
        await refetchRuns();
        await refetchSummary();
        if (Date.now() > deadline) {
          clearInterval(poll);
          setLaunching(null);
        }
      }, 2000);
      // Also clear on component unmount — best effort
      setTimeout(() => { clearInterval(poll); setLaunching(null); }, 95_000);
    } catch (e: any) {
      setLaunchError(e?.message || 'launch failed');
      setLaunching(null);
    }
  };

  // If a new run row appears after we launched, stop the busy spinner
  useEffect(() => {
    if (launching && latestStartedAt) {
      const age = Date.now() - new Date(latestStartedAt).getTime();
      if (age < 30_000) {
        // Fresh row — launch succeeded
        setLaunching(null);
      }
    }
  }, [latestStartedAt, launching]);

  useEffect(() => {
    if (selectedRunId == null) {
      setDetail(null);
      return;
    }
    setLoadingDetail(true);
    fetch(`/api/test-runs/${selectedRunId}`)
      .then(r => r.json())
      .then(d => setDetail(d))
      .catch(() => setDetail(null))
      .finally(() => setLoadingDetail(false));
  }, [selectedRunId]);

  const filteredResults = useMemo(() => {
    const results = detail?.results || [];
    if (outcomeFilter === 'all') return results;
    return results.filter(r => r.outcome === outcomeFilter);
  }, [detail, outcomeFilter]);

  const failTotals = useMemo(() => {
    const recent = trend.slice(-10);
    const sumFail = recent.reduce((s, r) => s + r.failed, 0);
    return { sumFail, window: recent.length };
  }, [trend]);

  const deleteRun = async (id: number) => {
    if (!confirm(`Delete test run #${id}?`)) return;
    await fetch(`/api/test-runs/${id}`, { method: 'DELETE' });
    if (selectedRunId === id) setSelectedRunId(null);
    refetchRuns();
    refetchSummary();
  };

  const suiteButtons: { suite: 'unit' | 'concurrency' | 'integration' | 'all'; label: string; hint: string }[] = [
    { suite: 'unit',        label: 'Run Unit',        hint: 'Fast in-memory tests' },
    { suite: 'concurrency', label: 'Run Concurrency', hint: 'Race-condition stress (threaded)' },
    { suite: 'integration', label: 'Run Integration', hint: 'DB locking + reconciliation (needs Postgres)' },
    { suite: 'all',         label: 'Run All',         hint: 'Every suite, unfiltered' },
  ];

  return (
    <div className="space-y-6">
      {/* Run-tests control bar */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-gray-400 mr-2">Launch a run:</span>
          {suiteButtons.map(b => (
            <button key={b.suite}
              onClick={() => launchRun(b.suite)}
              disabled={!!launching}
              title={b.hint}
              className={`px-3 py-1.5 text-xs rounded-md font-medium transition-all border ${
                launching === b.suite
                  ? 'bg-blue-600 text-white animate-pulse border-blue-500 cursor-wait'
                  : launching
                    ? 'bg-[#21262d] border-[#30363d] text-gray-600 cursor-not-allowed'
                    : 'bg-[#21262d] border-[#30363d] text-gray-300 hover:bg-[#30363d] hover:text-white'
              }`}>
              {launching === b.suite ? 'Running...' : b.label}
            </button>
          ))}
          {launching && (
            <span className="text-xs text-blue-400 ml-2">
              waiting for results to appear...
            </span>
          )}
          {launchError && (
            <span className="text-xs text-red-400 ml-2">
              {launchError}
            </span>
          )}
          <span className="text-xs text-gray-500 ml-auto">
            runs on the host via <code className="text-gray-400">bot_manager</code>
          </span>
        </div>
      </div>

      {/* Top row: latest + trend */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4">
          <div className="text-xs text-gray-500 mb-2">Latest Run</div>
          {latest ? (
            <div>
              <div className="flex items-baseline gap-2">
                <span className="text-3xl font-bold">
                  {latest.passed}/{latest.total}
                </span>
                <span className="text-sm text-gray-500">passed</span>
                <StatusPill status={latest.exit_status} />
              </div>
              <div className="mt-2 text-xs text-gray-500 space-y-0.5">
                <div>{latest.summary || '—'}</div>
                <div>
                  {latest.git_branch || '—'}
                  {latest.git_sha && <span> @ <span className="font-mono">{latest.git_sha}</span></span>}
                </div>
                <div>
                  {latest.started_at ? new Date(latest.started_at).toLocaleString() : '—'}
                  <span className="mx-1">•</span>
                  {latest.triggered_by}
                </div>
              </div>
            </div>
          ) : (
            <div className="text-sm text-gray-500">No runs recorded yet.</div>
          )}
        </div>

        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-4 lg:col-span-2">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs text-gray-500">Pass / Fail Trend (last {trend.length})</div>
            <div className="text-xs text-gray-500">
              {failTotals.window > 0 && (
                <>Last {failTotals.window}: <span className={failTotals.sumFail ? 'text-red-400' : 'text-green-400'}>{failTotals.sumFail} failures</span></>
              )}
            </div>
          </div>
          <div className="overflow-x-auto">
            <PassFailTrend runs={trend} />
          </div>
          <div className="mt-2 text-[11px] text-gray-500 flex items-center gap-4">
            <span><span className="inline-block w-2 h-2 rounded-full bg-green-400 mr-1" /> passed</span>
            <span><span className="inline-block w-2 h-2 rounded-full bg-red-400 mr-1" /> failed</span>
            <span className="ml-auto">Hover a bar for details</span>
          </div>
        </div>
      </div>

      {/* Runs list */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg overflow-x-auto">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
          <h3 className="text-sm font-semibold text-gray-300">Test Runs</h3>
          <button onClick={() => { refetchRuns(); refetchSummary(); }}
                  className="text-xs px-2 py-1 bg-[#21262d] border border-[#30363d] text-gray-400 rounded hover:text-white">
            Refresh
          </button>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-[#21262d]">
              {['When', 'Branch', 'SHA', 'Suite', 'Result', 'P / F / S', 'Duration', 'Triggered by', ''].map(h => (
                <th key={h} className="px-3 py-2 text-left text-xs font-semibold text-gray-500 border-b border-[#30363d]">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}
                  className={`cursor-pointer hover:bg-[#1c2128] border-b border-[#21262d] ${selectedRunId === r.id ? 'bg-[#1c2128]' : ''}`}
                  onClick={() => setSelectedRunId(r.id)}>
                <td className="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">
                  {r.started_at ? new Date(r.started_at).toLocaleString() : '—'}
                </td>
                <td className="px-3 py-2 text-xs text-gray-300">{r.git_branch || '—'}</td>
                <td className="px-3 py-2 text-xs text-gray-500 font-mono">{r.git_sha || '—'}</td>
                <td className="px-3 py-2 text-xs text-gray-400">{r.suite}</td>
                <td className="px-3 py-2"><StatusPill status={r.exit_status} /></td>
                <td className="px-3 py-2 text-xs">
                  <span className="text-green-400">{r.passed}</span>
                  <span className="text-gray-600 mx-1">/</span>
                  <span className={r.failed ? 'text-red-400' : 'text-gray-500'}>{r.failed}</span>
                  <span className="text-gray-600 mx-1">/</span>
                  <span className="text-yellow-400">{r.skipped}</span>
                </td>
                <td className="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{r.duration_sec.toFixed(2)}s</td>
                <td className="px-3 py-2 text-xs text-gray-500">{r.triggered_by}</td>
                <td className="px-3 py-2 text-right">
                  <button onClick={(e) => { e.stopPropagation(); deleteRun(r.id); }}
                          className="text-xs text-gray-500 hover:text-red-400">
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={9} className="px-3 py-8 text-center text-sm text-gray-500">
                  No test runs recorded yet. Run with:
                  <pre className="mt-2 inline-block bg-[#0d1117] border border-[#21262d] rounded px-2 py-1 text-xs text-gray-400">
                    PYTEST_DB_REPORT=1 python -m pytest tests/unit/
                  </pre>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Run detail */}
      {selectedRunId && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg">
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
            <h3 className="text-sm font-semibold text-gray-300">
              Run #{selectedRunId}
              {detail?.run?.summary && (
                <span className="ml-3 text-xs text-gray-500 font-normal">{detail.run.summary}</span>
              )}
            </h3>
            <div className="flex items-center gap-2">
              {(['all', 'failed', 'skipped'] as const).map(f => (
                <button key={f} onClick={() => setOutcomeFilter(f)}
                        className={`px-2 py-0.5 text-xs rounded ${outcomeFilter === f ? 'bg-blue-500/20 text-blue-400' : 'text-gray-500 hover:text-gray-300'}`}>
                  {f === 'all' ? 'All' : f === 'failed' ? 'Failures' : 'Skipped'}
                </button>
              ))}
              <button onClick={() => setSelectedRunId(null)}
                      className="text-xs text-gray-500 hover:text-white ml-2">Close</button>
            </div>
          </div>
          {loadingDetail ? (
            <div className="px-4 py-8 text-center text-sm text-gray-500">Loading...</div>
          ) : (
            <div className="max-h-[60vh] overflow-y-auto">
              {filteredResults.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-gray-500">
                  {outcomeFilter === 'failed' ? 'No failures — all green.' : 'No matching results.'}
                </div>
              ) : (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="sticky top-0 bg-[#21262d]">
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold">Outcome</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold">Module</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold">Class</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold">Test</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold">Time</th>
                      <th className="px-3 py-2 text-left text-gray-500 font-semibold">Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredResults.map(r => (
                      <tr key={r.id} className="border-b border-[#21262d] hover:bg-[#1c2128]">
                        <td className="px-3 py-2 whitespace-nowrap">
                          <OutcomeDot outcome={r.outcome} />
                          <span className="text-gray-300">{r.outcome}</span>
                        </td>
                        <td className="px-3 py-2 text-gray-400 font-mono">{r.module || '—'}</td>
                        <td className="px-3 py-2 text-gray-400">{r.test_class || '—'}</td>
                        <td className="px-3 py-2 text-gray-300">{r.test_name}</td>
                        <td className="px-3 py-2 text-gray-500 whitespace-nowrap">{r.duration_sec?.toFixed(3) || '—'}s</td>
                        <td className="px-3 py-2 text-red-400 max-w-md truncate" title={r.traceback || ''}>
                          {r.error_message || (r.outcome === 'failed' ? '—' : '')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
