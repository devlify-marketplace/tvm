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
"""Wheel artifact transport and local CI command contracts, without a TVM import."""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_local_ci():
    spec = importlib.util.spec_from_file_location("local_ci", ROOT / "tests/scripts/ci.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform", ["cpu", "gpu", "arm", "wasm"])
def test_local_wheel_build(monkeypatch, platform):
    ci = load_local_ci()
    calls = []
    monkeypatch.setattr(ci, "docker", lambda **kwargs: calls.append(kwargs))
    command, _, _ = ci.generate_command(platform, {}, "wheel build")
    command(cpp=True)
    scripts = calls[0]["scripts"]
    wheel = next(script for script in scripts if "-Cbuild-dir=" in script)
    assert "-Ccmake.build-type=RelWithDebInfo" in wheel
    assert "all;cpptest" in wheel
    assert not any("./3rdparty/tvm-ffi" in script for script in scripts)
    assert "--no-build-isolation --offline" in wheel
    assert any("task_python_install_wheels.sh" in script for script in scripts)
    calls.clear()
    command(skip_build=True, tests=["tests/python/example.py"])
    assert not any("uv build" in script for script in calls[0]["scripts"])


@pytest.mark.parametrize("succeed", [True, False])
def test_wheel_directory_upload_retries_each_transfer(tmp_path, succeed):
    # Exercise the existing S3 CLI/retry boundary with a directory bundle.
    for relative in [
        "ci/jenkins/scripts/s3.py",
        "ci/jenkins/scripts/retry.sh",
        "ci/jenkins/data.py",
    ]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    wheels = tmp_path / "build/wheels"
    wheels.mkdir(parents=True)
    (wheels / "apache_tvm.whl").write_text("tvm")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    aws = binaries / "aws"
    aws.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "path = pathlib.Path('attempts.json')\n"
        "calls = json.loads(path.read_text()) if path.exists() else []\n"
        "calls.append(sys.argv[1:])\n"
        "path.write_text(json.dumps(calls))\n"
        f"sys.exit(0 if {succeed!r} and len(calls) == 3 else 1)\n"
    )
    aws.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text('#!/bin/sh\necho "$1" >> delays.txt\n')
    sleep.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            "ci/jenkins/scripts/s3.py",
            "--action",
            "upload",
            "--bucket",
            "test",
            "--prefix",
            "cpu",
            "--bundle",
            "wheels",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == succeed, result.stdout + result.stderr
    attempts = json.loads((tmp_path / "attempts.json").read_text())
    assert len(attempts) == 3
    assert all(
        call[-3:] == ["--recursive", "build/wheels", "s3://test/cpu/build/wheels"]
        for call in attempts
    )
    delays = [int(line) for line in (tmp_path / "delays.txt").read_text().splitlines()]
    assert len(delays) == 2 and all(30 <= delay <= 200 for delay in delays)


@pytest.mark.parametrize("fail", [False, True])
def test_local_interactive_keeps_cache_stats(tmp_path, monkeypatch, fail):
    ci = load_local_ci()
    monkeypatch.setattr(ci, "check_docker", lambda: None)
    monkeypatch.setattr(ci, "cmd", lambda *args, **kwargs: None)
    monkeypatch.setattr(ci, "SCRIPT_DIR", tmp_path / "scripts")
    monkeypatch.setattr(ci, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("USE_SCCACHE", raising=False)
    monkeypatch.setenv("DEBUG", "1")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ["sccache", "bash"]:
        binary = binaries / name
        binary.write_text(f'#!/bin/sh\necho "{name} $*" >> "$CALL_LOG"\n')
        binary.chmod(0o755)
    ci.docker("interactive", "ci_cpu", ["false" if fail else "true"], {}, True)
    result = subprocess.run(
        [shutil.which("bash"), str(ci.SCRIPT_DIR / "interactive.sh")],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "CALL_LOG": str(tmp_path / "calls"),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode != 0) == fail
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == [
        "sccache --start-server",
        "sccache --show-stats",
        "sccache --show-stats",
        "bash ",
    ]


@pytest.mark.parametrize("wheel_docs", [False, True])
def test_docs_import_path(tmp_path, monkeypatch, wheel_docs):
    # Run the real Sphinx path bootstrap without requiring Sphinx extensions.
    # A stale --target=python FFI package must not precede Jenkins' wheel install.
    monkeypatch.chdir(ROOT / "docs")
    source_python = str(ROOT / "python")
    monkeypatch.setattr(sys, "path", [path for path in sys.path if path != source_python])
    monkeypatch.setenv("TVM_DOCS_USE_WHEEL", "1" if wheel_docs else "0")
    config = ROOT / "docs/conf.py"
    bootstrap = config.read_text().split("# -- General configuration")[0]
    exec(compile(bootstrap, str(config), "exec"), {"__file__": str(config)})
    assert (source_python in sys.path) != wheel_docs


@pytest.mark.parametrize(
    "host_limit,caller_limit,expected", [(None, None, "2"), ("7", None, "7"), ("7", "9", "9")]
)
def test_local_parallel_limit_crosses_docker_boundary(
    tmp_path, monkeypatch, host_limit, caller_limit, expected
):
    ci = load_local_ci()
    calls = []
    monkeypatch.setattr(ci, "check_docker", lambda: None)
    monkeypatch.setattr(ci, "cmd", lambda command: calls.append(command))
    monkeypatch.setattr(ci, "SCRIPT_DIR", tmp_path / "scripts")
    monkeypatch.setattr(ci, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ci, "NPROC", 8)
    monkeypatch.setenv("CI_NUM_EXECUTORS", "3")
    if host_limit is None:
        monkeypatch.delenv("CMAKE_BUILD_PARALLEL_LEVEL", raising=False)
    else:
        monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", host_limit)
    env = {} if caller_limit is None else {"CMAKE_BUILD_PARALLEL_LEVEL": caller_limit}
    ci.docker("build", "ci_cpu", ["true"], env, False)
    args = calls[0]
    index = args.index(f"CMAKE_BUILD_PARALLEL_LEVEL={expected}")
    assert args[index - 1] == "--env"


@pytest.mark.parametrize(
    "script", ["task_python_unittest.sh", "task_python_docs.sh", "task_web_wasm.sh"]
)
@pytest.mark.parametrize("wheel_mode", [False, True])
def test_shared_scripts_preserve_legacy_and_wheel_modes(tmp_path, script, wheel_mode):
    scripts = tmp_path / "tests/scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / "tests/scripts" / script, scripts / script)
    # Substitute only the install boundary; execute the real caller's mode routing.
    (scripts / "task_python_install_wheels.sh").write_text('echo WHEEL_SETUP >> "$CALL_LOG"\n')
    for directory in ["docs", "web", "bin"]:
        (tmp_path / directory).mkdir()
    for tool in ["uv", "python3", "make"]:
        binary = tmp_path / "bin" / tool
        binary.write_text(
            f'#!/bin/sh\necho "{tool}:$*" >> "$CALL_LOG"\n'
            + ("exit 77\n" if tool == "make" else "")
        )
        binary.chmod(0o755)
    result = subprocess.run(
        [shutil.which("bash"), str(scripts / script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CALL_LOG": str(tmp_path / "calls"),
            "TVM_TEST_USE_WHEEL": "1" if wheel_mode else "0",
            "IS_LOCAL": "0",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if script == "task_python_unittest.sh" else 77), result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert ("WHEEL_SETUP" in calls) == wheel_mode
    dependency_calls = [call for call in calls if call.startswith("uv:")]
    if wheel_mode:
        assert not dependency_calls
    else:
        assert "uv:pip install -v --target=python ./3rdparty/tvm-ffi/" in dependency_calls


def test_wheel_mode_does_not_fall_back_when_install_fails(tmp_path):
    scripts = tmp_path / "tests/scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / "tests/scripts/task_python_unittest.sh", scripts)
    (scripts / "task_python_install_wheels.sh").write_text("return 37\n")
    result = subprocess.run(
        [shutil.which("bash"), str(scripts / "task_python_unittest.sh")],
        cwd=tmp_path,
        env={**os.environ, "TVM_TEST_USE_WHEEL": "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 37
    assert "uv pip install" not in result.stderr


@pytest.mark.parametrize("bootstrap_fails", [False, True])
def test_build_bootstrap_precedes_offline_build(tmp_path, monkeypatch, bootstrap_fails):
    import shlex

    ci = load_local_ci()
    calls = []
    monkeypatch.setattr(ci, "docker", lambda **kwargs: calls.append(kwargs))
    command, _, _ = ci.generate_command("cpu", {}, "wheel build")
    command()
    commands = [script for script in calls[0]["scripts"] if script.startswith("uv ")]
    bootstrap, build = commands
    arguments = shlex.split(bootstrap)
    # The declared build requirements are a flat string list; keep this test usable on Python 3.10.
    requires = re.search(r"(?ms)^requires = (\[.*?\])", (ROOT / "pyproject.toml").read_text())
    expected = ast.literal_eval(requires[1])
    assert arguments[:5] == ["uv", "pip", "install", "--python", "python3"]
    assert arguments[5:] == expected
    assert "--no-build-isolation --offline --python python3" in build
    binary = tmp_path / "uv"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "with pathlib.Path('calls').open('a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit(17 if {bootstrap_fails!r} and sys.argv[1] == 'pip' else 0)\n"
    )
    binary.chmod(0o755)
    result = subprocess.run(
        [shutil.which("bash"), "-ec", "\n".join(commands)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (17 if bootstrap_fails else 0)
    recorded = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert [call[0] for call in recorded] == (["pip"] if bootstrap_fails else ["pip", "build"])
