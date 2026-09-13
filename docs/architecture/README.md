# ARIES Crash Recovery System — Architecture Foundation

## System Overview

This document outlines the high-level architecture and layered component roadmap for the educational ARIES-inspired database crash recovery system.

## Planned Layered Architecture

```
+-------------------------------------------------------------+
|                      Monitoring & API                       |
|           (FastAPI REST Endpoints + Web Dashboard)          |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|               Fault Injection & Benchmarking                |
|            (Crash simulator, Workload generator)            |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                    ARIES Recovery Engine                    |
|           - Analysis Phase                                  |
|           - Redo Phase (Repeating History)                  |
|           - Undo Phase (Rolling Back Active Txns)           |
|           - Compensation Log Records (CLRs)                 |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                     Transaction Manager                     |
|           - Transaction Table                               |
|           - Commit / Abort Protocol                         |
|           - Savepoints & Rollbacks                          |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                 Write-Ahead Logging (WAL)                   |
|           - Append-Only Log Records                         |
|           - LSN, prevLSN, pageLSN                           |
|           - Fuzzy Checkpointer                              |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                       Buffer Manager                        |
|           - Buffer Pool (Frames)                            |
|           - Replacement Policy (LRU / Clock)                |
|           - Dirty Page Table (DPT) & recLSN                 |
|           - Flush / Pin / Unpin Protocols                   |
+-------------------------------------------------------------+
                              |
+-------------------------------------------------------------+
|                   Persistent Disk Storage                   |
|           - Slotted Page Layout                             |
|           - Binary File I/O & Page Serialization            |
|           - Free Space & Catalog Management                 |
+-------------------------------------------------------------+
```

## Implementation Phases

- **Phase 0 (Current):** Project foundation, backend setup, frontend setup, health checks, testing infrastructure.
- **Phase 1:** Page-based persistent storage (slotted page layout, disk manager).
- **Phase 2:** Buffer pool manager (page frames, replacement policy, Dirty Page Table tracking).
- **Phase 3:** Write-Ahead Logging (WAL) & Log Manager (LSNs, log record serialization, fuzzy checkpointing).
- **Phase 4:** Transaction Manager (concurrency, transaction states, commit/abort protocols).
- **Phase 5:** ARIES Crash Recovery Engine (Analysis, Redo repeating history, Undo with CLRs).
- **Phase 6:** Fault injection, crash simulation, recovery benchmark & testing suite.
- **Phase 7:** Interactive monitoring dashboard & visualization frontend.
