/**
 * Phase 11 T11-2 — TanStack Query hooks wrapping the pure client.
 *
 * Each hook returns a stable query/mutation object the views (T11-3) can
 * render directly via `<IngestView />`, `<ConstraintsView />`, etc. The
 * underlying `ApiClient` is supplied via context (`ApiClientProvider`) so
 * tests can inject a mock without touching globals.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useContext } from "react";
import { ApiClientContext } from "./context";
import type {
  ConstraintQuery,
  ConstraintQueryResponse,
  EmbeddingCacheFlushResponse,
  HealthResponse,
  IngestionNotification,
  IngestionStatus,
} from "./schemas";

// --- Queries (read-only, cached) ------------------------------------------

export function useHealth(): UseQueryResult<HealthResponse, Error> {
  const client = useContext(ApiClientContext);
  return useQuery({
    queryKey: ["health"],
    queryFn: () => client.getHealth(),
    // Health is short-lived; let TanStack Query refetch on remount.
    staleTime: 0,
    refetchOnWindowFocus: true,
    retry: 1,
  });
}

export function useIngestionStatus(docHash: string | null): UseQueryResult<IngestionStatus, Error> {
  const client = useContext(ApiClientContext);
  return useQuery({
    queryKey: ["ingestion-status", docHash],
    queryFn: () => client.getIngestionStatus(docHash as string),
    enabled: docHash !== null && docHash.length > 0,
    // Poll every 2s while a status query is in flight (status === 'processing').
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.status === "processing" ? 2000 : false;
    },
    staleTime: 0,
    retry: 1,
  });
}

// --- Mutations (write, invalidate queries) --------------------------------

export function useNotifyIngest(): UseMutationResult<
  IngestionStatus,
  Error,
  IngestionNotification
> {
  const client = useContext(ApiClientContext);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input) => client.notifyIngest(input),
    onSuccess: (data, variables) => {
      qc.setQueryData(["ingestion-status", variables.doc_hash], data);
      qc.invalidateQueries({ queryKey: ["health"] });
    },
  });
}

export function useQueryConstraints(): UseMutationResult<
  ConstraintQueryResponse,
  Error,
  ConstraintQuery
> {
  const client = useContext(ApiClientContext);
  return useMutation({
    mutationFn: (input) => client.queryConstraints(input),
  });
}

export function useFlushEmbeddingCache(): UseMutationResult<
  EmbeddingCacheFlushResponse,
  Error,
  void
> {
  const client = useContext(ApiClientContext);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => client.flushEmbeddingCache(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["health"] });
    },
  });
}
