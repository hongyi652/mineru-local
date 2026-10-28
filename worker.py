from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import ENV_KEYS, default_config_path, load_settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 19:
                raise
            # Windows readers can briefly deny delete-sharing during atomic replacement.
            time.sleep(0.05)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def source_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parent
    configured = (os.environ.get('MINERU_LOCAL_CONFIG_FILE') or
                  os.environ.get('MINERU_LOCAL_ENV_FILE') or '').strip()
    config_file = Path(configured).expanduser().resolve() if configured else default_config_path()
    return root, config_file


def load_backend(root: Path):
    del root
    import mineru_backend
    return mineru_backend


def api_settings(backend, config_file: Path) -> tuple[str, str]:
    del backend
    settings = load_settings(config_file)
    return settings.token, settings.base_url


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def process_marker(pid: int) -> str | None:
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            stat = Path(f'/proc/{pid}/stat')
            return stat.read_text().rsplit(')', 1)[1].split()[19] if stat.is_file() else str(pid)
        except (OSError, IndexError):
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def safe_error(exc: Exception) -> dict:
    import requests
    chain = []
    current = exc
    while current is not None and id(current) not in {id(item) for item in chain}:
        chain.append(current)
        current = current.__cause__ or current.__context__
    categories = [
        (requests.exceptions.SSLError, 'tls_error', 'MinerU连接的TLS证书验证失败，未禁用证书校验。'),
        (requests.exceptions.ProxyError, 'proxy_error', 'MinerU代理连接失败，请检查本机代理配置。'),
        (requests.exceptions.Timeout, 'network_timeout', 'MinerU上传、下载或网络请求超时。'),
        (requests.exceptions.ConnectionError, 'connection_error', '无法连接MinerU或其文件存储服务。'),
    ]
    for kind, code, message in categories:
        if any(isinstance(item, kind) for item in chain):
            return {'code': code, 'message': message}
    for item in chain:
        response = getattr(item, 'response', None)
        if response is not None:
            return {'code': 'http_error', 'http_status': response.status_code,
                    'message': 'MinerU服务或文件存储返回HTTP错误，未自动重新上传。'}
    if str(exc) == 'MinerU parsing timed out.':
        return {'code': 'parsing_timeout', 'message': 'MinerU解析等待超时，云端任务可能仍在运行。'}
    match = re.search(r'\(code=(-?\d+)\)', str(exc))
    if match:
        return {'code': 'mineru_api_error', 'api_code': int(match.group(1)),
                'message': 'MinerU API拒绝请求，请检查凭据、额度和服务状态。'}
    return {'code': 'conversion_failed',
            'message': '转换未完成；请检查源文件、API配置或云端任务状态。未自动重传，原始异常未输出以保护凭据。'}


def run_job(job_dir: Path, backend=None) -> int:
    request = read_json(job_dir / 'request.json')
    status = {'job_id': request['job_id'], 'status': 'running', 'input_path': request['input_path'],
              'output_directory': str(job_dir), 'created_at': request['created_at'], 'started_at': now(),
              'source_sha256': request['source_sha256']}
    write_json(job_dir / 'status.json', status)
    started = time.monotonic()

    def hard_timeout():
        write_json(job_dir / 'status.json', dict(status, status='failed', finished_at=now(),
                   error={'code': 'worker_timeout', 'message': '转换超过最大总时限，云端任务状态未知，未自动重传。'}))
        os._exit(124)

    timer = threading.Timer(request['options']['timeout_seconds'] + 600, hard_timeout)
    timer.daemon = True
    timer.start()
    remote_started = False
    try:
        root, config_file = source_paths()
        backend = backend or load_backend(root)
        token, base_url = api_settings(backend, config_file)
        if not token:
            status['error'] = {'code': 'missing_credentials', 'message': '未配置MinerU API token，未上传文件。'}
            raise RuntimeError('missing_credentials')
        source = Path(request['input_path'])
        if file_hash(source) != request['source_sha256']:
            status['error'] = {'code': 'source_changed', 'message': '提交后源PDF已改变，未上传文件。'}
            raise RuntimeError('source_changed')
        output_path = job_dir / 'document.md'
        remote_started = True
        result = backend.convert_with_mineru(source, output_path, api_token=token, base_url=base_url,
                                             poll_interval=3.0, **request['options'])
        if file_hash(source) != request['source_sha256']:
            status['error'] = {'code': 'source_changed', 'message': '转换期间源PDF发生变化，请核对文件版本。'}
            raise RuntimeError('source_changed')
        if not result.markdown_path.is_file() or result.markdown_path.stat().st_size == 0:
            raise RuntimeError('empty_markdown')
        status.update(status='succeeded', markdown_path=str(result.markdown_path),
                      markdown_bytes=result.markdown_path.stat().st_size,
                      asset_directory=str(result.asset_dir) if result.asset_dir else None,
                      image_count=result.image_count,
                      manifest_path=str(result.manifest_path) if result.manifest_path else None,
                      source_unchanged=True)
    except Exception as exc:
        status.update(status='failed', error=status.get('error') or safe_error(exc),
                      remote_task_may_exist=remote_started)
    finally:
        timer.cancel()
    status.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - started, 2))
    write_json(job_dir / 'status.json', status)
    return 0 if status['status'] == 'succeeded' else 1


def health_report() -> dict:
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ('requests', 'PIL')}
    root, config_file = source_paths()
    report = {'ready': False, 'dependencies': dependencies,
              'backend_available': (root / 'mineru_backend.py').is_file(),
              'credentials_file_configured': bool(os.environ.get('MINERU_LOCAL_CONFIG_FILE') or os.environ.get('MINERU_LOCAL_ENV_FILE')),
              'cloud_processing': True}
    try:
        backend = load_backend(root)
        token, _ = api_settings(backend, config_file)
        report.update(credentials_available=bool(token), ready=all(dependencies.values()) and bool(token))
    except Exception:
        report['error'] = {'code': 'configuration_unavailable',
                           'message': '本地后端、依赖或API配置不可用；未联网，未输出凭据及原始异常。'}
    return report


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--health']:
            print(json.dumps(health_report(), ensure_ascii=False))
            raise SystemExit(0)
        raise SystemExit(run_job(Path(sys.argv[1]).resolve()))
    except Exception:
        raise SystemExit(1)
