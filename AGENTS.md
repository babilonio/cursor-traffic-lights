# Agent Instructions

## Project

Challenge description and rules: [`traffic-lights-arena/README.md`](traffic-lights-arena/README.md).

Control the city by editing only `traffic-lights-arena/controller.py`. Run locally with `python run.py` from that directory (see the README for setup and submit flow).

Before improving the controller, read [`NEXT_AGENT_GUIDE.md`](NEXT_AGENT_GUIDE.md).
It contains the current verified metrics, simulator constraints, benchmark
workflow, known failure modes, and acceptance gates for the next iteration.

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
