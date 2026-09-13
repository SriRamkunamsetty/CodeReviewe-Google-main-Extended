"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { getBackendUrl } from "@/lib/config";
import { useAuth } from "@/lib/auth";

export interface RubricBreakdown {
  overall_score: number;
  dimension_scores: Record<string, number>;
  weights_used: Record<string, number>;
  finding_counts: Record<string, number>;
  notes: string[];
}

export interface ReviewFinding {
  dimension: string;
  severity: "low" | "medium" | "high";
  line: number | null;
  message: string;
  rule_code?: string | null;
  matched_rule_ref?: string | null;
  source: string;
}

export interface ReviewSession {
  id: string;
  filename: string;
  language: string | null;
  status: "queued" | "running" | "completed" | "failed";
  score: number | null;
  rubric_breakdown: RubricBreakdown | null;
  findings?: ReviewFinding[];
  architecture_notes?: string[];
  optimization_notes?: string[];
  matched_historical_rules?: Array<{
    rule_ref: string;
    type: string;
    description: string;
    similarity: number | null;
  }>;
  static_analysis_available?: boolean;
  llm_parse_ok?: boolean;
  error_message?: string | null;
  created_at: string;
  completed_at: string | null;
}

interface UseReviewState {
  submitting: boolean;
  activeSession: ReviewSession | null;
  history: ReviewSession[];
  loadingHistory: boolean;
  error: string | null;
}

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 90000; // matches the 120s Celery soft_time_limit with margin

export function useReview() {
  const { session: authSession } = useAuth();
  const [state, setState] = useState<UseReviewState>({
    submitting: false,
    activeSession: null,
    history: [],
    loadingHistory: false,
    error: null,
  });

  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollStartRef = useRef<number>(0);

  const authHeaders = useCallback((): Record<string, string> => {
    return {
      "Content-Type": "application/json",
      Authorization: authSession?.access_token
        ? `Bearer ${authSession.access_token}`
        : "",
    };
  }, [authSession]);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const fetchHistory = useCallback(async () => {
    if (!authSession) return;
    setState((s) => ({ ...s, loadingHistory: true }));
    try {
      const backendUrl = getBackendUrl();
      const res = await fetch(`${backendUrl}/review/history`, {
        headers: authHeaders(),
      });
      const data = await res.json();
      setState((s) => ({ ...s, history: data.sessions || [], loadingHistory: false }));
    } catch (err) {
      console.error("Failed to fetch review history:", err);
      setState((s) => ({ ...s, loadingHistory: false }));
    }
  }, [authSession, authHeaders]);

  const pollStatus = useCallback(
    (sessionId: string) => {
      stopPolling();
      pollStartRef.current = Date.now();
      const backendUrl = getBackendUrl();

      pollTimerRef.current = setInterval(async () => {
        if (Date.now() - pollStartRef.current > POLL_TIMEOUT_MS) {
          stopPolling();
          setState((s) => ({
            ...s,
            submitting: false,
            error: "Review is taking longer than expected. Check history shortly.",
          }));
          return;
        }

        try {
          const res = await fetch(`${backendUrl}/review/status/${sessionId}`, {
            headers: authHeaders(),
          });
          if (!res.ok) return; // transient — keep polling until timeout
          const data: ReviewSession = await res.json();

          if (data.status === "completed" || data.status === "failed") {
            stopPolling();
            setState((s) => ({ ...s, submitting: false, activeSession: data }));
            fetchHistory();
          } else {
            setState((s) => ({ ...s, activeSession: data }));
          }
        } catch (err) {
          console.error("Poll failed:", err);
        }
      }, POLL_INTERVAL_MS);
    },
    [authHeaders, stopPolling, fetchHistory]
  );

  const submitReview = useCallback(
    async (code: string, filename: string, languageHint?: string) => {
      if (!authSession) {
        setState((s) => ({ ...s, error: "Please sign in to submit a review." }));
        return;
      }
      if (!code.trim()) {
        setState((s) => ({ ...s, error: "Paste some code before submitting." }));
        return;
      }

      setState((s) => ({ ...s, submitting: true, error: null, activeSession: null }));

      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/review/submit`, {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({
            code,
            filename,
            language_hint: languageHint || null,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : "Failed to submit review."
          );
        }
        pollStatus(data.session_id);
      } catch (err) {
        const message = err instanceof Error ? err.message : "Failed to submit review.";
        setState((s) => ({ ...s, submitting: false, error: message }));
      }
    },
    [authSession, authHeaders, pollStatus]
  );

  const ingestRules = useCallback(
    async (csvText: string): Promise<{ ok: boolean; message: string }> => {
      if (!authSession) return { ok: false, message: "Please sign in first." };
      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/review/rules/ingest`, {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ csv_text: csvText }),
        });
        const data = await res.json();
        if (!res.ok) {
          return { ok: false, message: data.detail || "Failed to ingest rules." };
        }
        return { ok: true, message: `Ingested ${data.rules_ingested} rule(s).` };
      } catch (err) {
        return {
          ok: false,
          message: err instanceof Error ? err.message : "Failed to ingest rules.",
        };
      }
    },
    [authSession, authHeaders]
  );

  useEffect(() => {
    let isSubscribed = true;
    if (!authSession) return;

    // Inline async fetch (rather than calling the memoized fetchHistory
    // directly) matches the pattern already used in DashboardShell.tsx's
    // equivalent effect, and adds an isSubscribed guard so a slow response
    // after unmount/re-auth can't set state on a stale render.
    (async () => {
      setState((s) => ({ ...s, loadingHistory: true }));
      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/review/history`, {
          headers: authHeaders(),
        });
        const data = await res.json();
        if (isSubscribed) {
          setState((s) => ({ ...s, history: data.sessions || [], loadingHistory: false }));
        }
      } catch (err) {
        console.error("Failed to fetch review history:", err);
        if (isSubscribed) {
          setState((s) => ({ ...s, loadingHistory: false }));
        }
      }
    })();

    return () => {
      isSubscribed = false;
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authSession]);

  return {
    ...state,
    submitReview,
    fetchHistory,
    ingestRules,
  };
}
