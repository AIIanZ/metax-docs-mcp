"""Exercise the installed stdio MCP against a synchronized expanded index."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROBES = [
    ('allgather_gemm', 'github', 'bytedance-seed/triton-distributed', 'code'),
    ('ConvertMETAXDistributedToLLVM', 'github', 'bytedance-seed/triton-distributed', 'code'),
    ('mxshmem_signal_wait_until', 'github', 'bytedance-seed/triton-distributed', 'code'),
    ('run_ring_put', 'github', 'bytedance-seed/triton-distributed', 'code'),
    ('mcclAllReduce', 'github', 'MetaX-MACA/mccl-nccl', 'code'),
    ('mcclBuildRings', 'github', 'MetaX-MACA/mccl-nccl', 'code'),
    ('AllReduce', 'github', 'MetaX-MACA/mccl_tests', 'code'),
    ('intranode', 'github', 'MetaX-MACA/MXDeepEP', 'code'),
    ('dispatch', 'github', 'MetaX-MACA/MXDeepEP', 'code'),
    ('mcblasGemmEx', 'official', None, None),
    ('mcblasLtMatmul', 'github', 'MetaX-MACA/Onnxruntime', 'code'),
    ('MacaPlatformBase', 'github', 'MetaX-MACA/vLLM-metax', 'code'),
    ('is_maca', 'github', 'MetaX-MACA/sglang', 'code'),
    ('mcMalloc', 'github', 'MetaX-MACA/maca-samples', 'code'),
    ('WarpSize', 'github', 'MetaX-MACA/gpuBenchmark', None),
    ('awq_gemm', 'github', 'MetaX-MACA/mcoplib', 'code'),
    ('mctlass', 'github', 'MetaX-MACA/mcTlass', 'code'),
    ('metax', 'github', 'MetaX-MACA/mcTriton', None),
    ('flash_attn', 'github', 'MetaX-MACA/flashattn', None),
    ('flashinfer', 'github', 'MetaX-MACA/McFlashInfer', None),
    ('tilelang', 'github', 'MetaX-MACA/op_optimization', None),
    ('maca', 'github', 'MetaX-MACA/TileOPs-Metax', None),
    ('maca', 'github', 'MetaX-MACA/TileKernels-Metax', None),
    ('maca', 'github', 'tile-ai/tilelang-metax', None),
    ('mcMalloc', 'official', None, None),
    ('共享内存', 'official', None, None),
    ('mxcc', 'official', None, None),
    ('MXMACA', 'docker', None, None),
]

# Pin acceptance to the requested backend, even when an upstream NVIDIA file
# scores higher for the same kernel name.
REQUIRED_PATHS = {
    ('allgather_gemm', 'bytedance-seed/triton-distributed'): 'python/triton_dist/kernels/metax/allgather_gemm.py',
    ('ConvertMETAXDistributedToLLVM', 'bytedance-seed/triton-distributed'): 'lib/Conversion/TritonDistributedToLLVM/METAX/ConvertMETAXDistributedToLLVM.cpp',
    ('mxshmem_signal_wait_until', 'bytedance-seed/triton-distributed'): 'python/triton_dist/language/extra/maca/libmxshmem_device.py',
    ('run_ring_put', 'bytedance-seed/triton-distributed'): 'shmem/mxshmem_bind/pymxshmem/example/run_ring_put.py',
    ('intranode', 'MetaX-MACA/MXDeepEP'): 'csrc/kernels/intranode.cu',
    ('dispatch', 'MetaX-MACA/MXDeepEP'): 'deep_ep/buffer.py',
    ('mcblasLtMatmul', 'MetaX-MACA/Onnxruntime'): 'cmake/external/ppl.nn/src/ppl/nn/engines/cuda/kernels/onnx/gemm_kernel.cc',
}


def payload(result):
    assert not result.isError, result
    data = result.structuredContent or json.loads(result.content[0].text)
    return data.get('result', data) if isinstance(data, dict) else data


async def verify(db: Path):
    report = {'database': str(db), 'queries': []}
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'examples/sources.json').read_text())
    github = config.get('github', {})
    expected_repositories = {
        (name if '/' in name else f"{github.get('organization', 'MetaX-MACA')}/{name}").lower()
        for name in github.get('repositories', [])
    }
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
        actual = {row[0].lower(): row[0] for row in conn.execute("SELECT DISTINCT repository FROM documents WHERE source='github'")}
        assert expected_repositories <= actual.keys(), f'Missing repositories: {expected_repositories - actual.keys()}'
        samples = []
        for repository in sorted(expected_repositories):
            row = conn.execute("""SELECT id,path,revision,json_extract(metadata_json,'$.content_kind'),
                json_extract(metadata_json,'$.repository_status') FROM documents WHERE repository=? AND length(text)>0
                ORDER BY CASE json_extract(metadata_json,'$.content_kind') WHEN 'code' THEN 0 WHEN 'documentation' THEN 1 ELSE 2 END,
                length(path),path LIMIT 1""", (actual[repository],)).fetchone()
            samples.append((actual[repository], row))
    source_root = str(Path(__file__).resolve().parents[1] / 'src')
    pythonpath = os.environ.get('PYTHONPATH', '')
    child_env = dict(os.environ)
    child_env['PYTHONPATH'] = os.pathsep.join(value for value in (source_root, pythonpath) if value)
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name('metax-docs-mcp')),
        args=['--db', str(db), 'serve'],
        env=child_env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            report['tools'] = [tool.name for tool in (await session.list_tools()).tools]
            report['sources'] = payload(await session.call_tool('list_metax_sources', {}))
            assert 'get_operator_guide' in report['tools'], report['tools']
            catalogue = payload(await session.call_tool('get_operator_guide', {}))
            report['operator_guide'] = []
            for entry in catalogue['topics']:
                guide = payload(await session.call_tool('get_operator_guide', {'topic': entry['slug']}))
                assert not guide['missing_references'], (entry['slug'], guide['missing_references'])
                assert guide['read_order'], entry['slug']
                report['operator_guide'].append({
                    'slug': entry['slug'],
                    'references': len(guide['references']),
                    'matches': sum(len(ref['matches']) for ref in guide['references']),
                    'read_order': len(guide['read_order']),
                })
            for query, source, repository, kind in PROBES:
                arguments = {'query': query, 'source': source, 'limit': 100}
                if repository:
                    arguments['repository'] = repository
                hits = payload(await session.call_tool('search_metax_docs', arguments))
                assert hits, f'No hits: {arguments}'
                document = None
                expected_path = REQUIRED_PATHS.get((query, repository))
                for hit in hits:
                    candidate = payload(await session.call_tool('get_metax_document', {'document_id': hit['id'], 'limit': 12000}))
                    if repository and candidate['metadata'].get('content_kind') == 'repository_metadata':
                        continue
                    if (kind is None or candidate['metadata'].get('content_kind') == kind) and (
                        expected_path is None or candidate['path'] == expected_path
                    ):
                        document = candidate
                        break
                assert document, f'No {kind} hit at {expected_path}: {arguments}'
                assert document['source'] == source
                if repository:
                    assert document['repository'] == repository
                    assert document['path'] and len(document['revision']) == 40 and document['revision'] in document['url'], (
                        arguments, document['path'], document['revision'], document['url']
                    )
                if source == 'official':
                    assert document['sections']
                    section_key = document['sections'][0]['anchor']
                    section = payload(await session.call_tool('get_metax_section', {'document_id': document['id'], 'section': section_key}))
                    assert section and section['id'] == document['id']
                report['queries'].append({'query': query, 'source': source, 'repository': repository, 'id': document['id'], 'path': document['path'], 'url': document['url'], 'revision': document['revision'], 'version': document['version'], 'content_kind': document['metadata'].get('content_kind'), 'read_ok': True})
            report['repository_checks'] = []
            for repository, (identifier, path, revision, kind, status) in samples:
                document = payload(await session.call_tool('get_metax_document', {'document_id': identifier, 'limit': 1000}))
                assert document and document['text'] and document['repository'] == repository
                if kind == 'repository_metadata':
                    assert status in {'empty', 'no_supported_files'}, (repository, status)
                    query = repository
                else:
                    assert len(revision) == 40 and revision in document['url']
                    query = document['title'] if kind == 'documentation' else path
                hits = payload(await session.call_tool('search_metax_docs', {'query': query, 'repository': repository, 'limit': 100}))
                assert any(hit['id'] == identifier for hit in hits), (repository, path)
                report['repository_checks'].append({'repository': repository, 'id': identifier, 'path': path,
                    'revision': revision, 'content_kind': kind, 'repository_status': status,
                    'query': query, 'search_ok': True, 'read_ok': True})
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
        report['coverage'] = [dict(zip(['source', 'repository', 'version', 'revision', 'kind', 'count'], row)) for row in conn.execute("SELECT source, repository, version, revision, json_extract(metadata_json, '$.content_kind'), count(*) FROM documents GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,5")]
        report['official_series'] = [dict(zip(['series', 'version', 'count'], row)) for row in conn.execute("SELECT json_extract(metadata_json, '$.series_name'), version, count(*) FROM documents WHERE source='official' GROUP BY 1,2 ORDER BY 1,2")]
    return report


if __name__ == '__main__':
    print(json.dumps(asyncio.run(verify(Path(sys.argv[1]).resolve())), ensure_ascii=False, indent=2))
