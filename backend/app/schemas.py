"""Pydantic schemas for API request/response validation."""

from datetime import datetime

from pydantic import BaseModel


class MotionScriptConfig(BaseModel):
    on: str | None = None
    off: str | None = None
    persons: bool = True
    vehicles: bool = True
    animals: bool = True
    motion_only: bool = True
    off_delay: int = 10  # seconds after last motion before off-script runs
    faces: list[int] = []  # if non-empty, fires only when any listed Face id is present
    face_unknown: bool = False  # fires when an unknown face is detected


class CameraGroupCreate(BaseModel):
    name: str


class CameraGroupUpdate(BaseModel):
    name: str | None = None
    sort_order: int | None = None


class CameraGroupResponse(BaseModel):
    id: int
    name: str
    sort_order: int = 0
    camera_count: int = 0

    model_config = {"from_attributes": True}


class CameraCreate(BaseModel):
    name: str
    rtsp_url: str
    sub_stream_url: str | None = None
    enabled: bool = True
    rotation: int = 0
    sort_order: int = 0
    group_id: int | None = None
    motion_sensitivity: int = 100
    motion_script: str | None = None
    motion_script_off: str | None = None
    motion_scripts: list[MotionScriptConfig] | None = None
    ai_detection: bool = True
    ai_detect_persons: bool = True
    ai_detect_vehicles: bool = True
    ai_detect_animals: bool = True
    ai_confidence_threshold: int = 50
    face_recognition: bool = False
    face_match_threshold: int = 50


class CameraUpdate(BaseModel):
    name: str | None = None
    rtsp_url: str | None = None
    sub_stream_url: str | None = None
    enabled: bool | None = None
    rotation: int | None = None
    sort_order: int | None = None
    group_id: int | None = None
    motion_sensitivity: int | None = None
    motion_script: str | None = None
    motion_script_off: str | None = None
    motion_scripts: list[MotionScriptConfig] | None = None
    ai_detection: bool | None = None
    ai_detect_persons: bool | None = None
    ai_detect_vehicles: bool | None = None
    ai_detect_animals: bool | None = None
    ai_confidence_threshold: int | None = None
    face_recognition: bool | None = None
    face_match_threshold: int | None = None


class CameraResponse(BaseModel):
    id: int
    name: str
    rtsp_url: str
    sub_stream_url: str | None = None
    enabled: bool
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    fps: float | None = None
    rotation: int = 0
    sort_order: int = 0
    group_id: int | None = None
    motion_sensitivity: int = 0
    motion_script: str | None = None
    motion_script_off: str | None = None
    motion_scripts: list[MotionScriptConfig] = []
    ai_detection: bool = False
    ai_detect_persons: bool = True
    ai_detect_vehicles: bool = False
    ai_detect_animals: bool = False
    ai_confidence_threshold: int = 50
    face_recognition: bool = False
    face_match_threshold: int = 50
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_camera(cls, camera):
        import json
        scripts = []
        if camera.motion_scripts:
            try:
                scripts = [MotionScriptConfig(**s) for s in json.loads(camera.motion_scripts)]
            except (json.JSONDecodeError, TypeError):
                pass
        data = {c.key: getattr(camera, c.key) for c in camera.__table__.columns}
        data["motion_scripts"] = scripts
        return cls(**data)


class FaceMatchInfo(BaseModel):
    face_id: int
    name: str
    confidence: float


class MotionEventResponse(BaseModel):
    id: int
    camera_id: int
    start_time: datetime
    end_time: datetime | None = None
    peak_intensity: float
    detection_label: str | None = None
    detection_confidence: float | None = None
    has_thumbnail: bool = False
    face_matches: list[FaceMatchInfo] = []
    face_unknown: bool = False

    model_config = {"from_attributes": True}

    @classmethod
    def from_event(cls, event, camera_id: int | None = None):
        import json
        matches: list[FaceMatchInfo] = []
        if event.face_matches:
            try:
                for m in json.loads(event.face_matches):
                    matches.append(FaceMatchInfo(**m))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return cls(
            id=event.id,
            camera_id=event.camera_id,
            start_time=event.start_time,
            end_time=event.end_time,
            peak_intensity=event.peak_intensity,
            detection_label=event.detection_label,
            detection_confidence=event.detection_confidence,
            has_thumbnail=bool(event.thumbnail_path),
            face_matches=matches,
            face_unknown=bool(getattr(event, "face_unknown", False)),
        )


class FaceCreate(BaseModel):
    name: str
    notes: str | None = None


class FaceUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None


class FaceEmbeddingInfo(BaseModel):
    id: int
    source_thumbnail_path: str | None = None
    face_crop_path: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FaceResponse(BaseModel):
    id: int
    name: str
    notes: str | None = None
    embedding_count: int = 0
    latest_crop_path: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FaceEnrollRequest(BaseModel):
    source_thumbnail_path: str
    bbox: list[int] | None = None  # [x1, y1, x2, y2] — choose a specific detected face


class FaceEnrollCandidate(BaseModel):
    bbox: list[int]
    score: float


class FaceEnrollResponse(BaseModel):
    status: str  # "enrolled" | "multiple_faces" | "no_face"
    embedding_id: int | None = None
    candidates: list[FaceEnrollCandidate] = []
    crop_path: str | None = None


class UnlabeledThumb(BaseModel):
    event_id: int
    camera_id: int
    camera_name: str
    start_time: datetime
    thumbnail_url: str
    detection_label: str | None = None
    assigned_face_names: list[str] = []  # Faces already enrolled from this thumbnail


class RecordingResponse(BaseModel):
    id: int
    camera_id: int
    file_path: str
    start_time: datetime
    end_time: datetime | None = None
    file_size: int | None = None
    duration: float | None = None
    in_progress: bool = False

    model_config = {"from_attributes": True}


class StreamStatus(BaseModel):
    camera_id: int
    camera_name: str
    running: bool
    pid: int | None = None
    uptime_seconds: float | None = None
    error: str | None = None
    go2rtc_connected: bool | None = None
    go2rtc_consumers: int | None = None


class SystemStatus(BaseModel):
    streams: list[StreamStatus]
    total_cameras: int
    active_streams: int
    go2rtc_rtsp_port: int = 18554


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


class ThumbnailInfo(BaseModel):
    timestamp: str       # "HH:MM:SS"
    url: str             # "/api/recordings/{camera_id}/thumb/{date}/{filename}"
    thumb_width: int
    thumb_height: int
    interval: int        # capture interval in seconds (for staleness cutoff)


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


class TestScriptRequest(BaseModel):
    command: str


class TestScriptResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
