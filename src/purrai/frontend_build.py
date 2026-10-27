"""Validate and select the frontend files produced by the formal Vite build."""

import hashlib
import json
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "frontend"
INPUTS = ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")
REBUILD = "前端构建缺失或已过期，请在 frontend 目录执行 npm run build。"


class FrontendBuild:
    def __init__(self, root, source=SOURCE):
        self.root, self.source = Path(root).resolve(), Path(source).resolve()
        self.files, self.signature, self.result = set(), None, None

    def status(self):
        try:
            raw = (self.root / "build-manifest.json").read_bytes()
            manifest = json.loads(raw)
            outputs, inputs = manifest["outputs"], manifest["inputs"]
            if manifest["schema"] != 1 or "index.html" not in outputs or not inputs:
                raise ValueError("incomplete build")
            paths = [(self.root / name, digest) for name, digest in outputs.items()]
            if any(not path.resolve().is_relative_to(self.root) for path, _ in paths):
                raise ValueError("invalid build path")
            # A source checkout must match its build exactly. A deployed artifact
            # without sources can still verify its complete output receipt.
            if (self.source / "src").is_dir():
                actual = {path.relative_to(self.source).as_posix() for path in (self.source / "src").rglob("*") if path.is_file()} | set(INPUTS)
                if actual != set(inputs):
                    raise ValueError("source set changed")
                paths += [(self.source / name, inputs[name]) for name in sorted(actual)]
            signature = (hashlib.sha256(raw).hexdigest(), tuple(
                (str(path), path.stat().st_mtime_ns, path.stat().st_size) for path, _ in paths
            ))
            if signature != self.signature:
                if any(hashlib.sha256(path.read_bytes()).hexdigest() != digest for path, digest in paths):
                    raise ValueError("build content changed")
                self.signature = signature
                self.files = set(outputs)
                self.result = {"ready": True, "reason": "当前构建已核对", "identity": signature[0]}
            return self.result
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.files, self.signature = set(), None
            self.result = {"ready": False, "reason": REBUILD, "identity": ""}
            return self.result
