from __future__ import annotations

import json
import sys
import base64

import pytest

from metax_docs_mcp import github_gh, sources
from test_sources import MemoryStore
from test_github_archive import archive_bytes, blob_sha
import tarfile


def fake_process(monkeypatch, program):
    original = github_gh.subprocess.Popen
    calls = []
    def launch(command, **kwargs):
        calls.append(command)
        return original([sys.executable, '-c', program], **kwargs)
    monkeypatch.setattr(github_gh.subprocess, 'Popen', launch)
    return calls


def test_gh_reads_binary_without_exporting_credentials(monkeypatch):
    calls = fake_process(monkeypatch, "import sys; sys.stdout.buffer.write(b'\\x1f\\x8bdata')")
    data, truncated = github_gh.api('repos/MetaX-MACA/example/tarball/'+'a'*40, max_bytes=100)
    assert data == b'\x1f\x8bdata' and not truncated
    assert calls[0][:4] == ['gh', 'api', '--method', 'GET']
    assert len(calls[0]) == 5


def test_gh_stops_at_response_bound(monkeypatch):
    fake_process(monkeypatch, "import sys; sys.stdout.buffer.write(b'x'*10000)")
    assert github_gh.api('repos/MetaX-MACA/example', max_bytes=32) == (b'x'*32, True)


def test_gh_auth_failure_is_explicit_and_stderr_is_not_exposed(monkeypatch):
    fake_process(monkeypatch, "import sys; sys.stderr.write('credential-secret HTTP 401'); sys.exit(1)")
    with pytest.raises(RuntimeError, match='HTTP 401') as error:
        github_gh.api('repos/MetaX-MACA/example', max_bytes=32)
    assert 'credential-secret' not in str(error.value)


def test_gh_timeout_terminates_its_child(monkeypatch):
    fake_process(monkeypatch, 'import time; time.sleep(5)')
    with pytest.raises(RuntimeError, match='timed out'):
        github_gh.api('repos/MetaX-MACA/example', max_bytes=32, timeout=0.05)


def test_gh_archive_route_and_host_boundary():
    assert github_gh.endpoint_for_url('https://codeload.github.com/MetaX-MACA/example/tar.gz/'+'a'*40) == 'repos/MetaX-MACA/example/tarball/'+'a'*40
    for url in ['https://evil.test/repos/example', 'http://api.github.com/repos/example', 'https://codeload.github.com/o/r/tar.gz/main']:
        with pytest.raises(ValueError):
            github_gh.endpoint_for_url(url)


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('transport', ['archive', 'api'])
@pytest.mark.parametrize('extension', ['py', 'pyx', 'f90', 'proto'])
def test_gh_ingestion_does_not_create_http_client_and_tracks_repo_status(monkeypatch, empty, transport, extension):
    revision = 'a'*40
    raw = b'def public_api(): pass\n'
    path = 'api.' + extension
    root = 'repos/MetaX-MACA/example'
    routes = {
        root: json.dumps({'default_branch': 'main', 'private': False, 'size': 0 if empty else 1}).encode(),
        root+'/commits/main': json.dumps({'sha': revision}).encode(),
        root+f'/git/trees/{revision}?recursive=1': json.dumps({'tree': [{'type': 'blob', 'path': path, 'sha': blob_sha(raw), 'size': len(raw)}]}).encode(),
        root+f'/tarball/{revision}': archive_bytes([('root/'+path, raw, tarfile.REGTYPE)]),
        root+'/git/blobs/'+blob_sha(raw): json.dumps({'content':base64.b64encode(raw).decode(),'encoding':'base64'}).encode(),
    }
    calls = []
    def api(endpoint, **kwargs):
        calls.append(endpoint)
        if empty and '/commits/' in endpoint:
            raise RuntimeError('gh api failed (HTTP 409)')
        return routes[endpoint], False
    monkeypatch.setattr(github_gh, 'api', api)
    def no_http(*args):
        raise AssertionError('gh must not use the HTTP client')
    monkeypatch.setattr(sources, '_new_client', no_http)
    store = MemoryStore()
    result = sources.sync_sources(store, {'github': {
        'repositories': ['example'], 'client': 'gh', 'transport': 'archive',
        'repository_transports': {'example': transport},
        'include_code': True, 'index_repository_metadata': True,
    }}, source='github')
    assert not result['failed'] and not result['partial']
    metadata = next(d for d in store.documents.values() if d.metadata['content_kind'] == 'repository_metadata')
    assert metadata.metadata['repository_status'] == ('empty' if empty else 'available')
    assert metadata.metadata['indexed_files'] == (0 if empty else 1)
    assert len(store.documents) == (1 if empty else 2)
    if not empty:
        assert (root+f'/tarball/{revision}' in calls) == (transport == 'archive')


def test_late_archive_failure_rolls_back_file_counts_and_store(monkeypatch, tmp_path):
    from metax_docs_mcp.store import Store
    revision = 'a'*40
    raw = b'def public_api(): pass\n'
    root = 'repos/MetaX-MACA/example'
    routes = {
        root: json.dumps({'default_branch':'main','private':False,'size':1}).encode(),
        root+'/commits/main': json.dumps({'sha':revision}).encode(),
        root+f'/git/trees/{revision}?recursive=1': json.dumps({'tree':[
            {'type':'blob','path':p,'sha':blob_sha(raw),'size':len(raw)}
            for p in ['api.py','missing.py']]}).encode(),
        root+f'/tarball/{revision}': archive_bytes([('root/api.py',raw,tarfile.REGTYPE)]),
    }
    monkeypatch.setattr(github_gh,'api',lambda endpoint,**kwargs:(routes[endpoint],False))
    store = Store(tmp_path/'index.sqlite3')
    try:
        result = sources.sync_sources(store, {'github':{
            'repositories':['example'],'client':'gh','transport':'archive','include_code':True,
        }}, source='github')
        assert result['failed'] == 1
        assert result['indexed'] == 0
        assert store.search('public_api') == []
    finally:
        store.close()


def test_gh_archive_timeout_is_independent_and_failure_names_the_archive(monkeypatch):
    revision = "a" * 40
    repo = "repos/MetaX-MACA/example"
    routes = {
        repo: {"default_branch": "main", "private": False},
        f"{repo}/commits/main": {"sha": revision},
        f"{repo}/git/trees/{revision}?recursive=1": {
            "tree": [{"type": "blob", "path": "example.py", "sha": "b" * 40, "size": 8}]
        },
    }
    deadlines = {}

    def api(endpoint, *, max_bytes, timeout):
        deadlines[endpoint] = timeout
        if endpoint not in routes:
            raise RuntimeError("gh api request timed out")
        return json.dumps(routes[endpoint]).encode(), False

    monkeypatch.setenv("METAX_GH_ARCHIVE_TIMEOUT", "240")
    monkeypatch.setattr(github_gh, "api", api)
    result = sources.sync_sources(MemoryStore(), {
        "github": {"repositories": ["example"], "client": "gh", "transport": "archive", "include_code": True},
        "http": {"timeout": 120, "retries": 0},
    }, source="github")

    assert deadlines == {**dict.fromkeys(routes, 120), f"{repo}/tarball/{revision}": 240}
    assert result["failed"] == 1
    assert f"codeload.github.com/MetaX-MACA/example/tar.gz/{revision}" in result["errors"][0]["error"]


def test_parallel_gh_blob_reads_keep_all_successful_files_on_one_failure(monkeypatch):
    import threading
    import time
    revision='a'*40
    root='repos/MetaX-MACA/example'
    files={f'api_{i}.py':f'def api_{i}(): return {i}\n'.encode() for i in range(7)}
    by_sha={blob_sha(raw):(path,raw) for path,raw in files.items()}
    active=maximum=0
    lock=threading.Lock()
    def api(endpoint,**kwargs):
        nonlocal active,maximum
        if endpoint==root:return json.dumps({'default_branch':'main','private':False}).encode(),False
        if endpoint.endswith('/commits/main'):return json.dumps({'sha':revision}).encode(),False
        if '/git/trees/' in endpoint:return json.dumps({'tree':[
            {'type':'blob','path':path,'sha':blob_sha(raw),'size':len(raw)} for path,raw in files.items()]}).encode(),False
        path,raw=by_sha[endpoint.rsplit('/',1)[1]]
        with lock:
            active+=1;maximum=max(maximum,active)
        try:
            time.sleep(.01)
            if path=='api_3.py':raise RuntimeError('gh api failed (HTTP 404)')
            return json.dumps({'encoding':'base64','content':base64.b64encode(raw).decode()}).encode(),False
        finally:
            with lock:active-=1
    monkeypatch.setattr(github_gh,'api',api)
    store=MemoryStore()
    result=sources.sync_sources(store,{'github':{'client':'gh','transport':'api',
        'repositories':['example'],'include_code':True},'http':{'rate_limit':0,'retries':0}},source='github')
    assert 1<maximum<=4
    assert result['failed']==1 and result['indexed']==6 and result['partial']
    assert {d.path:d.text for d in store.documents.values()}=={
        path:raw.decode() for path,raw in files.items() if path!='api_3.py'}


def test_full_organization_page_without_link_header_is_not_complete():
    class Fetcher:
        def get(self,url):
            assert 'per_page=100' in url
            return sources._FetchResult(url,200,{},json.dumps([
                {'name':f'repository-{i}'} for i in range(100)]).encode())
    stats=sources._new_stats('github')
    names=sources._discover_github_repositories('MetaX-MACA',Fetcher(),stats)
    assert len(names)==100
    assert stats['partial'] and stats['truncated']


@pytest.mark.parametrize('failure,recovered', [('gh api failed (exit 1)',True),('gh api failed (HTTP 401)',False)])
def test_gh_blob_network_fallback_preserves_content_and_auth_denials(monkeypatch,failure,recovered):
    raw=b'cdef int public_api = 1\n';sha=blob_sha(raw);revision='a'*40;root='repos/MetaX-MACA/example'
    def api(endpoint,**kwargs):
        if endpoint==root:return b'{"default_branch":"main","private":false}',False
        if '/commits/' in endpoint:return json.dumps({'sha':revision}).encode(),False
        if '/git/trees/' in endpoint:return json.dumps({'tree':[{'type':'blob','path':'api.pyx','sha':sha,'size':len(raw)}]}).encode(),False
        raise RuntimeError(failure)
    calls=[]
    def read(command,**kwargs):
        calls.append(command)
        return json.dumps({'data':{'repository':{'object':{'oid':sha,'byteSize':len(raw),'isBinary':False,'isTruncated':False,'text':raw.decode()}}}}).encode(),False
    monkeypatch.setattr(github_gh,'api',api);monkeypatch.setattr(github_gh,'_read',read)
    store=MemoryStore()
    report=sources.sync_sources(store,{'github':{'client':'gh','transport':'api','repositories':['example'],'include_code':True},'http':{'retries':0,'rate_limit':0}},source='github')
    assert bool(calls)==recovered
    assert report['failed']==(0 if recovered else 1)
    if recovered:
        assert calls[0][:4]==['gh','api','graphql','-f']
        assert next(iter(store.documents.values())).text==raw.decode()


@pytest.mark.parametrize('change', ['truncated','wrong_oid','changed_text'])
def test_gh_graphql_rejects_incomplete_or_changed_blob(monkeypatch,change):
    raw=b'public source\n';sha=blob_sha(raw)
    value={'oid':sha,'byteSize':len(raw),'isBinary':False,'isTruncated':False,'text':raw.decode()}
    if change=='truncated':value['isTruncated']=True
    if change=='wrong_oid':value['oid']='b'*40
    if change=='changed_text':value['text']='changed text!\n'
    monkeypatch.setattr(github_gh,'_read',lambda *args,**kwargs:(json.dumps({'data':{'repository':{'object':value}}}).encode(),False))
    with pytest.raises(RuntimeError):github_gh.blob('MetaX-MACA','example',sha,max_bytes=1000)


def test_gh_graphql_retries_transient_transport_failure(monkeypatch):
    raw=b'public source\n';sha=blob_sha(raw);calls=[]
    def read(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:raise RuntimeError('gh api failed (exit 1)')
        return json.dumps({'data':{'repository':{'object':{'oid':sha,'byteSize':len(raw),'isBinary':False,'isTruncated':False,'text':raw.decode()}}}}).encode(),False
    monkeypatch.setattr(github_gh,'_read',read)
    monkeypatch.setattr(github_gh.time,'sleep',lambda delay:None)
    assert base64.b64decode(github_gh.blob('MetaX-MACA','example',sha,max_bytes=1000)['content'])==raw
    assert len(calls)==2


@pytest.mark.parametrize('valid_original',[True,False])
def test_gh_archive_substituted_file_uses_verified_git_original(monkeypatch,valid_original):
    original=b'version = "$Format:%H$"\n';exported=b'version = "expanded-by-git-archive"\n'
    sha=blob_sha(original);revision='a'*40;root='repos/MetaX-MACA/example'
    routes={root:b'{"default_branch":"main","private":false}',
        root+'/commits/main':json.dumps({'sha':revision}).encode(),
        root+f'/git/trees/{revision}?recursive=1':json.dumps({'tree':[{'type':'blob','path':'_version.py','sha':sha,'size':len(original)}]}).encode(),
        root+f'/tarball/{revision}':archive_bytes([('root/_version.py',exported,tarfile.REGTYPE)]),
        root+'/git/blobs/'+sha:json.dumps({'encoding':'base64','content':base64.b64encode(original if valid_original else b'tampered').decode()}).encode()}
    monkeypatch.setattr(github_gh,'api',lambda endpoint,**kwargs:(routes[endpoint],False))
    store=MemoryStore()
    report=sources.sync_sources(store,{'github':{'repositories':['example'],'client':'gh','transport':'archive','include_code':True},'http':{'retries':0,'rate_limit':0}},source='github')
    assert report['failed']==(0 if valid_original else 1)
    assert len(store.documents)==int(valid_original)
    if valid_original:assert next(iter(store.documents.values())).text==original.decode()


def test_github_selection_excludes_link_pointers_and_keeps_regular_sources():
    tree={'tree':[
        {'type':'blob','mode':'100644','path':'README.md','sha':'a'*40},
        {'type':'blob','mode':'120000','path':'docs/README.md','sha':'b'*40},
        {'type':'blob','mode':'100755','path':'example.py','sha':'c'*40},
        {'type':'commit','mode':'160000','path':'vendor','sha':'d'*40}]}
    files,truncated=sources._github_files(tree,include_code=True,max_files=10)
    assert {item.path for item in files}=={'README.md','example.py'}
    assert not truncated
