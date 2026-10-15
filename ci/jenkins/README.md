<!--- Licensed to the Apache Software Foundation (ASF) under one -->
<!--- or more contributor license agreements.  See the NOTICE file -->
<!--- distributed with this work for additional information -->
<!--- regarding copyright ownership.  The ASF licenses this file -->
<!--- to you under the Apache License, Version 2.0 (the -->
<!--- "License"); you may not use this file except in compliance -->
<!--- with the License.  You may obtain a copy of the License at -->

<!---   http://www.apache.org/licenses/LICENSE-2.0 -->

<!--- Unless required by applicable law or agreed to in writing, -->
<!--- software distributed under the License is distributed on an -->
<!--- "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY -->
<!--- KIND, either express or implied.  See the License for the -->
<!--- specific language governing permissions and limitations -->
<!--- under the License. -->

# TVM CI

TVM runs CI jobs on every commit to an open pull request and to branches in the apache/tvm repo (such as `main`). These jobs are essential to keeping the TVM project in a healthy state and preventing breakages.

## Jenkins

Jenkins runs all of the linux-based TVM CI-enabled regression tests. This includes tests against accelerated hardware such as GPUs. It excludes those regression tests that run against hardware not available in the cloud (those tests aren't currently exercised in TVM CI). The tests run by Jenkins represent most of the merge-blocking tests (and passing Jenkins should mostly correlate with passing the remaining Windows/Mac builds).

## GitHub Actions

GitHub Actions is used to run Windows jobs, MacOS jobs, and various on-GitHub automations. These are defined in [`.github/workflows`](../../.github/workflows/). These automations include bots to:
* [allow non-committers to merge approved / CI passing PRs](https://discuss.tvm.apache.org/t/rfc-allow-merging-via-pr-comments/12220)

https://github.com/apache/tvm/actions has the logs for each of these workflows. Note that when debugging these workflows changes from PRs from forked repositories won't be reflected in the PR. These should be tested in the forked repository first and linked in the PR body.

# Jenkins CI

TVM uses Jenkins for running Linux continuous integration (CI) tests on
[branches](https://ci.tlcpack.ai/job/tvm/) and
[pull requests](https://ci.tlcpack.ai/job/tvm/view/change-requests/) through a
build configuration specified in a [`Jenkinsfile`](generated/).
Other jobs run in GitHub Actions for Windows and MacOS jobs.

## Pipeline entry points

The five `generated/*_jenkinsfile.groovy` paths remain Jenkins job entry points.
Despite the historical directory name, these are native Groovy sources: edit
and review them directly. `generated/common.groovy` shares preparation, checkout,
Docker setup, artifacts and spot fallback policy. Platform commands and deployment
stay visible in their respective job files. Image tags remain in
`docker-images.ini`; artifact bundles remain in `artifacts.py`. Bundle entries may
contain repository-relative globs such as `build/dist/*.whl`. Upload expands
patterns locally, sorts and deduplicates the paths, and keeps their relative
paths under the S3 prefix. Unmatched patterns produce a warning; missing literal
optional artifacts retain their warning-and-skip behavior. Directories keep
recursive upload behavior. Download still copies the complete prefix without
expanding local or remote globs.
Use `s3.py --artifacts NAME...` to select registry entries or `--paths PATH...`
to select raw paths; the selections are mutually exclusive. Existing Jenkinsfiles
can keep `--bundle` and `--items`, which are aliases with the same semantics.
The shared helper's legacy `upload(tag, options)` passes those options unchanged.
`artifacts.py` exposes the declarative `ARTIFACTS` registry; there is no `data.py`
compatibility module.

The shared helper groups internal dependencies first, public entry points next,
and retained legacy functions last. Existing function signatures, shared variables,
startup and retry behavior remain available to the platform Jenkinsfiles.

Host-side commands live in `scripts/`. GitHub bot and release API helpers live
in `../github/`. The retained `determine_docker_images.py` supports both
Jenkins image selection and local `--lookup-only` lookup; `should_rebuild_docker.py`
and the standalone skip commands retain their command-line interfaces.

### Trust and rollout

Each entry point evaluates the shared helper returned by `readTrusted` at
Jenkins' selected SCM revision. Untrusted PRs that modify or delete the helper
fail immediately, before node allocation, checkout, or credentials. Missing
helpers, interrupted reads, and evaluation errors also propagate directly.
No alternative helper is fetched or evaluated. Trusted named branches retain
their selected helper revision.

Cancellation runs once before preparation. During preparation, the normal source
merge pins the PR target revision for subsequent stages, including release
targets. Host scripts and artifact metadata follow that pinned target revision
for PR authors outside `CONTRIBUTORS.md`.

The pinned target must contain the coordinated `artifacts.py` and `s3.py` update.
Before that update reaches a target, a Jenkins-trusted PR from an author outside
its contributor list cannot restore the renamed registry, and that build fails.
Use a trusted named branch for the transition; do not bypass the trusted-file
checkout or mix the old target's script with a PR-owned registry.

The obsolete `ci/scripts/` copies and old node-info path have been removed now
that main's Jenkinsfiles use the canonical layout. The live `task_build.py`,
source-mode test scripts and artifact interfaces remain available. Older target
branches must retain their matching legacy commands; they are not supplied by
this checkout.

Before enabling the migration on production jobs, validate it with the existing
Jenkins plugin versions and sandbox settings on a trusted branch: run all five
entry points, suspend/resume across a node step, and exercise spot loss, a normal
build-command failure, superseded builds, and a failed on-demand retry. Check
trusted helper changes, untrusted PRs without helper changes, and refusal of
untrusted modified/deleted helpers, including a release-target PR. Verify image
overrides and disabled deployment gates before
checking enabled docs/Docker deployment in the normal trusted deployment context.
Local mocked execution checks pipeline control flow but cannot certify Jenkins
CPS serialization, sandbox approvals, SCM trust configuration, or real agent loss.
No job path or controller reconfiguration is part of this migration.

# Infrastructure

The community's infrastructure configuration and documentation live in
[tlc-pack/ci](https://github.com/tlc-pack/ci). Coordinate external callers when
changing any retained script interface.

## Wheel build and test artifacts

Normal builds use `uv build --wheel --no-build-isolation --offline` with the
existing platform CMake configuration. `uv build` builds only TVM; it does not
resolve runtime dependencies. CPU, ARM and Wasm host builds request `all;cpptest`
and run C++ tests in that same native tree before uploading anything. Python
consumers install the exact TVM wheel with `--no-deps --offline`, clear checkout
path overrides, and verify the package and native-library paths. FFI comes from
the checked-out submodule. GPU Java tests also receive their existing native
runtime libraries; the separate runtime-only compile check and Emscripten runtime
build remain.

Immediately before building, the same container and Python install the declared
`pyproject.toml` build requirements (scikit-build-core, setuptools-scm and
mlc-z3-static) and their build dependencies. A
failed bootstrap stops the build. Build requirements install into a writable local
directory, which is added to `PYTHONPATH` only for the build command. Both commands
select the original image Python, preserving its existing dependencies without
modifying the image environment or exposing the build overlay to test consumers.
`uv build` then runs offline without isolation.

The selected images must already provide compatible runtime and test dependencies,
apart from core FFI and its ORCJIT addon. Consumers install the checkout's pinned
FFI source into the writable test environment with `--no-deps`, using its declared
isolated build
requirements, plus the explicit `apache-tvm-ffi-orcjit==0.1.2 --no-deps` prerequisite
used by legacy Python tests. They then install the transferred TVM wheel with
`--no-deps --offline`.
The checkout remains available for tests and resources through `TVM_HOME`, including
Wasm assets under `web/dist`, without adding its Python package ahead of the
wheel. Provenance checks verify both TVM/native libraries and source-built FFI
load from the test environment; other missing runtime dependencies
remain errors rather than permission to fetch replacements.

`CMAKE_BUILD_PARALLEL_LEVEL` is passed through Docker as an environment variable.
An explicit value wins; otherwise builds use at least one CPU and divide the
node CPU count by `CI_NUM_EXECUTORS`. Both the wheel backend and specialized
native commands use that bound without an additional parallel flag.

### Current-main pipeline compatibility

Jenkins may select the old trusted-main Jenkinsfile while checking out a newer
PR revision. Retain `tests/scripts/task_build.py`, the original raw-artifact
bundles, and the source-mode test interfaces until trusted main has migrated.
The wheel entrypoints opt in after preparation, leaving the shared helper's
default unchanged for older entrypoints. They and local wheel commands explicitly
pass `TVM_TEST_USE_WHEEL=1`; that mode installs transferred TVM wheels with the
explicit FFI setup above. Calls without the flag retain the old source setup and
dependency installation behavior. A missing wheel in wheel mode is an error, not a fallback
to source mode.
