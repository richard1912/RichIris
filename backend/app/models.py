"""SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import local_now
from app.database import Base


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)


class CameraGroup(Base):
    __tablename__ = "camera_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, server_default=func.now())

    cameras: Mapped[list["Camera"]] = relationship(back_populates="group")


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    rtsp_url: Mapped[str] = mapped_column(String(500), nullable=False)
    sub_stream_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    rotation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, server_default=func.now()
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("camera_groups.id", ondelete="SET NULL"), nullable=True)

    motion_sensitivity: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    motion_script: Mapped[str | None] = mapped_column(String(500), nullable=True)
    motion_script_off: Mapped[str | None] = mapped_column(String(500), nullable=True)
    motion_scripts: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_detection: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    ai_detect_persons: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    ai_detect_vehicles: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    ai_detect_animals: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    ai_confidence_threshold: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
    face_recognition: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    face_match_threshold: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    # PTZ driver name (see services/ptz.py). NULL = no driver claims this camera
    # or it has not been probed yet, "none" = probed, no PTZ.
    ptz_driver: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Record the main stream's AAC audio (see build_recording_output). Off by
    # default: before this flag no AAC camera's sound reached the .ts files.
    record_audio: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Recording format: "ts" (ffmpeg MPEG-TS segments), "shadow" (ts AND riv,
    # for verification), "riv" (RIV only; see services/riv).
    rec_format: Mapped[str] = mapped_column(String(10), default="ts", server_default="ts")

    group: Mapped["CameraGroup | None"] = relationship(back_populates="cameras")
    recordings: Mapped[list["Recording"]] = relationship(back_populates="camera")
    clip_exports: Mapped[list["ClipExport"]] = relationship(back_populates="camera")
    motion_events: Mapped[list["MotionEvent"]] = relationship(back_populates="camera")
    zones: Mapped[list["Zone"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    points_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, server_default=func.now(), onupdate=local_now
    )

    camera: Mapped["Camera"] = relationship(back_populates="zones")


class RivFile(Base):
    """One RIV file (services/riv): one camera stream, up to 15 minutes.

    Separate from `recordings` on purpose: every legacy reader of that table
    runs ffmpeg on MPEG-TS, and must never be handed a RIV file.
    Times are naive local, like every other table.
    """
    __tablename__ = "riv_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False, index=True)
    stream: Mapped[str] = mapped_column(String(4), nullable=False)        # "s1" | "s2"
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)        # unix ms, first keyframe
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)    # unix ms, last frame
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keyframes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_progress: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    tier: Mapped[str] = mapped_column(String(10), default="hot", server_default="hot")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_progress: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Two-tier storage bookkeeping: "hot" (fast SSD / data_dir) or "archive"
    # (large HDD). The background flusher flips this to "archive" and rewrites
    # file_path in the same transaction; reads always use the absolute file_path.
    tier: Mapped[str] = mapped_column(String(10), default="hot", server_default="hot")

    camera: Mapped["Camera"] = relationship(back_populates="recordings")


class ClipExport(Base):
    __tablename__ = "clip_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # "single" = one camera; "grid" = synchronized side-by-side composite of
    # several cameras. For grid clips, camera_id holds the first camera (so the
    # clip still surfaces when filtering a camera's timeline) and camera_ids
    # holds the full JSON list of participating camera IDs.
    mode: Mapped[str] = mapped_column(String(20), default="single")
    camera_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, server_default=func.now()
    )

    camera: Mapped["Camera"] = relationship(back_populates="clip_exports")


class MotionEvent(Base):
    __tablename__ = "motion_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    peak_intensity: Mapped[float] = mapped_column(Float, default=0.0)
    detection_label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    detection_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    face_matches: Mapped[str | None] = mapped_column(Text, nullable=True)
    face_unknown: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    face_detected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # JSON array of display-name snapshots for scripts that fired (e.g. ["porch light","Script 2"])
    scripts_fired: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON array of zone-name snapshots whose polygon contained the detection at event start.
    # Bbox anchor (feet/wheels) for AI events; motion-intersection for motion-only events.
    zones_triggered: Mapped[str | None] = mapped_column(Text, nullable=True)

    camera: Mapped["Camera"] = relationship(back_populates="motion_events")


class EventAssessment(Base):
    """What the AI guard (services/guard.py) made of one person event."""
    __tablename__ = "event_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    motion_event_id: Mapped[int] = mapped_column(
        ForeignKey("motion_events.id"), nullable=False, unique=True, index=True)
    camera_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, server_default=func.now())
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | error
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # resident | delivery | passerby | visitor | unknown | none
    subject: Mapped[str | None] = mapped_column(String(20), nullable=True)
    people_count: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    suspicion: Mapped[int] = mapped_column(Integer, default=0)  # 0 none .. 3 high
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    would_alert: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frames_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Face(Base):
    __tablename__ = "faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Nullable: null = auto-clustered suggestion (Immich-style "unassigned person").
    # Uniqueness is enforced below via a partial index on non-null names.
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, server_default=func.now())

    embeddings: Mapped[list["FaceEmbedding"]] = relationship(
        back_populates="face", cascade="all, delete-orphan"
    )


class FaceEmbedding(Base):
    __tablename__ = "face_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    face_id: Mapped[int] = mapped_column(
        ForeignKey("faces.id", ondelete="CASCADE"), nullable=False
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    face_crop_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # "user_enrolled" (tagged manually) or "auto_clustered" (added by background clusterer).
    source: Mapped[str] = mapped_column(String(20), default="user_enrolled", server_default="user_enrolled")
    # SCRFD score at embedding time; used to pick the sharpest thumbnail for cluster tiles.
    detection_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_motion_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("motion_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, server_default=func.now())

    face: Mapped["Face"] = relationship(back_populates="embeddings")


class UnclusteredFace(Base):
    """Queue of unknown-face embeddings awaiting background clustering.

    Rows are written by the motion pipeline when a person is detected but no
    named-face match passes the strict threshold. The face_clusterer worker
    drains pending rows (processed_at IS NULL) and either attaches them to an
    existing Face (named or unnamed) or creates a new unnamed Face cluster.
    """

    __tablename__ = "unclustered_faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    motion_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("motion_events.id", ondelete="CASCADE"), nullable=True
    )
    camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    face_crop_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    detection_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, server_default=func.now())
    # NULL = pending. Set when the clusterer has assigned this row to a Face.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_face_id: Mapped[int | None] = mapped_column(
        ForeignKey("faces.id", ondelete="SET NULL"), nullable=True
    )
