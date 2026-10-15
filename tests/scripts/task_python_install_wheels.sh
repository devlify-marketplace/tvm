#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# Source this file so subsequent Python invocations use the wheel environment.
# Docker defaults PYTHONPATH to the checkout; never let it shadow the artifacts.
unset PYTHONPATH TVM_LIBRARY_PATH TVM_FFI_LIBRARY_PATH
export PYTHONNOUSERSITE=1
# Installed TVM still needs checkout resources such as web/dist for Wasm linking.
export TVM_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TVM_WHEEL_DIR="$(realpath "${TVM_WHEEL_DIR:-$(pwd)/build/wheels}")"
# Preserve the image's active environment (often itself a venv), including when
# local ci.py sources this setup again for a named test suite.
export TVM_IMAGE_PYTHON="${TVM_IMAGE_PYTHON:-$(command -v python3)}"
export TVM_IMAGE_SITE_PACKAGES="${TVM_IMAGE_SITE_PACKAGES:-$("$TVM_IMAGE_PYTHON" -I -c 'import site; print("\n".join(site.getsitepackages()))')}"
export VIRTUAL_ENV="${TVM_WHEEL_DIR%/}/../wheel-venv"
uv venv --offline --python "$TVM_IMAGE_PYTHON" --clear "$VIRTUAL_ENV"
# Append the image dependencies after our wheels without executing its .pth files:
# system-site-packages can install editable-import hooks ahead of the wheel.
TVM_WHEEL_SITE="$("$VIRTUAL_ENV/bin/python3" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
printf '%s\n' "$TVM_IMAGE_SITE_PACKAGES" > "$TVM_WHEEL_SITE/image-dependencies.pth"
export PATH="$VIRTUAL_ENV/bin:$PATH"
# Keep the checkout available to tests and build its pinned FFI into this environment.
# FFI declares its own isolated build requirements; do not reuse TVM's build overlay.
export TVM_FFI_SOURCE="$TVM_HOME/3rdparty/tvm-ffi"
uv pip install --python "$VIRTUAL_ENV/bin/python3" --no-deps --reinstall "$TVM_FFI_SOURCE"
# Match the explicit prerequisite in the legacy Python test setup.
uv pip install --python "$VIRTUAL_ENV/bin/python3" --no-deps apache-tvm-ffi-orcjit==0.1.2
uv pip install --python "$VIRTUAL_ENV/bin/python3" --no-deps --offline --reinstall \
    "$TVM_WHEEL_DIR"/apache_tvm-*.whl

# Fail before pytest if a source/editable install or native override leaked in.
python3 - <<'PYTHON'
import json
import os
import sys
from importlib.metadata import distribution, requires, version
from pathlib import Path
from urllib.parse import unquote, urlparse
from packaging.requirements import Requirement

# Core FFI comes from pinned source and ORCJIT is pinned explicitly.
# Check the wheel requirements without invoking a resolver or hardcoding a floor.
for text in requires("apache-tvm") or []:
    requirement = Requirement(text)
    if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
        continue
    installed = version(requirement.name)
    assert requirement.specifier.contains(installed, prereleases=True), (text, installed)

import tvm
import tvm_ffi

prefix = Path(sys.prefix).resolve()
path = Path(tvm.__file__).resolve()
assert path.is_relative_to(prefix), path
print(f"Installed tvm: {path}")
# Verify FFI was installed from this checkout and loads entirely from this environment.
source = json.loads(distribution("apache-tvm-ffi").read_text("direct_url.json"))
assert Path(unquote(urlparse(source["url"]).path)).resolve() == Path(os.environ["TVM_FFI_SOURCE"]).resolve()
assert not source.get("dir_info", {}).get("editable", False), source
for path in (Path(tvm_ffi.__file__).resolve(), Path(tvm_ffi.LIB._name).resolve()):
    assert path.is_relative_to(prefix), path
    print(f"Source-built FFI: {path}")
assert {"tvm_runtime", "tvm_compiler"} <= tvm.base._LOADED_LIBS.keys()
for name, library in tvm.base._LOADED_LIBS.items():
    path = Path(library._name).resolve()
    assert path.is_relative_to(prefix), (name, path)
    print(f"Installed {name}: {path}")
PYTHON
