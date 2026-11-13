Architectural Upgrades & Technical Debt Resolution

### Problem Statement:
When deploying or migrating the application to a new PC via USB, the operator often copies the `Today_Complaints/` directory (containing 13+ technician folders and 500+ already-downloaded PDFs) without an existing `complaints_state.db` or legacy `.txt` files. Currently, because `complaints_state.db` starts empty, Bulk Mode treats those 500+ existing PDFs as un-downloaded and re-downloads them from Oracle ERP (triggering Windows file-overwrite prompts).

### Architectural Requirement (Best Practice):
Implement a lightweight, idempotent **Startup Disk-to-DB Sync** mechanism so the SQLite database (`complaints_state.db`) automatically stays in sync with physical files on disk:
1. **Startup Directory Scan:** During database initialization (`state_manager.py`), scan all technician subdirectories inside `Today_Complaints/`.
2. **Validation & Backfill:** For every valid numeric PDF filename (`^\d+\.pdf$`) with file size > 0 bytes that is missing from `complaints_state.db`, automatically register it in SQLite with:
   - `complaint_id`: extracted numeric ID
   - `technician`: parent folder name
   - `status`: `'DOWNLOADED'` (so Bulk Mode skips re-downloading it, while Offline Batch Print still knows it needs physical printing)
   - `file_path`: resolved path to the PDF
3. **Runtime Defense-in-Depth (`engine.py`):** Before initiating Oracle GUI automation for a complaint in Bulk Mode, add a fast disk-existence fallback check: if a valid non-empty PDF already exists in the target technician folder on disk, backfill it to SQLite and skip the ERP download.
4. **Console Feedback:** If any orphaned PDFs on disk were newly synced into the database at startup, display a single clean console message (e.g., `[+] Synced 512 existing PDFs from Today_Complaints/ into database.`).

### Instructions:
Inspect `state_manager.py`, `engine.py`, `main.py`, and `build.py`. Ask me any clarifying questions if needed, then present a concise step-by-step plan to implement this and rebuild `dist/ERP_Automation_Suite/ERP_Automation_Suite.exe`.