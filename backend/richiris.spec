# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for RichIris NVR backend."""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # FastAPI + Uvicorn
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # SQLAlchemy + aiosqlite
        'aiosqlite',
        'sqlalchemy.dialects.sqlite',
        'sqlalchemy.dialects.sqlite.aiosqlite',
        # App modules
        'app.main',
        'app.config',
        'app.database',
        'app.logging_config',
        'app.models',
        'app.schemas',
        'app.routers.cameras',
        'app.routers.clips',
        'app.routers.motion',
        'app.routers.recordings',
        'app.routers.settings',
        'app.routers.storage',
        'app.routers.streams',
        'app.routers.system',
        'app.services.clip_exporter',
        'app.services.ffmpeg',
        'app.services.go2rtc_client',
        'app.services.go2rtc_manager',
        'app.services.job_object',
        'app.services.motion_detector',
        'app.services.object_detector',
        'app.services.playback',
        'app.services.recorder',
        'app.services.retention',
        'app.services.settings',
        'app.services.storage_migration',
        'app.services.stream_manager',
        'app.services.thumbnail_capture',
        # structlog
        'structlog',
        'structlog.stdlib',
        'structlog.dev',
        'structlog.processors',
        # Other
        'yaml',
        'httpx',
        'anyio',
        'anyio._backends._asyncio',
        'multipart',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'test',
        'unittest',
        # Removed: ultralytics + torch (replaced by onnxruntime-directml)
        'torch', 'torchvision', 'torchaudio',
        'ultralytics',
        'pytorch_lightning', 'pytorch_metric_learning',
        # Transitive deps of ultralytics/torch not needed
        'scipy', 'sklearn', 'pandas', 'polars',
        'pyarrow', 'llvmlite', 'numba',
        'matplotlib', 'seaborn',
        'imageio', 'imageio_ffmpeg',
        'transformers', 'huggingface_hub', 'safetensors',
        'botocore', 'boto3', 's3transfer',
        'tensorboard', 'tensorboardX',
        'IPython', 'jupyter', 'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='richiris',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # Service runs headless
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='richiris',
)
