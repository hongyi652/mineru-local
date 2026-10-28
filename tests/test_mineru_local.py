from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import anyio
import pytest
import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
import server
import worker


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'JOBS', tmp_path / 'jobs')
    monkeypatch.setattr(server, 'PROCESSES', {})
    for key in worker.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MINERU_LOCAL_ENV_FILE', str(tmp_path / 'missing.env'))


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / '论文 test.pdf'
    path.write_bytes(b'%PDF-1.7\nmock offline input\n%%EOF')
    return path


@pytest.fixture
def backend():
    root, _ = worker.source_paths()
    return worker.load_backend(root)


def submit(pdf, **overrides):
    args = dict(input_path=str(pdf), confirm_upload=True, is_ocr=False, page_ranges='',
                enable_formula=True, enable_table=True, timeout_seconds=30)
    args.update(overrides)
    return server.start(**args)


class FakeProcess:
    pid = 123456789

    def __init__(self, *args, **kwargs):
        self.returncode = None
        self.args = args
        self.kwargs = kwargs

    def poll(self):
        return self.returncode


@pytest.fixture
def fake_spawn(monkeypatch):
    monkeypatch.setattr(server.subprocess, 'Popen', FakeProcess)
    monkeypatch.setattr(server, 'process_marker', lambda pid: 'created-once')


class Response:
    def __init__(self, payload=None, content=b''):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def bundle():
    image = io.BytesIO()
    Image.new('RGB', (4, 3), 'white').save(image, format='PNG')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('result/full.md', '# Offline paper\n\n$x^2$\n\n|a|b|\n|-|-|\n|1|2|\n\n![图](images/fig.png)\n')
        archive.writestr('result/images/fig.png', image.getvalue())
    return output.getvalue()


def test_generated_config_contains_only_token(tmp_path):
    config = install.build_config(Path('/tmp/python'), 'claude', '')
    server_config = config['mcpServers']['mineru-local']
    assert server_config['command'] == str(Path('/tmp/python').resolve())
    assert server_config['args'] == [str((ROOT / 'server.py').resolve())]
    assert server_config['env'] == {'MINERU_API_TOKEN': ''}
    assert 'MINERU_LOCAL_JOB_DIR' not in json.dumps(config)


def test_default_config_path_is_not_project_local():
    assert install.ROOT not in worker.default_config_path().parents


def test_requires_authorization_before_reading(monkeypatch):
    monkeypatch.setattr(server, 'file_hash', lambda path: pytest.fail('read without consent'))
    result = submit(Path('does-not-exist'), confirm_upload=False)
    assert result['error']['code'] == 'upload_not_confirmed'
    assert not server.JOBS.exists()


@pytest.mark.parametrize('path', ['relative.pdf', 'https://example.invalid/a.pdf',
                                 '//server/share/a.pdf', '\\\\server\\share\\a.pdf', ''])
def test_rejects_nonlocal_or_relative_paths(path):
    assert submit(Path('.'), input_path=path)['status'] == 'rejected'
    assert not server.JOBS.exists()


def test_rejects_not_pdf(pdf, tmp_path):
    assert submit(tmp_path)['status'] == 'rejected'
    assert submit(tmp_path / 'missing.pdf')['status'] == 'rejected'
    text = tmp_path / 'fake.pdf'
    text.write_text('not a PDF')
    assert submit(text)['status'] == 'rejected'
    renamed = tmp_path / 'real.txt'
    renamed.write_bytes(pdf.read_bytes())
    assert submit(renamed)['status'] == 'rejected'


@pytest.mark.parametrize('ranges', ['0', '2-1', '1--2', '-1', '1,', '1, 2', '1;2', '01', '1-0'])
def test_invalid_page_ranges(pdf, ranges):
    assert submit(pdf, page_ranges=ranges)['status'] == 'rejected'


@pytest.mark.parametrize('timeout', [0, 29, 3601, True, 30.5])
def test_invalid_timeout(pdf, timeout):
    assert submit(pdf, timeout_seconds=timeout)['status'] == 'rejected'


@pytest.mark.parametrize('ranges', ['', '1', '1-5,8', ' 1-2 '])
def test_start_is_nonblocking_and_isolated(pdf, fake_spawn, ranges):
    first = submit(pdf, page_ranges=ranges)
    second = submit(pdf, page_ranges=ranges)
    assert first['status'] == second['status'] == 'queued'
    assert first['job_id'] != second['job_id']
    job = Path(first['output_directory'])
    request = worker.read_json(job / 'request.json')
    assert request['source_sha256'] == worker.file_hash(pdf)
    assert request['options']['page_ranges'] == ranges.strip()
    assert 'confirm_upload' not in request['options']
    process = server.PROCESSES[first['job_id']]
    assert process.kwargs['stdout'] == subprocess.DEVNULL
    assert process.kwargs['stderr'] == subprocess.DEVNULL
    assert server.get_status(first['job_id'])['status'] == 'queued'


def test_success_complete_mock_pipeline(pdf, backend, fake_spawn, monkeypatch):
    monkeypatch.setenv('MINERU_API_TOKEN', 'secret-token-test')
    calls = []
    archive = bundle()

    def post(url, **kwargs):
        calls.append(('POST', url))
        assert kwargs['headers']['Authorization'] == 'Bearer secret-token-test'
        return Response({'code': 0, 'data': {'batch_id': 'offline',
                       'file_urls': ['https://store.invalid/upload?signature=private']}})

    def put(url, **kwargs):
        calls.append(('PUT', url))
        assert kwargs['data'].read() == pdf.read_bytes()
        return Response()

    def get(url, **kwargs):
        calls.append(('GET', url))
        if kwargs.get('stream'):
            return Response(content=archive)
        return Response({'code': 0, 'data': {'extract_result': [{'state': 'done',
                       'full_zip_url': 'https://store.invalid/result?signature=private'}]}})

    monkeypatch.setattr(backend.requests, 'post', post)
    monkeypatch.setattr(backend.requests, 'put', put)
    monkeypatch.setattr(backend.requests, 'get', get)
    before = pdf.read_bytes()
    submitted = submit(pdf)
    job = Path(submitted['output_directory'])
    assert worker.run_job(job, backend) == 0
    result = server.get_status(submitted['job_id'])
    assert result['status'] == 'succeeded'
    assert result['source_unchanged'] is True
    assert result['image_count'] == 1
    markdown = Path(result['markdown_path']).read_text(encoding='utf-8')
    assert 'document_files/fig.png' in markdown
    manifest = worker.read_json(Path(result['manifest_path']))
    assert manifest[0]['pixel_width'] == 4
    assert manifest[0]['usage_count'] == 1
    assert pdf.read_bytes() == before
    assert [call[0] for call in calls] == ['POST', 'PUT', 'GET', 'GET']
    for path in job.rglob('*.json'):
        assert 'secret-token-test' not in path.read_text(encoding='utf-8')
        assert 'signature=private' not in path.read_text(encoding='utf-8')


def test_failure_never_retries_or_exposes_secrets(pdf, backend, fake_spawn, monkeypatch):
    monkeypatch.setenv('MINERU_API_TOKEN', 'private-token')
    calls = []

    def fail(url, **kwargs):
        calls.append(url)
        raise requests.exceptions.ProxyError('proxy://username:password@host?token=private-token')

    monkeypatch.setattr(backend.requests, 'post', fail)
    submitted = submit(pdf)
    job = Path(submitted['output_directory'])
    assert worker.run_job(job, backend) == 1
    result = server.get_status(submitted['job_id'])
    assert result['error']['code'] == 'proxy_error'
    assert result['remote_task_may_exist'] is True
    assert len(calls) == 1
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in ['private-token', 'username', 'password'])


@pytest.mark.parametrize('exception,code', [
    (requests.exceptions.SSLError('secret'), 'tls_error'),
    (requests.exceptions.Timeout('secret'), 'network_timeout'),
    (requests.exceptions.ConnectionError('secret'), 'connection_error'),
    (RuntimeError('MinerU parsing timed out.'), 'parsing_timeout'),
    (RuntimeError('secret (code=-6001)'), 'mineru_api_error'),
    (RuntimeError('secret https://private.invalid/'), 'conversion_failed'),
])
def test_safe_errors(exception, code):
    error = worker.safe_error(exception)
    assert error['code'] == code
    assert 'secret' not in json.dumps(error)


def test_http_error_reports_only_status():
    response = requests.Response()
    response.status_code = 403
    response._content = b'private token and signed url'
    exc = requests.HTTPError('private URL', response=response)
    assert worker.safe_error(exc)['http_status'] == 403
    assert 'private' not in json.dumps(worker.safe_error(exc))


def test_source_changed_blocks_conversion(pdf, backend, fake_spawn, monkeypatch):
    monkeypatch.setenv('MINERU_API_TOKEN', 'offline')
    monkeypatch.setattr(backend, 'convert_with_mineru', lambda *a, **k: pytest.fail('uploaded changed input'))
    submitted = submit(pdf)
    pdf.write_bytes(pdf.read_bytes() + b'changed')
    job = Path(submitted['output_directory'])
    assert worker.run_job(job, backend) == 1
    status = server.get_status(submitted['job_id'])
    assert status['error']['code'] == 'source_changed'
    assert status['remote_task_may_exist'] is False


def test_missing_credentials_no_upload(pdf, backend, fake_spawn, monkeypatch):
    monkeypatch.setattr(backend, 'convert_with_mineru', lambda *a, **k: pytest.fail('uploaded without token'))
    submitted = submit(pdf)
    assert worker.run_job(Path(submitted['output_directory']), backend) == 1
    assert server.get_status(submitted['job_id'])['error']['code'] == 'missing_credentials'


def test_configuration_environment_alias_precedence(backend, tmp_path, monkeypatch):
    env_file = tmp_path / 'chosen.env'
    env_file.write_text('MINERU_API_TOKEN="from-file" # comment\nMINERU_API_BASE_URL=https://file.invalid\n')
    monkeypatch.setenv('MINERU_TOKEN', 'from-environment')
    monkeypatch.setenv('MINERU_BASE_URL', 'https://environment.invalid/')
    assert worker.api_settings(backend, env_file) == ('from-environment', 'https://environment.invalid')


@pytest.mark.parametrize('url', ['http://not-tls.invalid', 'https://user:password@host.invalid',
                                'https://host.invalid/?token=secret', 'https://host.invalid/#secret'])
def test_rejects_unsafe_api_base(backend, tmp_path, monkeypatch, url):
    monkeypatch.setenv('MINERU_API_BASE_URL', url)
    with pytest.raises(ValueError):
        worker.api_settings(backend, tmp_path / 'none.env')


def test_worker_crash_and_restart_identity(pdf, fake_spawn, monkeypatch):
    submitted = submit(pdf)
    job_id = submitted['job_id']
    server.PROCESSES[job_id].returncode = 1
    result = server.get_status(job_id)
    assert result['error']['code'] == 'worker_interrupted'
    assert result['remote_task_may_exist'] is True
    second = submit(pdf)
    server.PROCESSES.clear()
    assert server.get_status(second['job_id'])['status'] == 'queued'
    monkeypatch.setattr(server, 'process_marker', lambda pid: 'different-process-same-pid')
    assert server.get_status(second['job_id'])['error']['code'] == 'worker_interrupted'


def test_invalid_unknown_and_unreadable_jobs(tmp_path):
    assert server.get_status('../secret')['error']['code'] == 'invalid_job_id'
    assert server.get_status(str(uuid.uuid4()))['error']['code'] == 'job_or_output_not_found'
    job_id = str(uuid.uuid4())
    job = server.JOBS / job_id
    job.mkdir(parents=True)
    (job / 'status.json').write_text('broken')
    assert server.get_status(job_id)['error']['code'] == 'status_unreadable'


def test_live_process_identity():
    marker = worker.process_marker(os.getpid())
    assert marker and marker == worker.process_marker(os.getpid())
    assert worker.process_marker(429496729) is None


def test_real_subprocess_without_credentials(pdf):
    submitted = submit(pdf)
    process = server.PROCESSES[submitted['job_id']]
    process.wait(timeout=20)
    assert process.returncode == 1
    assert server.get_status(submitted['job_id'])['error']['code'] == 'missing_credentials'


def test_atomic_publication_retries_only_metadata(tmp_path, monkeypatch):
    target = tmp_path / 'status.json'
    original = Path.replace
    attempts = []

    def busy_once(path, destination):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError('sharing violation')
            error.winerror = 32
            raise error
        return original(path, destination)

    monkeypatch.setattr(Path, 'replace', busy_once)
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: None)
    worker.write_json(target, {'status': 'succeeded'})
    assert worker.read_json(target)['status'] == 'succeeded'
    assert len(attempts) == 2


def test_protocol_full_conversion_keeps_session_open(pdf, tmp_path):
    project = tmp_path / 'isolated-mcp'
    project.mkdir()
    for name in ('server.py', 'worker.py', 'config.py', 'console_encoding.py', 'mineru_backend.py'):
        (project / name).write_bytes((ROOT / name).read_bytes())
    archive = base64.b64encode(bundle()).decode('ascii')
    fake_module = '''import base64
import time
from pathlib import Path
import mineru_real_backend as real_backend

ARCHIVE = %(archive)r

def convert_with_mineru(source, output, **options):
    time.sleep(0.15)
    archive_path = output.parent / 'offline.zip'
    archive_path.write_bytes(base64.b64decode(ARCHIVE))
    return real_backend.normalize_mineru_zip(archive_path, output)
''' % {'archive': archive}
    (project / 'mineru_real_backend.py').write_bytes((ROOT / 'mineru_backend.py').read_bytes())
    (project / 'mineru_backend.py').write_text(fake_module, encoding='utf-8')
    before = pdf.read_bytes()

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=['-X', 'utf8', str(project / 'server.py')],
            env={**os.environ,
                 'MINERU_LOCAL_ENV_FILE': str(os.environ['MINERU_LOCAL_ENV_FILE']),
                 'MINERU_API_TOKEN': 'offline-only',
                 'MINERU_LOCAL_JOB_DIR': str(tmp_path / 'subprocess-jobs')},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool('start_pdf_conversion', {'input_path': str(pdf), 'confirm_upload': True})
                submitted = result.structuredContent
                assert submitted['status'] == 'queued'
                with anyio.fail_after(20):
                    while True:
                        status_result = await session.call_tool('get_conversion_status', {'job_id': submitted['job_id']})
                        status = status_result.structuredContent
                        if status['status'] not in ('queued', 'running'):
                            break
                        await anyio.sleep(0.1)
                assert status['status'] == 'succeeded', status
                assert status['image_count'] == 1
                assert status['source_unchanged'] is True
                assert Path(status['markdown_path']).is_file()
    anyio.run(run)
    assert pdf.read_bytes() == before


def test_protocol_health_and_denied_upload():
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-X', 'utf8', str(ROOT / 'server.py')],
                                       env={**os.environ,
                                            'MINERU_LOCAL_ENV_FILE': os.environ['MINERU_LOCAL_ENV_FILE']})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {'health_check', 'start_pdf_conversion', 'get_conversion_status'}
                start_tool = next(tool for tool in tools if tool.name == 'start_pdf_conversion')
                assert start_tool.annotations.openWorldHint is True
                result = await session.call_tool('health_check', {})
                assert not result.isError
                assert result.structuredContent['cloud_processing'] is True
                assert result.structuredContent['credentials_available'] is False
                rejected = await session.call_tool('start_pdf_conversion', {'input_path': 'unused.pdf'})
                assert rejected.structuredContent['error']['code'] == 'upload_not_confirmed'
    anyio.run(run)
