# Bounded delegation

The coordinating session decides whether to delegate.
Give each worker one bounded task with named inputs, expected outputs, and a tool budget.
Use the worker definition when children must be disabled.
Name the model in the dispatch when model selection is available.
Keep a configurable concurrency limit and assign disjoint files to simultaneous workers.
Keep shared files with the coordinating session.

Only the coordinating session may start workers.
Workers may not start children through tools or shell commands.
Use the native spawn depth limit where the installed client supports it.
The tool allowlist and the no-children instruction are additional controls.

A defect fix return includes the test failing on the original code and passing on the corrected code.
An extra behavior change needs its own evidence.
The coordinating session reviews the changed files and executes relevant checks before accepting a return.
Read large logs in bounded ranges around the evidence.
Do not delete directory trees.
