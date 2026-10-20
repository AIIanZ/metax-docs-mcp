"""Exercise the installed stdio MCP against a synchronized expanded index."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROBES = [
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


def payload(result):
    assert not result.isError, result
    data = result.structuredContent or json.loads(result.content[0].text)
    return data.get('result', data) if isinstance(data, dict) else data


async def verify(db: Path):
    report = {'database': str(db), 'queries': []}
    params = StdioServerParameters(command=str(Path(sys.executable).with_name('metax-docs-mcp')), args=['--db', str(db), 'serve'])
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
                for hit in hits:
                    candidate = payload(await session.call_tool('get_metax_document', {'document_id': hit['id'], 'limit': 12000}))
                    if kind is None or candidate['metadata'].get('content_kind') == kind:
                        document = candidate
                        break
                assert document, f'No {kind} hit: {arguments}'
                assert document['source'] == source
                if repository:
                    assert document['repository'] == repository
                    assert len(document['revision']) == 40 and document['revision'] in document['url']
                if source == 'official':
                    assert document['sections']
                    section_key = document['sections'][0]['anchor']
                    section = payload(await session.call_tool('get_metax_section', {'document_id': document['id'], 'section': section_key}))
                    assert section and section['id'] == document['id']
                report['queries'].append({'query': query, 'source': source, 'repository': repository, 'id': document['id'], 'path': document['path'], 'url': document['url'], 'revision': document['revision'], 'version': document['version'], 'content_kind': document['metadata'].get('content_kind'), 'read_ok': True})
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
        report['coverage'] = [dict(zip(['source', 'repository', 'version', 'revision', 'kind', 'count'], row)) for row in conn.execute("SELECT source, repository, version, revision, json_extract(metadata_json, '$.content_kind'), count(*) FROM documents GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,5")]
        report['official_series'] = [dict(zip(['series', 'version', 'count'], row)) for row in conn.execute("SELECT json_extract(metadata_json, '$.series_name'), version, count(*) FROM documents WHERE source='official' GROUP BY 1,2 ORDER BY 1,2")]
    return report


if __name__ == '__main__':
    print(json.dumps(asyncio.run(verify(Path(sys.argv[1]).resolve())), ensure_ascii=False, indent=2))
