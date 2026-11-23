from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import pytest

from metax_docs_mcp import github_archive, sources
from test_sources import FakeClient, FakeResponse, MemoryStore, _patch_client


def archive_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, raw, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(raw) if kind == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(raw) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def test_archive_sync_uses_one_snapshot_and_preserves_code(monkeypatch):
    revision = 'a' * 40
    root = 'https://api.github.com/repos/MetaX-MACA/sglang'
    url = f'https://codeload.github.com/MetaX-MACA/sglang/tar.gz/{revision}'
    files = {'README.md': b'# SGLang MACA\n', 'python/sglang/maca.py': b'# comment\n\ndef is_maca():\n    return True\n\n'}
    routes = {
        root: FakeResponse(content=json.dumps({'default_branch': 'main', 'private': False}).encode()),
        root + '/commits/main': FakeResponse(content=json.dumps({'sha': revision}).encode()),
        root + f'/git/trees/{revision}?recursive=1': FakeResponse(content=json.dumps({'tree': [
            {'path': path, 'type': 'blob', 'sha': blob_sha(raw), 'size': len(raw)} for path, raw in files.items()
        ]}).encode()),
        url: FakeResponse(content=archive_bytes([(f'sglang-{revision}/{path}', raw, tarfile.REGTYPE) for path, raw in files.items()])),
    }
    client = FakeClient(routes)
    headers = []
    def new_client(timeout, values):
        headers.append(values)
        return client
    monkeypatch.setattr(sources, '_new_client', new_client)
    monkeypatch.setenv('GITHUB_TOKEN', 'test-secret')
    store = MemoryStore()
    config = {'github': {'repositories': ['sglang'], 'include_code': True, 'transport': 'archive'}, 'http': {'retries': 0, 'rate_limit': 0}}
    report = sources.sync_sources(store, config, source='github')
    assert report['indexed'] == 2 and not report['partial']
    assert len(client.calls) == 4
    assert headers[0]['Authorization'] == 'Bearer test-secret'
    assert 'Authorization' not in headers[1]
    code = next(doc for doc in store.documents.values() if doc.path.endswith('.py'))
    assert code.text == files[code.path].decode()
    assert code.title == code.path and code.metadata['content_kind'] == 'code'
    assert revision in code.url
    again = sources.sync_sources(store, config, source='github')
    assert again['unchanged'] == 2 and again['indexed'] == 0
    routes[url] = FakeResponse(content=archive_bytes([(f'root/{path}', b'tampered', tarfile.REGTYPE) for path in files]))
    bad = sources.sync_sources(store, config, source='github')
    assert bad['failed'] == 2 and bad['partial']
    assert code.text == next(doc for doc in store.documents.values() if doc.path.endswith('.py')).text


@pytest.mark.parametrize('entries,match', [
    ([('../bad', b'x', tarfile.REGTYPE)], 'unsafe'),
    ([('/bad', b'x', tarfile.REGTYPE)], 'unsafe'),
    ([('root/a.py', b'', tarfile.SYMTYPE)], 'missing'),
    ([('root/a.py', b'x', tarfile.REGTYPE), ('root/a.py', b'x', tarfile.REGTYPE)], 'duplicate'),
    ([('root/other.py', b'x', tarfile.REGTYPE)], 'missing'),
])
def test_archive_rejects_unsafe_or_missing_selected_files(entries, match):
    with pytest.raises(ValueError, match=match):
        list(github_archive.iter_archive_files(archive_bytes(entries), {'a.py'}))


def test_archive_expansion_bound(monkeypatch):
    monkeypatch.setattr(github_archive, 'MAX_EXPANDED_BYTES', 100)
    with pytest.raises(ValueError, match='expanded byte limit'):
        list(github_archive.iter_archive_files(archive_bytes([('root/a.py', b'x'*1000, tarfile.REGTYPE)]), {'a.py'}))


def test_archive_file_and_member_bounds(monkeypatch):
    content = archive_bytes([('root/a.py', b'xx', tarfile.REGTYPE)])
    monkeypatch.setattr(github_archive, 'MAX_FILE_BYTES', 1)
    with pytest.raises(ValueError, match='file byte limit'):
        list(github_archive.iter_archive_files(content, {'a.py'}))
    monkeypatch.setattr(github_archive, 'MAX_MEMBERS', 0)
    with pytest.raises(ValueError, match='member limit'):
        list(github_archive.iter_archive_files(content, {'a.py'}))


def test_excludes_apply_before_file_limit():
    tree = {'tree': [{'path': path, 'type': 'blob', 'sha': 'a'*40} for path in ['models/tokenizer.json', 'src/runtime.py']]}
    files, truncated = sources._github_files(tree, include_code=True, max_files=1, exclude_globs=['**/tokenizer.json'])
    assert [item.path for item in files] == ['src/runtime.py'] and not truncated


def test_cross_organization_allowlist(monkeypatch):
    client = FakeClient({})
    _patch_client(monkeypatch, client)
    report = sources.sync_sources(MemoryStore(), {'github': {'repositories': ['evil/sglang']}}, source='github')
    assert report['failed'] and not client.calls
    assert sources._normalise_repository('MetaX-MACA/sglang', 'MetaX-MACA') == 'sglang'


def test_archive_host_redirect_cannot_escape():
    url = 'https://codeload.github.com/MetaX-MACA/sglang/tar.gz/' + 'a'*40
    client = FakeClient({url: FakeResponse(302, headers={'location': 'https://evil.test/archive'})})
    fetcher = sources._HttpFetcher(client, allowed_hosts={'codeload.github.com'}, retries=0, max_response_bytes=1000, rate_limiter=sources._RateLimiter(0))
    with pytest.raises(RuntimeError, match='allowlist'):
        fetcher.get(url)
    assert client.calls == [url]


@pytest.mark.parametrize('repository', ['tile-ai/tilelang-metax', 'ByteDance-Seed/Triton-distributed'])
def test_explicit_repository_uses_allowlisted_external_owner(monkeypatch, repository):
    root = 'https://api.github.com/repos/' + repository.lower()
    revision = 'a'*40
    client = FakeClient({
        root: FakeResponse(content=json.dumps({'default_branch': 'dev', 'private': False}).encode()),
        root+'/commits/dev': FakeResponse(content=json.dumps({'sha': revision}).encode()),
        root+f'/git/trees/{revision}?recursive=1': FakeResponse(content=b'{"tree": []}'),
    })
    _patch_client(monkeypatch, client)
    report = sources.sync_sources(MemoryStore(), {'github': {'repositories': [repository]}, 'http': {'rate_limit': 0}}, source='github')
    assert report['failed'] == 0
    assert all('/repos/' + repository.lower() in call for call in client.calls)


def test_distributed_compiler_sources_and_build_files_are_selected():
    paths = ['README.md', 'include/DistributedOps.td', 'test/metax/wait.mlir', 'project.mk',
             'third-party/nvshmem.patch', 'python/api.pyi', 'assets/readme-banner.png', 'asset/plot.png']
    tree = {'tree': [{'path': path, 'type': 'blob', 'sha': 'a'*40} for path in paths]}
    files, truncated = sources._github_files(tree, include_code=True, max_files=100)
    assert {item.path for item in files} == set(paths[:-2])
    assert not truncated
    files, _ = sources._github_files(tree, include_code=False, max_files=100)
    assert {item.path for item in files} == {"README.md"}


@pytest.mark.parametrize('patterns,expected,failed', [
    (['src/*.cc'], {'src/gemm.cc'}, False),
    (['missing/*.cc'], set(), True),
    (None, {'src/gemm.cc', 'unrelated.py'}, False),
])
def test_repository_include_scope_is_enforced_before_download(monkeypatch, patterns, expected, failed):
    revision = 'a'*40
    root = 'https://api.github.com/repos/MetaX-MACA/Onnxruntime'
    raw = b'mcblasLtMatmul();\n'
    sha = blob_sha(raw)
    client = FakeClient({
        root: FakeResponse(content=b'{"default_branch":"main","private":false}'),
        root+'/commits/main': FakeResponse(content=json.dumps({'sha': revision}).encode()),
        root+f'/git/trees/{revision}?recursive=1': FakeResponse(content=json.dumps({'tree': [
            {'path': path, 'sha': sha, 'type': 'blob', 'size': len(raw)}
            for path in ['src/gemm.cc', 'unrelated.py']
        ]}).encode()),
        root+f'/git/blobs/{sha}': FakeResponse(content=json.dumps({
            'encoding': 'base64', 'content': base64.b64encode(raw).decode(),
        }).encode()),
    })
    _patch_client(monkeypatch, client)
    github = {'repositories': ['Onnxruntime'], 'include_code': True}
    if patterns is not None:
        github['repository_include_globs'] = {'MetaX-MACA/Onnxruntime': patterns}
    store = MemoryStore()
    report = sources.sync_sources(store, {'github': github, 'http': {'rate_limit': 0}}, source='github')
    assert {document.path for document in store.documents.values()} == expected
    assert bool(report['failed']) is failed
    if failed:
        assert report['partial']
        assert not any('/git/blobs/' in call for call in client.calls)


@pytest.mark.parametrize('patterns', [[], '', [''], [False]])
def test_invalid_repository_include_scope_does_not_fetch(monkeypatch, patterns):
    client = FakeClient({})
    _patch_client(monkeypatch, client)
    report = sources.sync_sources(MemoryStore(), {'github': {
        'repositories': ['Onnxruntime'], 'repository_include_globs': {'MetaX-MACA/Onnxruntime': patterns},
    }}, source='github')
    assert report['failed'] and not client.calls


def test_archive_compressed_bound_is_reported(monkeypatch):
    revision = 'a'*40
    root = 'https://api.github.com/repos/MetaX-MACA/sglang'
    url = f'https://codeload.github.com/MetaX-MACA/sglang/tar.gz/{revision}'
    client = FakeClient({
        root: FakeResponse(content=b'{"default_branch": "main", "private": false}'),
        root+'/commits/main': FakeResponse(content=json.dumps({'sha': revision}).encode()),
        root+f'/git/trees/{revision}?recursive=1': FakeResponse(content=json.dumps({'tree': [{'path': 'a.py', 'type': 'blob', 'sha': 'b'*40}]}).encode()),
        url: FakeResponse(content=b'x'*17000),
    })
    _patch_client(monkeypatch, client)
    report = sources.sync_sources(MemoryStore(), {'github': {'repositories': ['sglang'], 'transport': 'archive', 'include_code': True, 'max_archive_bytes': 16384}, 'http': {'rate_limit': 0, 'retries': 0}}, source='github')
    assert report['failed'] == 1 and report['truncated'] and report['partial']
    assert report['indexed'] == 0


def test_repository_ref_pins_release_branch_and_version(monkeypatch):
    root = 'https://api.github.com/repos/MetaX-MACA/sglang'
    revision = 'a'*40
    raw = b'def is_maca():\n    return True\n'
    client = FakeClient({
        root: FakeResponse(content=b'{"default_branch": "main", "private": false}'),
        root+'/commits/release%2Fv0.5.14': FakeResponse(content=json.dumps({'sha': revision}).encode()),
        root+f'/git/trees/{revision}?recursive=1': FakeResponse(content=json.dumps({'tree': [{'path': 'common.py', 'type': 'blob', 'sha': blob_sha(raw)}]}).encode()),
        f'https://codeload.github.com/MetaX-MACA/sglang/tar.gz/{revision}': FakeResponse(content=archive_bytes([('root/common.py', raw, tarfile.REGTYPE)])),
    })
    _patch_client(monkeypatch, client)
    store = MemoryStore()
    config = {'github': {'repositories': ['sglang'], 'repository_refs': {'MetaX-MACA/sglang': 'release/v0.5.14'}, 'transport': 'archive', 'include_code': True}, 'http': {'rate_limit': 0}}
    report = sources.sync_sources(store, config, source='github')
    assert report['indexed'] == 1 and not report['partial']
    doc = next(iter(store.documents.values()))
    assert doc.version == 'release/v0.5.14'
    assert doc.metadata['branch'] == doc.version and doc.revision == revision
    assert root+'/commits/main' not in client.calls


def test_invalid_repository_ref_is_reported_without_network(monkeypatch):
    client = FakeClient({})
    _patch_client(monkeypatch, client)
    report = sources.sync_sources(MemoryStore(), {'github': {'repositories': ['sglang'], 'repository_refs': {'sglang': 'bad ref'}}}, source='github')
    assert report['failed'] == 1 and not client.calls
