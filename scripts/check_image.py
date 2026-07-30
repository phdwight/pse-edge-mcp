#!/usr/bin/env python3
"""Image hygiene gate: assert the runtime image contains only what the app needs to run.

Invariant #5 is about *necessity*, not a megabyte budget. A size threshold is a poor
proxy — it passes while shipping a package manager and dev tooling, and fails when a
genuinely required dependency is large. So the checks here are:

  1. Installed Python distributions == the resolved runtime closure, exactly — where the
     closure includes whichever optional extras the image declares (--extra, mirroring
     the Dockerfile).
     Anything extra is bloat (a stray extra, a leaked dev dependency); anything
     missing means the image cannot actually run.
  2. No build toolchain and no package manager in the runtime image.
  3. No bytecode caches, tests, docs, or fixtures.
  4. Runs as a non-root user.

Size is measured and reported, never gated.

Usage: python scripts/check_image.py <image-ref> [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from packaging.markers import InvalidMarker, Marker

# Executables that have no business in a runtime image: compilers and package managers.
FORBIDDEN_BINARIES = ("gcc", "cc", "g++", "make", "ld", "uv", "pip", "pip3", "git")

# Paths that would mean test/doc material, or a stray source tree, leaked in.
FORBIDDEN_PATHS = ("/app/tests", "/app/docs", "/app/src", "/app/.git")


def normalise(name: str) -> str:
    """PEP 503 name normalisation, so `typing_extensions` == `typing-extensions`."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"command failed: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        sys.exit(2)
    return result.stdout


def expected_closure(target_platform: str, extras: list[str]) -> set[str]:
    """The runtime dependency closure for `target_platform`, straight from the lockfile.

    `uv export` resolves exactly what a production install pulls: no dev group, no
    optional extras we did not ask for. This is the source of truth the image is
    compared against, so the gate cannot drift from the declared dependencies.

    Exported lines carry environment markers, and the lock covers every platform — it
    lists pywin32 and colorama for Windows, which a Linux image is right not to have.
    Markers are therefore evaluated against the image's own platform rather than
    assumed away, so a genuinely absent Linux dependency still fails the check.
    """
    command = [
        "uv",
        "export",
        "--no-dev",
        "--no-hashes",
        "--no-emit-project",
        "--frozen",
        "--quiet",
        "--format",
        "requirements-txt",
    ]
    for extra in extras:
        command += ["--extra", extra]
    out = run(command)
    environment = {
        "sys_platform": target_platform,
        "platform_system": {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}.get(
            target_platform, "Linux"
        ),
        "os_name": "nt" if target_platform == "win32" else "posix",
        "platform_machine": "",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }

    names: set[str] = set()
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        requirement, _, marker_text = line.partition(";")
        if marker_text.strip():
            try:
                if not Marker(marker_text.strip()).evaluate(environment):
                    continue  # not required on this platform
            except InvalidMarker:
                pass  # unparseable marker: keep the requirement rather than skip it
        names.add(normalise(re.split(r"[=<>!\[ ]", requirement, maxsplit=1)[0]))
    return {n for n in names if n}


def installed_distributions(image: str) -> set[str]:
    script = (
        "import importlib.metadata as m;"
        "print('\\n'.join(d.metadata['Name'] for d in m.distributions()"
        " if d.metadata['Name']))"
    )
    out = run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c", script])
    return {normalise(line) for line in out.splitlines() if line.strip()}


def shell(image: str, script: str) -> str:
    return run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", script])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        dest="extras",
        help="optional-dependency group the image installs; must mirror the Dockerfile, "
        "otherwise a legitimately-installed extra looks like bloat (repeatable)",
    )
    args = parser.parse_args()
    image = args.image

    failures: list[str] = []
    notes: dict[str, object] = {}

    # 1. installed == declared runtime closure (plus the project itself)
    expected = expected_closure("linux", args.extras) | {normalise("pse-edge-mcp")}
    installed = installed_distributions(image)
    extra = sorted(installed - expected)
    missing = sorted(expected - installed)
    notes["package_count"] = len(installed)
    if extra:
        failures.append(
            "image ships distributions outside the runtime closure "
            f"(unnecessary bloat): {', '.join(extra)}"
        )
    if missing:
        failures.append(f"image is missing declared runtime dependencies: {', '.join(missing)}")

    # 2. no toolchain / package manager
    found = shell(
        image,
        "for b in " + " ".join(FORBIDDEN_BINARIES) + '; do command -v "$b" || true; done',
    ).split()
    if found:
        failures.append(f"build tooling present in runtime image: {', '.join(sorted(found))}")

    # 3. no caches, tests, docs, or leftover source tree
    # Scoped to /app: the base image's stdlib caches sit in a layer we cannot rewrite,
    # and deleting from it would only add a whiteout without reclaiming bytes.
    caches = shell(image, "find /app -name '__pycache__' -type d 2>/dev/null | wc -l").strip()
    notes["pycache_dirs"] = int(caches)
    if int(caches) > 0:
        failures.append(f"{caches} __pycache__ directories under /app (not needed to run)")

    leaked = shell(
        image,
        "for p in " + " ".join(FORBIDDEN_PATHS) + '; do test -e "$p" && echo "$p" || true; done',
    ).split()
    if leaked:
        failures.append(f"non-runtime paths present: {', '.join(sorted(leaked))}")

    # 4. non-root
    user = shell(image, "id -un").strip()
    notes["user"] = user
    if user == "root":
        failures.append("image runs as root")

    # Size: reported, not gated.
    size_bytes = int(run(["docker", "image", "inspect", image, "--format", "{{.Size}}"]).strip())
    arch = run(["docker", "image", "inspect", image, "--format", "{{.Architecture}}"]).strip()
    notes["size_mb"] = round(size_bytes / 1024 / 1024, 1)
    notes["architecture"] = arch

    if args.json:
        print(json.dumps({"failures": failures, **notes}, indent=2))
    else:
        print(f"image:        {image}")
        print(f"architecture: {arch}")
        print(f"size:         {notes['size_mb']} MB  (reported, not gated)")
        closure = "matches runtime closure" if not (extra or missing) else "MISMATCH"
        print(f"packages:     {len(installed)} ({closure})")
        print(f"user:         {user}")
        print(f"__pycache__:  {caches} directories")
        if failures:
            print("\nFAILED:")
            for f in failures:
                print(f"  - {f}")
        else:
            print("\nOK: image contains only what the app needs to run.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
