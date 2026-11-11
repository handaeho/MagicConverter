# Windows CI entry point

Run from a clean Windows x64 checkout with Python 3.14.7. Dependency versions,
hashes, wheels and vendor inputs come from `requirements/` and `packaging/`.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/ci/run_windows_validation.ps1 -Phase All
```

Use `-Offline` when the reviewed wheel/vendor caches are already available.
The five phases can also be invoked separately: `Build`, `Source`, `Frozen`,
`External`, `Reproduce`. The GitHub workflow invokes these same phases.
An external Windows runner attached to GitBucket can call this command too;
no CI server is configured in the currently supplied environment.

The release directory is computed from `magic_converter.__version__`.
Per-phase receipts are invalidated before version/dependency checks and record
failure on nonzero native exits, including early initialization failures.
Normal stderr on exit 0 is permitted on Windows PowerShell 5 and PowerShell 7.
Use a fresh checkout: validators deliberately reject reused fixture folders.

These phases run without opting into installed Office automation.
Optional native tests and their skips are separate from validation on a host
with licensed Office. Local receipts do not attest remote execution;
the CI service must retain its actual run/commit record and artifacts.

The source-review bundle is an internal preparation artifact. The GitHub
upload explicitly excludes its ZIP. Public policy flags, project licensing,
corresponding-source sufficiency and publication are never approved by CI.
