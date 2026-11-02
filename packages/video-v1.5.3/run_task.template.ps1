# Retired compatibility entrypoint. Do not dispatch or resume historical tasks.
[Console]::Error.WriteLine('LEGACY_RUNNER_RETIRED: JyRun is retired. Use the documented worker entrypoint for a new, explicitly configured task.')
exit 2
