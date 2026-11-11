# Cairn execution logs — 2026-11-11

Full pytest run was interrupted by automatic approval review for an unexpected Microsoft telemetry connection. The partial log below is not a completed suite result.

Synthetic OCR recognition: CAIRN 12345; confidence 0.996; assertion passed. Engine initialization log follows.


## python-install.log

```text
Obtaining file:///workspace/scratch/08e486b68db3/cairn-public
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Checking if build backend supports build_editable: started
  Checking if build backend supports build_editable: finished with status 'done'
  Getting requirements to build editable: started
  Getting requirements to build editable: finished with status 'done'
  Preparing editable metadata (pyproject.toml): started
  Preparing editable metadata (pyproject.toml): finished with status 'done'
Collecting sqlalchemy<3,>=2.0 (from cairn==0.6.0a1)
  Downloading sqlalchemy-2.1.8-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (9.7 kB)
Collecting fsrs==6.3.2 (from cairn==0.6.0a1)
  Downloading fsrs-6.3.2-py3-none-any.whl.metadata (14 kB)
Collecting fastapi>=0.110 (from cairn==0.6.0a1)
  Downloading fastapi-0.162.4-py3-none-any.whl.metadata (27 kB)
Collecting jinja2>=3.1 (from cairn==0.6.0a1)
  Downloading jinja2-3.1.6-py3-none-any.whl.metadata (2.9 kB)
Collecting uvicorn>=0.29 (from cairn==0.6.0a1)
  Downloading uvicorn-0.54.0-py3-none-any.whl.metadata (6.6 kB)
Collecting mcp>=2.3.0 (from cairn==0.6.0a1)
  Downloading mcp-2.3.0-py3-none-any.whl.metadata (6.6 kB)
Collecting math-verify>=0.8 (from cairn==0.6.0a1)
  Downloading math_verify-0.9.0-py3-none-any.whl.metadata (1.6 kB)
Collecting rapidocr-onnxruntime<2,>=1.4 (from cairn==0.6.0a1)
  Downloading rapidocr_onnxruntime-1.4.4-py3-none-any.whl.metadata (1.3 kB)
Collecting typing-extensions (from fsrs==6.3.2->cairn==0.6.0a1)
  Downloading typing_extensions-4.16.0-py3-none-any.whl.metadata (3.3 kB)
Collecting pytest>=8 (from cairn==0.6.0a1)
  Downloading pytest-9.2.0-py3-none-any.whl.metadata (7.9 kB)
Collecting httpx>=0.27 (from cairn==0.6.0a1)
  Downloading httpx-0.28.1-py3-none-any.whl.metadata (7.1 kB)
Collecting starlette>=0.46.0 (from fastapi>=0.110->cairn==0.6.0a1)
  Downloading starlette-1.10.0-py3-none-any.whl.metadata (6.6 kB)
Collecting anyio>=4.1.0 (from fastapi>=0.110->cairn==0.6.0a1)
  Downloading anyio-4.16.0-py3-none-any.whl.metadata (4.7 kB)
Collecting pydantic>=2.9.0 (from fastapi>=0.110->cairn==0.6.0a1)
  Downloading pydantic-2.14.3-py3-none-any.whl.metadata (141 kB)
Collecting typing-inspection>=0.4.2 (from fastapi>=0.110->cairn==0.6.0a1)
  Downloading typing_inspection-0.4.5-py3-none-any.whl.metadata (2.6 kB)
Collecting annotated-doc>=0.0.2 (from fastapi>=0.110->cairn==0.6.0a1)
  Downloading annotated_doc-0.0.6-py3-none-any.whl.metadata (6.5 kB)
Collecting opentelemetry-api>=1.44.0 (from fastapi>=0.110->cairn==0.6.0a1)
  Downloading opentelemetry_api-1.45.2-py3-none-any.whl.metadata (1.4 kB)
Collecting certifi (from httpx>=0.27->cairn==0.6.0a1)
  Downloading certifi-2026.10.23-py3-none-any.whl.metadata (2.5 kB)
Collecting httpcore==1.* (from httpx>=0.27->cairn==0.6.0a1)
  Downloading httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
Collecting idna (from httpx>=0.27->cairn==0.6.0a1)
  Downloading idna-3.20-py3-none-any.whl.metadata (7.2 kB)
Collecting h11>=0.16 (from httpcore==1.*->httpx>=0.27->cairn==0.6.0a1)
  Downloading h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
Collecting MarkupSafe>=2.0 (from jinja2>=3.1->cairn==0.6.0a1)
  Downloading markupsafe-3.0.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.7 kB)
Collecting latex2sympy2_extended==1.11.0 (from math-verify>=0.8->cairn==0.6.0a1)
  Downloading latex2sympy2_extended-1.11.0-py3-none-any.whl.metadata (5.3 kB)
Collecting sympy (from latex2sympy2_extended==1.11.0->math-verify>=0.8->cairn==0.6.0a1)
  Downloading sympy-1.14.0-py3-none-any.whl.metadata (12 kB)
Collecting antlr4-python3-runtime<=4.13.2,>=4.9.3 (from latex2sympy2_extended==1.11.0->math-verify>=0.8->cairn==0.6.0a1)
  Downloading antlr4_python3_runtime-4.13.2-py3-none-any.whl.metadata (304 bytes)
Collecting httpx2>=2.10.0 (from mcp>=2.3.0->cairn==0.6.0a1)
  Downloading httpx2-2.16.0-py3-none-any.whl.metadata (10 kB)
Collecting jsonschema>=4.20.0 (from mcp>=2.3.0->cairn==0.6.0a1)
  Downloading jsonschema-4.26.0-py3-none-any.whl.metadata (7.6 kB)
Collecting mcp-types==2.3.0 (from mcp>=2.3.0->cairn==0.6.0a1)
  Downloading mcp_types-2.3.0-py3-none-any.whl.metadata (1.9 kB)
Collecting pyjwt>=2.10.1 (from pyjwt[crypto]>=2.10.1->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading pyjwt-2.15.1-py3-none-any.whl.metadata (3.5 kB)
Collecting python-multipart>=0.0.9 (from mcp>=2.3.0->cairn==0.6.0a1)
  Downloading python_multipart-0.0.33-py3-none-any.whl.metadata (6.3 kB)
Collecting sse-starlette>=3.0.0 (from mcp>=2.3.0->cairn==0.6.0a1)
  Downloading sse_starlette-3.7.0-py3-none-any.whl.metadata (15 kB)
Collecting iniconfig>=2 (from pytest>=8->cairn==0.6.0a1)
  Downloading iniconfig-2.3.1-py3-none-any.whl.metadata (2.8 kB)
Collecting packaging>=24 (from pytest>=8->cairn==0.6.0a1)
  Downloading packaging-26.3-py3-none-any.whl.metadata (3.5 kB)
Collecting pluggy<2,>=1.6 (from pytest>=8->cairn==0.6.0a1)
  Downloading pluggy-1.7.0-py3-none-any.whl.metadata (5.0 kB)
Collecting pygments>=2.15 (from pytest>=8->cairn==0.6.0a1)
  Downloading pygments-2.21.0-py3-none-any.whl.metadata (2.5 kB)
Collecting pyclipper>=1.2.0 (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading pyclipper-1.4.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (8.6 kB)
Collecting opencv-python>=4.5.1.48 (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading opencv_python-5.0.0.93-cp37-abi3-manylinux_2_28_x86_64.whl.metadata (19 kB)
Collecting numpy<3.0.0,>=1.19.5 (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading numpy-2.5.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl.metadata (6.6 kB)
Collecting six>=1.15.0 (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading six-1.17.0-py2.py3-none-any.whl.metadata (1.7 kB)
Collecting Shapely!=2.0.4,>=1.7.1 (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading shapely-2.2.0-cp312-cp312-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl.metadata (6.7 kB)
Collecting PyYAML (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting Pillow (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading pillow-13.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl.metadata (9.1 kB)
Collecting onnxruntime>=1.7.0 (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading onnxruntime-1.31.1-cp312-cp312-manylinux_2_28_x86_64.whl.metadata (5.8 kB)
Collecting tqdm (from rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading tqdm-4.70.1-py3-none-any.whl.metadata (57 kB)
Collecting click>=7.0 (from uvicorn>=0.29->cairn==0.6.0a1)
  Downloading click-8.5.1-py3-none-any.whl.metadata (2.6 kB)
Collecting httpcore2==2.16.0 (from httpx2>=2.10.0->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading httpcore2-2.16.0-py3-none-any.whl.metadata (27 kB)
Collecting truststore>=0.10 (from httpx2>=2.10.0->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading truststore-0.10.5-py3-none-any.whl.metadata (4.5 kB)
Collecting attrs>=22.2.0 (from jsonschema>=4.20.0->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading attrs-26.1.0-py3-none-any.whl.metadata (8.8 kB)
Collecting jsonschema-specifications>=2023.03.6 (from jsonschema>=4.20.0->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading jsonschema_specifications-2025.9.1-py3-none-any.whl.metadata (2.9 kB)
Collecting referencing>=0.28.4 (from jsonschema>=4.20.0->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading referencing-0.37.0-py3-none-any.whl.metadata (2.8 kB)
Collecting rpds-py>=0.25.0 (from jsonschema>=4.20.0->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading rpds_py-2026.9.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (4.1 kB)
Collecting flatbuffers (from onnxruntime>=1.7.0->rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading flatbuffers-25.12.19-py2.py3-none-any.whl.metadata (1.0 kB)
Collecting protobuf>=4.25.8 (from onnxruntime>=1.7.0->rapidocr-onnxruntime<2,>=1.4->cairn==0.6.0a1)
  Downloading protobuf-7.36.2-cp310-abi3-manylinux2014_x86_64.whl.metadata (595 bytes)
Collecting annotated-types>=0.6.0 (from pydantic>=2.9.0->fastapi>=0.110->cairn==0.6.0a1)
  Downloading annotated_types-0.8.0-py3-none-any.whl.metadata (15 kB)
Collecting pydantic-core==2.50.4 (from pydantic>=2.9.0->fastapi>=0.110->cairn==0.6.0a1)
  Downloading pydantic_core-2.50.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (6.6 kB)
Collecting cryptography>=3.4.0 (from pyjwt[crypto]>=2.10.1->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading cryptography-50.0.2-cp311-abi3-manylinux_2_34_x86_64.whl.metadata (4.4 kB)
Collecting cffi>=2.0.0 (from cryptography>=3.4.0->pyjwt[crypto]>=2.10.1->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading cffi-2.1.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (2.5 kB)
Collecting mpmath<1.4,>=1.1.0 (from sympy->latex2sympy2_extended==1.11.0->math-verify>=0.8->cairn==0.6.0a1)
  Downloading mpmath-1.3.0-py3-none-any.whl.metadata (8.6 kB)
Collecting pycparser (from cffi>=2.0.0->cryptography>=3.4.0->pyjwt[crypto]>=2.10.1->mcp>=2.3.0->cairn==0.6.0a1)
  Downloading pycparser-3.12-py3-none-any.whl.metadata (9.7 kB)
Downloading fsrs-6.3.2-py3-none-any.whl (22 kB)
Downloading fastapi-0.162.4-py3-none-any.whl (151 kB)
Downloading httpx-0.28.1-py3-none-any.whl (73 kB)
Downloading httpcore-1.0.9-py3-none-any.whl (78 kB)
Downloading jinja2-3.1.6-py3-none-any.whl (134 kB)
Downloading math_verify-0.9.0-py3-none-any.whl (29 kB)
Downloading latex2sympy2_extended-1.11.0-py3-none-any.whl (209 kB)
Downloading mcp-2.3.0-py3-none-any.whl (368 kB)
Downloading mcp_types-2.3.0-py3-none-any.whl (69 kB)
Downloading pytest-9.2.0-py3-none-any.whl (411 kB)
Downloading rapidocr_onnxruntime-1.4.4-py3-none-any.whl (14.9 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 14.9/14.9 MB 48.8 MB/s eta 0:00:00
Downloading sqlalchemy-2.1.8-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (4.7 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 4.7/4.7 MB 35.9 MB/s eta 0:00:00
Downloading uvicorn-0.54.0-py3-none-any.whl (87 kB)
Downloading annotated_doc-0.0.6-py3-none-any.whl (5.3 kB)
Downloading anyio-4.16.0-py3-none-any.whl (134 kB)
Downloading click-8.5.1-py3-none-any.whl (126 kB)
Downloading h11-0.16.0-py3-none-any.whl (37 kB)
Downloading httpx2-2.16.0-py3-none-any.whl (97 kB)
Downloading httpcore2-2.16.0-py3-none-any.whl (85 kB)
Downloading idna-3.20-py3-none-any.whl (69 kB)
Downloading iniconfig-2.3.1-py3-none-any.whl (7.6 kB)
Downloading jsonschema-4.26.0-py3-none-any.whl (90 kB)
Downloading markupsafe-3.0.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (22 kB)
Downloading numpy-2.5.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl (16.7 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 16.7/16.7 MB 47.3 MB/s eta 0:00:00
Downloading onnxruntime-1.31.1-cp312-cp312-manylinux_2_28_x86_64.whl (23.8 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 23.8/23.8 MB 54.2 MB/s eta 0:00:00
Downloading opencv_python-5.0.0.93-cp37-abi3-manylinux_2_28_x86_64.whl (73.8 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 73.8/73.8 MB 53.8 MB/s eta 0:00:00
Downloading opentelemetry_api-1.45.2-py3-none-any.whl (60 kB)
Downloading packaging-26.3-py3-none-any.whl (129 kB)
Downloading pluggy-1.7.0-py3-none-any.whl (27 kB)
Downloading pyclipper-1.4.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (978 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 978.2/978.2 kB 12.9 MB/s eta 0:00:00
Downloading pydantic-2.14.3-py3-none-any.whl (494 kB)
Downloading pydantic_core-2.50.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (2.3 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 2.3/2.3 MB 27.2 MB/s eta 0:00:00
Downloading pygments-2.21.0-py3-none-any.whl (1.3 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1.3/1.3 MB 17.2 MB/s eta 0:00:00
Downloading pyjwt-2.15.1-py3-none-any.whl (33 kB)
Downloading python_multipart-0.0.33-py3-none-any.whl (32 kB)
Downloading shapely-2.2.0-cp312-cp312-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl (2.3 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 2.3/2.3 MB 25.8 MB/s eta 0:00:00
Downloading six-1.17.0-py2.py3-none-any.whl (11 kB)
Downloading sse_starlette-3.7.0-py3-none-any.whl (18 kB)
Downloading starlette-1.10.0-py3-none-any.whl (80 kB)
Downloading typing_extensions-4.16.0-py3-none-any.whl (45 kB)
Downloading typing_inspection-0.4.5-py3-none-any.whl (14 kB)
Downloading certifi-2026.10.23-py3-none-any.whl (135 kB)
Downloading pillow-13.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl (7.1 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 7.1/7.1 MB 44.2 MB/s eta 0:00:00
Downloading pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (807 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 807.9/807.9 kB 11.4 MB/s eta 0:00:00
Downloading tqdm-4.70.1-py3-none-any.whl (80 kB)
Downloading annotated_types-0.8.0-py3-none-any.whl (13 kB)
Downloading antlr4_python3_runtime-4.13.2-py3-none-any.whl (144 kB)
Downloading attrs-26.1.0-py3-none-any.whl (67 kB)
Downloading cryptography-50.0.2-cp311-abi3-manylinux_2_34_x86_64.whl (4.8 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 4.8/4.8 MB 38.2 MB/s eta 0:00:00
Downloading jsonschema_specifications-2025.9.1-py3-none-any.whl (18 kB)
Downloading protobuf-7.36.2-cp310-abi3-manylinux2014_x86_64.whl (343 kB)
Downloading referencing-0.37.0-py3-none-any.whl (26 kB)
Downloading rpds_py-2026.9.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (371 kB)
Downloading truststore-0.10.5-py3-none-any.whl (19 kB)
Downloading flatbuffers-25.12.19-py2.py3-none-any.whl (26 kB)
Downloading sympy-1.14.0-py3-none-any.whl (6.3 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 6.3/6.3 MB 43.7 MB/s eta 0:00:00
Downloading cffi-2.1.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (221 kB)
Downloading mpmath-1.3.0-py3-none-any.whl (536 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 536.2/536.2 kB 6.0 MB/s eta 0:00:00
Downloading pycparser-3.12-py3-none-any.whl (62 kB)
Building wheels for collected packages: cairn
  Building editable for cairn (pyproject.toml): started
  Building editable for cairn (pyproject.toml): finished with status 'done'
  Created wheel for cairn: filename=cairn-0.6.0a1-0.editable-py3-none-any.whl size=8686 sha256=5be7621d562800bb509d426e214181c69bd8b4ab977aea7c70ef1a22c39e0158
  Stored in directory: /tmp/pip-ephem-wheel-cache-gbaux1pq/wheels/cb/04/fa/e5ea0ee32b41b709b029e895d116ab6901c97916ba99a559c5
Successfully built cairn
Installing collected packages: mpmath, flatbuffers, antlr4-python3-runtime, typing-extensions, truststore, tqdm, sympy, six, rpds-py, PyYAML, python-multipart, pyjwt, pygments, pycparser, pyclipper, protobuf, pluggy, Pillow, packaging, numpy, MarkupSafe, iniconfig, idna, h11, click, certifi, attrs, annotated-types, annotated-doc, uvicorn, typing-inspection, sqlalchemy, Shapely, referencing, pytest, pydantic-core, opentelemetry-api, opencv-python, onnxruntime, latex2sympy2_extended, jinja2, httpcore2, httpcore, fsrs, cffi, anyio, starlette, rapidocr-onnxruntime, pydantic, math-verify, jsonschema-specifications, httpx2, httpx, cryptography, sse-starlette, mcp-types, jsonschema, fastapi, mcp, cairn
Successfully installed MarkupSafe-3.0.4 Pillow-13.0.0 PyYAML-6.0.3 Shapely-2.2.0 annotated-doc-0.0.6 annotated-types-0.8.0 antlr4-python3-runtime-4.13.2 anyio-4.16.0 attrs-26.1.0 cairn-0.6.0a1 certifi-2026.10.23 cffi-2.1.1 click-8.5.1 cryptography-50.0.2 fastapi-0.162.4 flatbuffers-25.12.19 fsrs-6.3.2 h11-0.16.0 httpcore-1.0.9 httpcore2-2.16.0 httpx-0.28.1 httpx2-2.16.0 idna-3.20 iniconfig-2.3.1 jinja2-3.1.6 jsonschema-4.26.0 jsonschema-specifications-2025.9.1 latex2sympy2_extended-1.11.0 math-verify-0.9.0 mcp-2.3.0 mcp-types-2.3.0 mpmath-1.3.0 numpy-2.5.4 onnxruntime-1.31.1 opencv-python-5.0.0.93 opentelemetry-api-1.45.2 packaging-26.3 pluggy-1.7.0 protobuf-7.36.2 pyclipper-1.4.0 pycparser-3.12 pydantic-2.14.3 pydantic-core-2.50.4 pygments-2.21.0 pyjwt-2.15.1 pytest-9.2.0 python-multipart-0.0.33 rapidocr-onnxruntime-1.4.4 referencing-0.37.0 rpds-py-2026.9.1 six-1.17.0 sqlalchemy-2.1.8 sse-starlette-3.7.0 starlette-1.10.0 sympy-1.14.0 tqdm-4.70.1 truststore-0.10.5 typing-extensions-4.16.0 typing-inspection-0.4.5 uvicorn-0.54.0

[notice] A new release of pip is available: 25.0.1 -> 26.3
[notice] To update, run: python -m pip install --upgrade pip

```


## dependency-check.log

```text
No broken requirements found.

```


## first-suite-failure.log

```text
......................................F
=================================== FAILURES ===================================
___________________ test_lazy_import_survives_missing_pyobjc ___________________

    def test_lazy_import_survives_missing_pyobjc():
        """把 AVFoundation 打成「导入即失败」，整个包仍能 import、只报告不可用。"""
        code = "\n".join(
            [
                "import sys",
                "sys.modules['AVFoundation'] = None",
                "sys.modules['CoreMedia'] = None",
                "sys.modules['Foundation'] = None",
                "import cairn.capture as c",
                "print('package', c.__name__, callable(c.get_backend))",
                "print('available', *c.avfoundation.is_available())",
            ]
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert "package cairn.capture True" in result.stdout
        assert "available False" in result.stdout
>       assert "pyobjc" in result.stdout
E       assert 'pyobjc' in 'package cairn.capture True\navailable False 非 macOS（linux），没有 AVFoundation\n'
E        +  where 'package cairn.capture True\navailable False 非 macOS（linux），没有 AVFoundation\n' = CompletedProcess(args=['/workspace/scratch/08e486b68db3/.venv/bin/python', '-c', "import sys\nsys.modules['AVFoundatio...e())"], returncode=0, stdout='package cairn.capture True\navailable False 非 macOS（linux），没有 AVFoundation\n', stderr='').stdout

tests/test_capture.py:53: AssertionError
=============================== warnings summary ===============================
tests/test_answer_backfill.py::test_two_keys_land_rating_and_schedule
tests/test_answer_backfill.py::test_two_keys_land_rating_and_schedule
  /workspace/scratch/08e486b68db3/.venv/lib/python3.12/site-packages/jinja2/runtime.py:303: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    return __obj(*args, **kwargs)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_capture.py::test_lazy_import_survives_missing_pyobjc - asse...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 38 passed, 2 warnings in 4.41s

```


## crop-failure.log

```text
F
=================================== FAILURES ===================================
________________ test_adjusted_corners_recrop_from_the_original ________________

client = <starlette.testclient.TestClient object at 0x7fe513517aa0>
db_path = PosixPath('/tmp/pytest-of-agent/pytest-3/test_adjusted_corners_recrop_f0/web.db')

    def test_adjusted_corners_recrop_from_the_original(client, db_path) -> None:
        """AC2：向内收 5% → 新图是 PNG、尺寸与自动裁的不同、逐像素等于"从原图重算"。"""
>       scan_id = upload(client)
                  ^^^^^^^^^^^^^^

tests/test_crop_apply.py:160: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
tests/test_crop_apply.py:83: in upload
    files={"file": (path.name, path.read_bytes(), "image/jpeg")},
                               ^^^^^^^^^^^^^^^^^
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib/python3.12/pathlib.py:1019: in read_bytes
    with self.open(mode='rb') as f:
         ^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = PosixPath('/workspace/scratch/08e486b68db3/cairn-public/docs/samples/hi-redbg.jpg')
mode = 'rb', buffering = -1, encoding = None, errors = None, newline = None

    def open(self, mode='r', buffering=-1, encoding=None,
             errors=None, newline=None):
        """
        Open the file pointed to by this path and return a file object, as
        the built-in open() function does.
        """
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
>       return io.open(self, mode, buffering, encoding, errors, newline)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       FileNotFoundError: [Errno 2] No such file or directory: '/workspace/scratch/08e486b68db3/cairn-public/docs/samples/hi-redbg.jpg'

/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib/python3.12/pathlib.py:1013: FileNotFoundError
---------------------------- Captured stdout setup -----------------------------
[cairn] practice batch config: size=20 green_ratio=0.2 red_ratio=0.8
[cairn] default review strategy: fsrs (env CAIRN_DEFAULT_STRATEGY)
---------------------------- Captured stderr setup -----------------------------
[cairn.capture] 警告：AVFoundation 不可用（非 macOS（linux），没有 AVFoundation），已回退到假设备
=========================== short test summary info ============================
FAILED tests/test_crop_apply.py::test_adjusted_corners_recrop_from_the_original
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.56s

```


## python-tests.log

```text
......................................F................................. [  4%]
....................FFFFFFFFFFFFFFFFFFFFFF.FFFFFFFssFF.FFFFFF..F.FFF.... [  9%]
..F.FFFF.............FFFFFFFF........................................... [ 13%]
..................................F.............................F....... [ 18%]
ssF.......................................................
```


## ocr-smoke.log

```text
RapidOCR

```


## api-smoke.log

```text
[cairn.capture] 警告：AVFoundation 不可用（非 macOS（linux），没有 AVFoundation），已回退到假设备
[cairn] practice batch config: size=20 green_ratio=0.2 red_ratio=0.8
[cairn] default review strategy: fsrs (env CAIRN_DEFAULT_STRATEGY)
0 / 200
0 /api/tools 200
0 /api/workflows/default 200
0 /stats 200
unauthenticated write 401
exact origin CORS 200 https://studio.example.invalid
[cairn] practice batch config: size=20 green_ratio=0.2 red_ratio=0.8
[cairn] default review strategy: fsrs (env CAIRN_DEFAULT_STRATEGY)
1 / 200
1 /api/tools 200
1 /api/workflows/default 200
1 /stats 200
unauthenticated write 401
exact origin CORS 200 https://studio.example.invalid
SQLite reopen 339968 bytes

```


## docker-validation.log

```text
env: ‘docker’: No such file or directory

```


## cairn-public/react-install.log

```text
npm warn Unknown env config "http-proxy". This will stop working in the next major version of npm.

added 92 packages in 7s
npm notice
npm notice New major version of npm available! 11.9.0 -> 12.2.2
npm notice Changelog: https://github.com/npm/cli/releases/tag/v12.2.2
npm notice To update run: npm install -g npm@12.2.2
npm notice

```


## cairn-public/react-build.log

```text
npm warn Unknown env config "http-proxy". This will stop working in the next major version of npm.

> cairn-workflow-canvas@0.1.0 build
> tsc -b && vite build

vite v6.4.5 building for production...
transforming...
✓ 1605 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.50 kB │ gzip:   0.33 kB
dist/assets/index-C4MPWBl5.css   36.89 kB │ gzip:   7.76 kB
dist/assets/index-DO_L8SRc.js   393.25 kB │ gzip: 126.52 kB
✓ built in 1.80s

```
