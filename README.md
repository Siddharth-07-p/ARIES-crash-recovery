# ARIES Crash Recovery System

An educational, transactional database storage engine and crash recovery system inspired by IBM's **ARIES** (Algorithm for Recovery and Isolation Exploiting Semantics).

> **Note:** This project implements the database engine and recovery algorithms entirely from scratch in Python (using binary file I/O and custom data structures), with zero reliance on external database engines or third-party persistence engines.

---

## 1. Project Objective

The goal of this project is to build an educational mini transactional DBMS storage engine featuring an authentic, robust ARIES crash recovery mechanism. It aims to provide transparent visibility into how modern relational databases guarantee ACID properties (specifically Atomicity and Durability) in the event of unexpected system crashes.

---

## 2. ARIES Crash Recovery Overview

The ARIES recovery algorithm operates on three fundamental principles:
1. **Write-Ahead Logging (WAL):** Any data change must be recorded in an append-only log on non-volatile storage *before* the corresponding modified dirty page can be written to disk.
2. **Repeating History during Redo:** Upon restart after a crash, the system reconstructs the state of the database immediately prior to the crash by replaying all logged actions (including those of uncommitted transactions and compensation records).
3. **Logging Changes during Undo:** When rolling back aborted or active transactions, undo actions are logged as **Compensation Log Records (CLRs)** to ensure the recovery process itself can withstand repeated crashes without repeating undone work.

The recovery procedure follows three phases:
- **Analysis Phase:** Scans the log forward from the most recent checkpoint to reconstruct the **Transaction Table** (active transactions at crash time) and the **Dirty Page Table (DPT)** with each dirty page's earliest un-flushed log sequence number (`recLSN`).
- **Redo Phase:** Scans the log forward starting from the smallest `recLSN` across all dirty pages, reapplying all logged operations to bring disk pages up to date ("repeating history").
- **Undo Phase:** Scans backward through the log, rolling back active/uncommitted transactions in reverse chronological order using their `prevLSN` pointers and logging CLRs.

---

## 3. Technology Stack

### Backend
- **Language:** Python 3.11+
- **API Framework:** FastAPI
- **ASGI Server:** Uvicorn
- **Testing:** pytest, httpx
- **Database Engine:** Custom from-scratch implementation (Zero external database dependencies)

### Frontend
- **Framework:** React 18+ (Vite)
- **Language:** TypeScript
- **Styling:** CSS3 (Modern Glassmorphism & Dark Mode)

---

## 4. Planned Implementation Phases

| Phase | Description | Status |
|---|---|---|
| **Phase 0** | Project Setup & Architectural Foundation | **Complete** |
| **Phase 1** | Page-Based Persistent Storage Engine | Upcoming |
| **Phase 2** | Buffer Pool Manager & Dirty Page Tracking | Upcoming |
| **Phase 3** | Write-Ahead Logging (WAL) & Fuzzy Checkpointer | Upcoming |
| **Phase 4** | Transaction Manager & Concurrency Control | Upcoming |
| **Phase 5** | ARIES Crash Recovery Engine (Analysis, Redo, Undo, CLRs) | Upcoming |
| **Phase 6** | Fault Injection, Crash Simulation & Benchmark Suite | Upcoming |
| **Phase 7** | Interactive Visualization Dashboard | Upcoming |

---

## 5. Getting Started

### Prerequisites
- Python 3.11 or higher
- Node.js (v18+) and npm

---

### Backend Setup

1. Navigate to the `backend/` directory:
   ```bash
   cd backend
   ```

2. Create and activate a Python virtual environment:
   - **Windows (PowerShell):**
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```
   - **Windows (CMD):**
     ```cmd
     python -m venv .venv
     .venv\Scripts\activate.bat
     ```
   - **macOS / Linux:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Start the backend server:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```
   The backend API will be running at `http://localhost:8000`.

5. Run backend tests:
   ```bash
   pytest
   ```

---

### Frontend Setup

1. Navigate to the `frontend/` directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Start the Vite development server:
   ```bash
   npm run dev
   ```
   The dashboard will be available at `http://localhost:5173`.

4. Build frontend for production:
   ```bash
   npm run build
   ```
