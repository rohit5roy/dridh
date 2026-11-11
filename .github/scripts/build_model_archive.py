#!/usr/bin/env python3
"""Build the embedding-model archive that each release carries next to the wheel.

FastEmbed downloads Code Reacher's model (BAAI/bge-small-en-v1.5, as Qdrant's quantised ONNX copy) from Hugging Face
on first use. The release ships it instead, so an install needs no Hugging Face access and works offline:

  dridh-embedding-model.tar.gz         the FastEmbed cache for the model, unpacked by install.sh into
                                       ~/.cache/dridh/fastembed (FASTEMBED_CACHE_PATH or XDG_CACHE_HOME override it)
  dridh-embedding-model.tar.gz.sha256  its checksum, checked before anything is unpacked

FastEmbed itself fills an empty cache, so the layout is exactly the one it looks up. The model is then loaded with
the network disabled to prove the cache is complete. The archive also holds dridh-embedding-model.json (model,
source, revision, licence) and the model's licence notice. Archive member order and timestamps are fixed, so the
same model revision gives the same checksum.

Usage (with fastembed installed): build_model_archive.py --out DIR
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

MODEL = "BAAI/bge-small-en-v1.5"  # must match FASTEMBED_MODEL in dridh/src/dridh/code_reacher/embeddings.py
ARCHIVE = "dridh-embedding-model.tar.gz"
MANIFEST = "dridh-embedding-model.json"
LICENCE_NOTICE = """\
The embedding model in this archive is BAAI/bge-small-en-v1.5 (https://huggingface.co/BAAI/bge-small-en-v1.5),
as the quantised ONNX copy Qdrant/bge-small-en-v1.5-onnx-Q (https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q),
both published under the MIT licence. Dridh redistributes it unmodified, as FastEmbed would download it.

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

# Loads the model from the given cache with every network path to Hugging Face disabled, and embeds one text.
OFFLINE_CHECK = """
import sys
from fastembed import TextEmbedding
model = TextEmbedding(model_name=sys.argv[1], cache_dir=sys.argv[2], local_files_only=True)
vector = next(iter(model.embed(["validate an email address"])))
assert len(vector) == 384, len(vector)
print("offline load ok")
"""


def _description() -> dict[str, str]:
    from fastembed import TextEmbedding

    for entry in TextEmbedding.list_supported_models():
        if entry["model"] == MODEL:
            return {"source": entry["sources"]["hf"], "licence": entry.get("license", ""), "file": entry["model_file"]}
    sys.exit(f"FastEmbed does not list {MODEL}")


def _revision(cache: Path, source: str) -> str:
    refs = cache / f"models--{source.replace('/', '--')}" / "refs" / "main"
    return refs.read_text().strip() if refs.is_file() else "unknown"


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, 0
    tar.addfile(info, io.BytesIO(data))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    import fastembed
    from fastembed import TextEmbedding

    described = _description()
    with tempfile.TemporaryDirectory(prefix="dridh-model-") as tmp:
        cache = Path(tmp) / "cache"
        TextEmbedding(model_name=MODEL, cache_dir=str(cache))  # downloads into the empty cache
        offline = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "HF_ENDPOINT": "http://127.0.0.1:9"}
        subprocess.run([sys.executable, "-c", OFFLINE_CHECK, MODEL, str(cache)], env=offline, check=True)  # noqa: S603

        manifest = {
            "model": MODEL,
            "source": described["source"],
            "revision": _revision(cache, described["source"]),
            "licence": described["licence"] or "mit",
            "fastembed": fastembed.__version__,
        }
        files = sorted(p for p in cache.rglob("*") if (p.is_file() or p.is_symlink()) and ".locks" not in p.relative_to(cache).parts)
        archive = out / ARCHIVE
        with archive.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            _add(tar, MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
            _add(tar, "dridh-embedding-model-LICENSE.txt", LICENCE_NOTICE.encode())
            for path in files:
                info = tar.gettarinfo(str(path), arcname=path.relative_to(cache).as_posix())
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                if info.issym():
                    tar.addfile(info)
                else:
                    info.mode = 0o644
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / f"{ARCHIVE}.sha256").write_text(f"{digest}  {ARCHIVE}\n")
    size = archive.stat().st_size / 1e6
    print(f"{ARCHIVE}: {size:.1f} MB, sha256 {digest}")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
