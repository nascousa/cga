# CGA Plugin SDK v1.0.0

This Python SDK develops **reviewed first-party, in-process integrations**.
It is not a marketplace, hot loader, isolation boundary or permission broker.
No archive can be installed into a running CGA by this SDK.
The host plugin framework is pending CGA source work, not part of this standalone
SDK wheel or proof that the older public CGA release supports these integrations.

## Quick start

Python 3.11+ is required. Install the wheel from the joint development-kit
release, or install this source directory with `python -m pip install .`.

```powershell
python -m cga_plugin_sdk init .\sample-inspector --id sample_inspector
python -m cga_plugin_sdk validate .\sample-inspector
python -m pip install pytest
python -m pytest .\sample-inspector\test_plugin.py
python -m cga_plugin_sdk package .\sample-inspector --output .\dist
python -m cga_plugin_sdk schema --output .\plugin.schema.json
```

Validation never imports plugin code. It rejects unknown manifest fields,
core IDs, undeclared entrypoints, duplicate/case-colliding paths, traversal,
symlinks, oversized content, key files and inconsistent network/secret
declarations. Only declared resources enter the deterministic ZIP. Existing
output archives and scaffold directories are never overwritten.

The generated example is read-only and needs a project scope. Its
`execute(task, started_at, started_clock)` signature and four-field return tuple
match the existing CGA scheduler adapter interface. `ExecutionResult` bounds JSON
evidence and prevents reporting HTTP errors as success. The SDK does not
silently convert exceptions into success.

## Integrating into CGA

1. Develop and test outside core modules. Declare every external host, secret
   environment reference, permission, timeout and retention policy.
2. Submit source, a manifest, tests, licenses, data-flow/rollback evidence and
   the package hash for review under the versioned standard and approval policy.
3. A maintainer manually adds the implementation to `backend.plugins.catalog`,
   its resource ownership, task mapping/dispatch and optional UI/tool registration.
   The manifest is a review/package contract, **not an automatic registration**.
4. Execution must use `backend.plugins.host.execution` or `require_plugin`
   and the existing administrator/project authorization. Keep imports lazy when
   omitted, preserve core APIs and test core-only distributions.
   Current host leases are process-local; this does not add multi-worker isolation.
5. Test disable-during-execution conflicts, disabled/omitted endpoints, secrets,
   provider errors, bounded evidence, configuration migration and uninstall.
   Runtime uninstall preserves configuration/history.

The generated example requires no SDK-only task type. It is not registered or
runnable inside CGA until the above host integration is approved. The declared minimum CGA version is an
integration prerequisite, not proof that every installed build has the API.
Manifest permissions are declarations; same-process Python can bypass them.
Only trusted reviewed code belongs in this first release.

## Approval and release

Technical validation and hashes do **not** approve a plugin. The release gate
reads current GitHub reviews and successful checks, excludes the author and
requires two distinct authorized maintainers/security reviewers on the exact
head commit. No approval file supplied by the plugin author is trusted.
Missing reviewer configuration blocks release. Revocation means remove a
version from future distributions, disable it through the normal lease-aware
host controls, preserve evidence and publish an advisory; do not delete data.

SDK releases use `plugin-sdk-v1.0.0`, separately from CGA's API image tags.
The joint kit contains English/Chinese standards, approval policy, this guide,
wheel, source distribution, JSON Schema, read-only example, approval/build tools
and SHA-256 checksums. Formal workflow
publication additionally produces GitHub artifact attestations; local ZIPs
and draft releases are not attested approvals.
