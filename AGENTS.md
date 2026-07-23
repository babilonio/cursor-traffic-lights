# Agent Instructions

## Virtual environment

- Virtual environment: `.venv` (already in `.gitignore`)
- numpy: 2.2.6 (Python 3.10)

To activate in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by execution policy, run:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate again. After that, `python` and `pip` will use the virtual environment.
