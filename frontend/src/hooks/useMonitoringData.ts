import { useState, useEffect, useRef, useCallback } from 'react';
import {
  AllMonitoringData,
  BenchmarkSummaryResponse,
  BufferPoolStatisticsResponse,
  EndpointState,
  HealthResponse,
  MonitoringStatusResponse,
  RecoveryStatisticsResponse,
  TransactionStatisticsResponse,
  WALStatisticsResponse,
  WorkloadSummaryResponse,
} from '../types';

const createInitialEndpointState = <T>(): EndpointState<T> => ({
  data: null,
  loading: true,
  error: null,
  lastUpdated: null,
});

export function useMonitoringData(pollIntervalMs: number = 10000) {
  const [data, setData] = useState<AllMonitoringData>({
    health: createInitialEndpointState<HealthResponse>(),
    status: createInitialEndpointState<MonitoringStatusResponse>(),
    transactions: createInitialEndpointState<TransactionStatisticsResponse>(),
    wal: createInitialEndpointState<WALStatisticsResponse>(),
    bufferPool: createInitialEndpointState<BufferPoolStatisticsResponse>(),
    recovery: createInitialEndpointState<RecoveryStatisticsResponse>(),
    workload: createInitialEndpointState<WorkloadSummaryResponse>(),
    benchmark: createInitialEndpointState<BenchmarkSummaryResponse>(),
    lastSuccessfulRefresh: null,
    isRefreshing: false,
  });

  const abortControllerRef = useRef<AbortController | null>(null);
  const activeFetchIdRef = useRef<number>(0);
  const isMountedRef = useRef<boolean>(true);

  const fetchEndpoint = async <T>(
    url: string,
    signal: AbortSignal
  ): Promise<T> => {
    const res = await fetch(url, { signal, headers: { Accept: 'application/json' } });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    return (await res.json()) as T;
  };

  const refreshAll = useCallback(async (force: boolean = false) => {
    // If a request is already in progress and this is not a forced/manual refresh, do not overlap
    if (abortControllerRef.current && !force) {
      return;
    }

    // Cancel any existing in-flight request before starting a new one
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    const controller = new AbortController();
    abortControllerRef.current = controller;
    const fetchId = ++activeFetchIdRef.current;
    const signal = controller.signal;

    setData((prev) => ({
      ...prev,
      isRefreshing: true,
      health: { ...prev.health, loading: prev.health.data === null },
      status: { ...prev.status, loading: prev.status.data === null },
      transactions: { ...prev.transactions, loading: prev.transactions.data === null },
      wal: { ...prev.wal, loading: prev.wal.data === null },
      bufferPool: { ...prev.bufferPool, loading: prev.bufferPool.data === null },
      recovery: { ...prev.recovery, loading: prev.recovery.data === null },
      workload: { ...prev.workload, loading: prev.workload.data === null },
      benchmark: { ...prev.benchmark, loading: prev.benchmark.data === null },
    }));

    const now = new Date();

    try {
      const [
        healthRes,
        statusRes,
        txRes,
        walRes,
        bpRes,
        recRes,
        wlRes,
        bmRes,
      ] = await Promise.allSettled([
        fetchEndpoint<HealthResponse>('/api/health', signal),
        fetchEndpoint<MonitoringStatusResponse>('/api/monitoring/status', signal),
        fetchEndpoint<TransactionStatisticsResponse>('/api/monitoring/transactions', signal),
        fetchEndpoint<WALStatisticsResponse>('/api/monitoring/wal', signal),
        fetchEndpoint<BufferPoolStatisticsResponse>('/api/monitoring/buffer-pool', signal),
        fetchEndpoint<RecoveryStatisticsResponse>('/api/monitoring/recovery', signal),
        fetchEndpoint<WorkloadSummaryResponse>('/api/monitoring/workload', signal),
        fetchEndpoint<BenchmarkSummaryResponse>('/api/monitoring/benchmark', signal),
      ]);

      // If unmounted, or this fetch was superseded by a newer one, or signal was aborted: ignore
      if (!isMountedRef.current || fetchId !== activeFetchIdRef.current || signal.aborted) {
        return;
      }

      const updateEndpoint = <T>(
        res: PromiseSettledResult<T>,
        prev: EndpointState<T>
      ): EndpointState<T> => {
        if (res.status === 'fulfilled') {
          return {
            data: res.value,
            loading: false,
            error: null,
            lastUpdated: now,
          };
        } else {
          return {
            data: prev.data,
            loading: false,
            error: res.reason instanceof Error ? res.reason.message : 'Request failed',
            lastUpdated: prev.lastUpdated,
          };
        }
      };

      const anyFulfilled = [
        healthRes,
        statusRes,
        txRes,
        walRes,
        bpRes,
        recRes,
        wlRes,
        bmRes,
      ].some((r) => r.status === 'fulfilled');

      setData((prev) => ({
        health: updateEndpoint(healthRes, prev.health),
        status: updateEndpoint(statusRes, prev.status),
        transactions: updateEndpoint(txRes, prev.transactions),
        wal: updateEndpoint(walRes, prev.wal),
        bufferPool: updateEndpoint(bpRes, prev.bufferPool),
        recovery: updateEndpoint(recRes, prev.recovery),
        workload: updateEndpoint(wlRes, prev.workload),
        benchmark: updateEndpoint(bmRes, prev.benchmark),
        lastSuccessfulRefresh: anyFulfilled ? now : prev.lastSuccessfulRefresh,
        isRefreshing: false,
      }));
    } catch (_err: unknown) {
      if (!isMountedRef.current || fetchId !== activeFetchIdRef.current) {
        return;
      }
      setData((prev) => ({
        ...prev,
        isRefreshing: false,
      }));
    } finally {
      if (activeFetchIdRef.current === fetchId) {
        abortControllerRef.current = null;
        if (isMountedRef.current) {
          setData((prev) => (prev.isRefreshing ? { ...prev, isRefreshing: false } : prev));
        }
      }
    }
  }, []);

  const manualRefresh = useCallback(() => {
    return refreshAll(true);
  }, [refreshAll]);

  useEffect(() => {
    isMountedRef.current = true;
    refreshAll(true);

    if (pollIntervalMs > 0) {
      const timer = setInterval(() => {
        refreshAll(false);
      }, pollIntervalMs);

      return () => {
        isMountedRef.current = false;
        clearInterval(timer);
        if (abortControllerRef.current) {
          abortControllerRef.current.abort();
          abortControllerRef.current = null;
        }
      };
    }

    return () => {
      isMountedRef.current = false;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
    };
  }, [refreshAll, pollIntervalMs]);

  return {
    data,
    refresh: manualRefresh,
  };
}
