---
name: commit-push
description: Commits changes with a concise conventional commit message prefix (feat, fix, refactor, test, docs, style, chore) and pushes them to the remote repository. Use this skill whenever the user asks to commit and push, run /commit-push, or save and push changes to git.
---

# Commit and Push Skill

Automated runbook to safely verify, stage, commit with a standard prefix, and push changes to the remote git repository.

## Mandatory Pre-Commit Requirements

Before committing, **ALWAYS** perform the following three steps in order:

1. **Ensure all tests pass for both Webapp and Android**:
   - Run the full test suite using `run_all_tests.ps1`:
     ```powershell
     powershell -ExecutionPolicy Bypass -File .\run_all_tests.ps1
     ```
   - Alternatively run both individually:
     - Webapp (Jest): `npm test` in `webapp/`
     - Android (Gradle): `.\gradlew.bat test` in `expenseiq-android-app/`
   - If any test fails, do **not** proceed with commit and push until resolved.

2. **Update existing documentation for anything to be updated**:
   - Review the diff to identify any changed subsystems, behaviors, UI flows, data formats, or settings.
   - Update canonical architecture guides in `docs/architecture/` (or create a new guide if introducing a new subsystem).
   - Update `README.md` or `AGENTS.md` index tables if applicable.
   - Keep documentation accurate and aligned with the active codebase.

3. **Run `sync.bat` and stage any data changes**:
   - Run `sync.bat` to synchronize the latest cloud data for all configured accounts:
     ```powershell
     cmd /c .\sync.bat
     ```
   - Check `git status -s` to inspect any newly synced or modified data files (e.g. CSVs under `data/`).
   - Stage all data changes along with the code changes.

---

## Commit Message Prefix Standards

Every commit message **MUST** be a small, concise, one-liner using the appropriate prefix:

| Prefix | Description | Example |
| :--- | :--- | :--- |
| `feat:` | New feature, UI addition, or functional capability | `feat: show txn tags on dashboard` |
| `fix:` | Bug fix or regression patch | `fix: prevent duplicate sms txn import` |
| `refactor:` | Code restructuring without behavior changes | `refactor: extract date calculation helpers` |
| `style:` | CSS, layout, spacing, or visual adjustments | `style: adjust table padding on mobile` |
| `test:` | Adding, updating, or fixing unit/E2E tests | `test: add unit tests for dashboard tags` |
| `docs:` | Documentation, guide, or architecture updates | `docs: document virtual keyboard viewport safeguards` |
| `perf:` | Performance improvements or optimizations | `perf: optimize indexeddb query indexing` |
| `chore:` | Dependency bumps, build scripts, or project maintenance | `chore: update jest config and test runner` |

> [!IMPORTANT]
> - Always keep the commit message concise, one-liner, and under 72 characters.
> - Use imperative mood (e.g., `feat: add ...`, not `feat: added ...`).
> - Do not add a trailing period.

---

## Execution Runbook

### Step 1: Ensure All Tests Pass
Verify that all test suites pass with zero failures.

### Step 2: Update Existing Documentation
Check if any documentation needs updating based on the changes.

### Step 3: Inspect Status & Stage Changes
Review all modifications:
```powershell
git status -s
git diff
```
Stage all intended changes (including documentation, data files, and code):
```powershell
git add -A
```
Verify staged files with `git status -s`. Ensure no unintended files (temporary scratch files, secrets, local logs) are staged.

### Step 4: Formulate Commit Message
1. Identify the primary category of changes (feature, bugfix, styling, tests, docs, refactor, chore).
2. Draft a clear, concise one-liner with the corresponding prefix (e.g. `feat: approve scheduled transactions conditionally`).

### Step 5: Commit Changes
Execute the commit:
```powershell
git commit -m "<type>: <concise description>"
```

### Step 6: Push to Remote
Push to the active branch:
```powershell
git push
```
*(If upstream branch is not set, use `git push -u origin HEAD`)*

### Step 7: Confirm & Report
Verify that the working tree is clean and report the commit hash and one-liner message to the user:
```powershell
git log -1 --oneline
```

