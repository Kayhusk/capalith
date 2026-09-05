# Third-party notices

## PyYAML 6.0.3

Capalith uses PyYAML to parse YAML frontmatter. PyYAML 6.0.3 is distributed under the MIT License.

Source: https://github.com/yaml/pyyaml
Package: https://pypi.org/project/PyYAML/6.0.3/

```text
Copyright (c) 2017-2021 Ingy döt Net
Copyright (c) 2006-2016 Kirill Simonov

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Local semantic retrieval

Capalith uses `fastembed` 0.8.0 under Apache-2.0 with the `Qdrant/bge-small-en-v1.5-onnx-Q` model artifact at revision `52398278842ec682c6f32300af41344b1c0b0bb2`, also treated as Apache-2.0. The logical model is `BAAI/bge-small-en-v1.5`.

Sources:

- https://github.com/qdrant/fastembed
- https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q
- https://huggingface.co/BAAI/bge-small-en-v1.5

The source repository's `requirements.txt` and `DEPENDENCIES.json` record the exact 28-wheel semantic closure and selected artifact hashes. Installed wheels retain their bundled license and notice files. The resolved license inventory is:

- Apache-2.0: fastembed 0.8.0, flatbuffers 25.12.19, hf-xet 1.6.0, huggingface-hub 1.29.0, requests 2.34.2, tokenizers 0.23.1.
- MIT: anyio 4.14.2, charset-normalizer 3.5.1, filelock 3.32.5, h11 0.16.0, loguru 0.7.3, mmh3 5.3.0, onnxruntime 1.29.0, py-rust-stemmers 0.1.8, PyYAML 6.0.3, urllib3 2.7.0.
- BSD-3-Clause: click 8.5.0, fsspec 2026.7.0, httpcore 1.0.9, httpx 0.28.1, idna 3.19, protobuf 7.36.1.
- MPL-2.0: certifi 2026.7.22.
- MPL-2.0 AND MIT: tqdm 4.70.0.
- Apache-2.0 OR BSD-2-Clause: packaging 26.3.
- BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0: numpy 2.4.6.
- MIT-CMU: Pillow 12.3.0.
- PSF-2.0: typing-extensions 4.16.0.

## Read-only MCP server

Capalith uses the Model Context Protocol Python SDK 2.1.1 under the MIT License. The server imports the SDK directly and does not install the optional `cli` extra.

Sources:

- https://github.com/modelcontextprotocol/python-sdk
- https://pypi.org/project/mcp/2.1.1/

The source repository's `requirements.txt` and `DEPENDENCIES.json` record the exact 28-wheel MCP closure and selected artifact hashes. Five wheels are shared with the semantic retrieval closure: `anyio`, `click`, `h11`, `idna`, and `typing-extensions`. The 23 additional distributions retain their bundled license and notice files:

- Apache-2.0: opentelemetry-api 1.44.0, python-multipart 0.0.32.
- Apache-2.0 OR BSD-3-Clause: cryptography 50.0.1.
- BSD-3-Clause: httpcore2 2.12.0, httpx2 2.12.0, pycparser 3.0, sse-starlette 3.4.10, starlette 1.6.0, uvicorn 0.52.4.
- MIT: annotated-types 0.8.0, attrs 26.1.0, jsonschema 4.26.0, jsonschema-specifications 2025.9.1, mcp 2.1.1, mcp-types 2.1.1, pydantic 2.13.5, pydantic_core 2.46.5, PyJWT 2.13.0, referencing 0.37.0, rpds-py 2026.6.3, truststore 0.10.4, typing-inspection 0.4.4.
- MIT-0: cffi 2.1.1.
