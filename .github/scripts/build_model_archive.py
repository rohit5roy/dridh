#!/usr/bin/env python3
"""Build the embedding-model archive that each release carries next to the wheel.

Code Reacher's model is thenlper/gte-small at one pinned revision of its files (``FASTEMBED_MODEL`` and
``FASTEMBED_REVISION`` in dridh/src/dridh/code_reacher/embeddings.py). Without this archive, Dridh downloads it from
Hugging Face on first use. The release ships it instead, so an install needs no Hugging Face access and works offline:

  dridh-embedding-model.tar.gz         the model folder exactly as Dridh keeps it, unpacked by install.sh into
                                       ~/.cache/dridh/fastembed (FASTEMBED_CACHE_PATH or XDG_CACHE_HOME override it)
  dridh-embedding-model.tar.gz.sha256  its checksum, checked before anything is unpacked

The folder is made by Dridh's own ``model_files()`` into an empty cache, so the layout is the one Dridh looks up
(``thenlper--gte-small/<revision>/``, with the tokenizer's context length set and the ``.dridh-ready`` marker). The model
is then loaded by Dridh's ``FastEmbedProvider`` with the network disabled, to prove the archive is complete. The
archive also holds dridh-embedding-model.json (model, revision, licence) and the model's licence notice. Member order
and timestamps are fixed, so the same model revision gives the same checksum.

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

SOURCE = Path(__file__).resolve().parents[2] / "dridh" / "src"
ARCHIVE = "dridh-embedding-model.tar.gz"
MANIFEST = "dridh-embedding-model.json"
LICENCE_NOTICE = """\
The embedding model in this archive is thenlper/gte-small (https://huggingface.co/thenlper/gte-small), published
under the MIT licence. Dridh redistributes its ONNX export and tokenizer files unmodified, except that the
tokenizer_config.json model_max_length is set to 512 (the published file gives no usable value).

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

# Dridh's own provider loads the model from the cache with every network path to Hugging Face disabled.
OFFLINE_CHECK = """
import sys
sys.path.insert(0, sys.argv[1])
from dridh.code_reacher.embeddings import FastEmbedProvider
vector = FastEmbedProvider().embed(["validate an email address"])[0]
assert len(vector) == 384, len(vector)
print("offline load ok")
"""


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, 0
    tar.addfile(info, io.BytesIO(data))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    import fastembed

    with tempfile.TemporaryDirectory(prefix="dridh-model-") as tmp:
        cache = Path(tmp) / "cache"
        os.environ["FASTEMBED_CACHE_PATH"] = str(cache)
        sys.path.insert(0, str(SOURCE))
        from dridh.code_reacher import embeddings

        folder = Path(embeddings.model_files())  # downloads the pinned revision into the empty cache
        offline = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "HF_ENDPOINT": "http://127.0.0.1:9",
                   "HTTPS_PROXY": "http://127.0.0.1:9", "FASTEMBED_CACHE_PATH": str(cache)}
        subprocess.run([sys.executable, "-c", OFFLINE_CHECK, str(SOURCE)], env=offline, check=True)  # noqa: S603

        manifest = {
            "model": embeddings.FASTEMBED_MODEL,
            "revision": embeddings.FASTEMBED_REVISION,
            "folder": folder.relative_to(cache).as_posix(),
            "licence": "mit",
            "fastembed": fastembed.__version__,
        }
        # Hugging Face's own download bookkeeping (.cache/) is not part of the model.
        files = sorted(p for p in folder.rglob("*") if p.is_file() and ".cache" not in p.relative_to(folder).parts)
        archive = out / ARCHIVE
        with archive.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            _add(tar, MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
            _add(tar, "dridh-embedding-model-LICENSE.txt", LICENCE_NOTICE.encode())
            for path in files:
                info = tar.gettarinfo(str(path), arcname=path.relative_to(cache).as_posix())
                info.mtime, info.uid, info.gid, info.uname, info.gname, info.mode = 0, 0, 0, "", "", 0o644
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / f"{ARCHIVE}.sha256").write_text(f"{digest}  {ARCHIVE}\n")
    print(f"{ARCHIVE}: {archive.stat().st_size / 1e6:.1f} MB, sha256 {digest}")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
