"""Pydantic schemas for API request/response validation."""

from datetime import datetime

from pydantic import BaseModel


class CameraCreate(BaseModel):
    name: str
    rtsp_url: str
    enabled: bool = True


class CameraUpdate(BaseModel):
    name: str | None = None
    rtsp_url: str | None = None
    enabled: bool | None = None


class CameraResponse(BaseModel):
    id: int
    name: str
    rtsp_url: str
    enabled: bool
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    fps: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RecordingResponse(BaseModel):
    id: int
    camera_id: int
    file_path: str
    start_time: datetime
    end_time: datetime | None = None
    file_size: int | None = None
    duration: float | None = None

    model_config = {"from_attributes": True}


class StreamStatus(BaseModel):
    camera_id: int
    camera_name: str
    running: bool
    pid: int | None = None
    uptime_seconds: float | None = None
    error: str | None = None


class SystemStatus(BaseModel):
    streams: list[StreamStatus]
    total_cameras: int
    active_streams: int


class CameraStorageStats(BaseModel):
    camera_id: int
    segment_count: int
    total_size_bytes: int
    oldest_recording: str | None = None
    newest_recording: str | None = None


class StorageStats(BaseModel):
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    recordings_total_bytes: int
    max_storage_bytes: int
    max_age_days: int
    camera_stats: list[CameraStorageStats]


class RetentionResult(BaseModel):
    deleted: int
    freed_bytes: int


class ClipExportCreate(BaseModel):
    camera_id: int
    start_time: datetime
    end_time: datetime


class ClipExportResponse(BaseModel):
    id: int
    camera_id: int
    start_time: datetime
    end_time: datetime
    file_path: str | None = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
