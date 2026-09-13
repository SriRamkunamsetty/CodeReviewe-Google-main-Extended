"use client";

import { useState, useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import {
  Loader2,
  ShieldCheck,
  AlertTriangle,
  Sparkles,
  ClipboardList,
} from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { useReview, type ReviewFinding, type ReviewSession } from "@/lib/hooks/use-review";

const LANGUAGE_OPTIONS = [
  { value: "", label: "Auto-detect from filename" },
  { value: "python", label: "Python" },
  { value: "javascript", label: "JavaScript" },
  { value: "typescript", label: "TypeScript" },
  { value: "java", label: "Java" },
  { value: "c", label: "C" },
  { value: "cpp", label: "C++" },
  { value: "go", label: "Go" },
  { value: "rust", label: "Rust" },
];

function severityVariant(
  severity: string
): "error" | "warning" | "info" | "default" {
  if (severity === "high") return "error";
  if (severity === "medium") return "warning";
  return "info";
}

function scoreVariant(score: number | null): "success" | "warning" | "error" | "default" {
  if (score === null) return "default";
  if (score >= 8) return "success";
  if (score >= 5) return "warning";
  return "error";
}

function FindingRow({ finding }: { finding: ReviewFinding }) {
  return (
    <div className="flex items-start gap-3 bg-zinc-950/60 border border-zinc-800/40 rounded-lg p-3">
      <Badge variant={severityVariant(finding.severity)}>{finding.severity}</Badge>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-xs text-zinc-500 mb-1">
          <span className="uppercase tracking-wide">{finding.dimension}</span>
          {finding.line != null && <span>· line {finding.line}</span>}
          {finding.rule_code && (
            <span className="font-mono text-zinc-600">[{finding.rule_code}]</span>
          )}
          {finding.matched_rule_ref && (
            <span className="text-indigo-400">
              · violates historical rule #{finding.matched_rule_ref}
            </span>
          )}
        </div>
        <p className="text-sm text-zinc-300">{finding.message}</p>
      </div>
    </div>
  );
}

function ResultPanel({ session }: { session: ReviewSession }) {
  if (session.status === "queued" || session.status === "running") {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
        <Loader2 className="w-6 h-6 mb-3 text-indigo-400 animate-spin" />
        <p className="text-sm">Reviewing your submission…</p>
      </div>
    );
  }

  if (session.status === "failed") {
    return (
      <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/20 p-4 rounded-xl flex items-start gap-2">
        <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
        <span>{session.error_message || "Review failed for an unknown reason."}</span>
      </div>
    );
  }

  const rubric = session.rubric_breakdown;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4 bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4">
        <div className="text-4xl font-bold tabular-nums">
          <Badge variant={scoreVariant(session.score)}>
            <span className="text-lg px-1">{session.score ?? "–"}/10</span>
          </Badge>
        </div>
        <div className="flex-1">
          <p className="text-sm text-zinc-300">{/* summary comes from findings context, shown below */}</p>
          {rubric?.notes.map((note, i) => (
            <p key={i} className="text-xs text-zinc-500 mt-1">
              {note}
            </p>
          ))}
        </div>
      </div>

      {rubric && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {Object.entries(rubric.dimension_scores).map(([dim, score]) => (
            <div
              key={dim}
              className="bg-zinc-950/60 border border-zinc-800/40 rounded-lg p-3 text-center"
            >
              <p className="text-xs text-zinc-500 uppercase tracking-wide mb-1">{dim}</p>
              <p className="text-lg font-semibold tabular-nums">{score}/10</p>
            </div>
          ))}
        </div>
      )}

      {!session.static_analysis_available && (
        <div className="text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 p-2.5 rounded-lg">
          No deterministic static analyzer is available for this language yet —
          findings below are from AST structure and LLM reasoning only.
        </div>
      )}

      {(session.findings?.length ?? 0) > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">
            Findings
          </h4>
          <div className="space-y-2">
            {session.findings!.map((f, i) => (
              <FindingRow key={i} finding={f} />
            ))}
          </div>
        </div>
      )}

      {(session.architecture_notes?.length ?? 0) > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">
            Architecture notes
          </h4>
          <ul className="text-sm text-zinc-300 space-y-1 list-disc list-inside">
            {session.architecture_notes!.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}

      {(session.optimization_notes?.length ?? 0) > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">
            Optimization notes
          </h4>
          <ul className="text-sm text-zinc-300 space-y-1 list-disc list-inside">
            {session.optimization_notes!.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ScoreTrendChart({ history }: { history: ReviewSession[] }) {
  const data = useMemo(() => {
    return [...history]
      .filter((s) => s.status === "completed" && s.score !== null)
      .reverse() // history arrives newest-first; chart reads left-to-right chronologically
      .map((s, i) => ({
        index: i + 1,
        score: s.score,
        filename: s.filename,
        date: new Date(s.created_at).toLocaleDateString(),
      }));
  }, [history]);

  if (data.length < 2) {
    return (
      <p className="text-xs text-zinc-500">
        Submit at least two reviews to see your score trend over time.
      </p>
    );
  }

  return (
    <div className="h-48 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 5, right: 10, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
          <XAxis dataKey="index" stroke="#71717a" fontSize={11} />
          <YAxis domain={[0, 10]} stroke="#71717a" fontSize={11} />
          <Tooltip
            contentStyle={{
              background: "#18181b",
              border: "1px solid #3f3f46",
              borderRadius: "8px",
              fontSize: "12px",
            }}
            labelFormatter={(index) => `Submission #${index}`}
            formatter={(value: number, _name, item) => [
              `${value}/10`,
              item.payload.filename,
            ]}
          />
          <Line
            type="monotone"
            dataKey="score"
            stroke="#6366f1"
            strokeWidth={2}
            dot={{ r: 3, fill: "#6366f1" }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function ReviewView() {
  const {
    submitting,
    activeSession,
    history,
    loadingHistory,
    error,
    submitReview,
    ingestRules,
  } = useReview();

  const [code, setCode] = useState("");
  const [filename, setFilename] = useState("submission.py");
  const [languageHint, setLanguageHint] = useState("");
  const [rulesCsv, setRulesCsv] = useState("");
  const [rulesStatus, setRulesStatus] = useState<string | null>(null);
  const [showRulesPanel, setShowRulesPanel] = useState(false);

  const handleSubmit = () => {
    submitReview(code, filename, languageHint || undefined);
  };

  const handleIngestRules = async () => {
    const result = await ingestRules(rulesCsv);
    setRulesStatus(result.message);
    if (result.ok) setRulesCsv("");
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden p-6 max-w-6xl mx-auto w-full">
      <div className="flex items-center justify-between mb-6 shrink-0">
        <div>
          <h2 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
            <ShieldCheck className="w-5 h-5 text-indigo-400" />
            Code Review
          </h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            Submit code for an automated multi-language review, grounded in your
            team&apos;s historical rules.
          </p>
        </div>
        <button
          onClick={() => setShowRulesPanel((v) => !v)}
          className="flex items-center gap-2 px-3.5 py-1.5 bg-zinc-900 border border-zinc-800 text-zinc-300 rounded-lg text-xs font-medium hover:bg-zinc-800 transition-colors"
        >
          <ClipboardList className="w-3.5 h-3.5" />
          {showRulesPanel ? "Hide rules" : "Manage historical rules"}
        </button>
      </div>

      {showRulesPanel && (
        <div className="mb-6 bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4 shrink-0">
          <p className="text-xs text-zinc-500 mb-2">
            Paste a CSV in the schema <code className="text-zinc-400">id, type, description</code>
            {" "}— these rules ground every future review for your organization.
          </p>
          <textarea
            value={rulesCsv}
            onChange={(e) => setRulesCsv(e.target.value)}
            placeholder={"id,type,description\n1,security,Never interpolate raw user input into SQL queries"}
            className="w-full h-24 bg-zinc-950 border border-zinc-800 rounded-lg p-3 text-xs font-mono text-zinc-200 focus:outline-none focus:border-indigo-500/60"
          />
          <div className="flex items-center gap-3 mt-2">
            <button
              onClick={handleIngestRules}
              className="px-3.5 py-1.5 bg-indigo-500/15 text-indigo-400 rounded-lg text-xs font-medium hover:bg-indigo-500/25 transition-colors"
            >
              Ingest rules
            </button>
            {rulesStatus && <span className="text-xs text-zinc-500">{rulesStatus}</span>}
          </div>
        </div>
      )}

      <div className="flex-1 overflow-y-auto custom-scrollbar space-y-6">
        {/* Submission form */}
        <div className="bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4">
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <input
              value={filename}
              onChange={(e) => setFilename(e.target.value)}
              placeholder="filename.py"
              className="flex-1 min-w-[160px] bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-1.5 text-xs font-mono text-zinc-200 focus:outline-none focus:border-indigo-500/60"
            />
            <select
              value={languageHint}
              onChange={(e) => setLanguageHint(e.target.value)}
              className="bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-1.5 text-xs text-zinc-300 focus:outline-none focus:border-indigo-500/60"
            >
              {LANGUAGE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <textarea
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="Paste code to review…"
            className="w-full h-56 bg-zinc-950 border border-zinc-800 rounded-lg p-3 text-xs font-mono text-zinc-200 focus:outline-none focus:border-indigo-500/60 resize-y"
          />
          {error && <p className="text-xs text-red-400 mt-2">{error}</p>}
          <div className="flex justify-end mt-3">
            <button
              onClick={handleSubmit}
              disabled={submitting}
              className="flex items-center gap-2 px-4 py-2 bg-indigo-500 text-white rounded-lg text-sm font-medium hover:bg-indigo-400 transition-colors disabled:opacity-50 active:scale-[0.98]"
            >
              {submitting ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Sparkles className="w-4 h-4" />
              )}
              {submitting ? "Reviewing…" : "Submit for review"}
            </button>
          </div>
        </div>

        {/* Active review result */}
        {activeSession && <ResultPanel session={activeSession} />}

        {/* Growth trend */}
        <div className="bg-zinc-900/40 border border-zinc-800/60 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-zinc-200 mb-3">
            Your score over time
          </h3>
          <ScoreTrendChart history={history} />
        </div>

        {/* History list */}
        <div>
          <h3 className="text-sm font-semibold text-zinc-200 mb-3">Review history</h3>
          {loadingHistory && history.length === 0 ? (
            <div className="flex items-center justify-center h-32">
              <Loader2 className="w-5 h-5 text-indigo-400 animate-spin" />
            </div>
          ) : history.length === 0 ? (
            <p className="text-xs text-zinc-500">No reviews submitted yet.</p>
          ) : (
            <div className="space-y-2">
              {history.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center justify-between bg-zinc-900/30 border border-zinc-800/50 rounded-lg px-3 py-2"
                >
                  <div className="flex items-center gap-3">
                    <Badge variant={scoreVariant(s.score)}>{s.score ?? "–"}/10</Badge>
                    <span className="text-xs font-mono text-zinc-300">{s.filename}</span>
                    <span className="text-xs text-zinc-600">{s.language}</span>
                  </div>
                  <span className="text-xs text-zinc-500 font-mono tabular-nums">
                    {new Date(s.created_at).toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
