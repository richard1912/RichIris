"""SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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
        DateTime, server_default=func.now()
    )

    motion_sensitivity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    motion_script: Mapped[str | None] = mapped_column(String(500), nullable=True)
    motion_script_off: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ai_detection: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    ai_detect_persons: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    ai_detect_vehicles: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    ai_detect_animals: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    ai_confidence_threshold: Mapped[int] = mapped_column(Integer, default=50, server_default="50")

    recordings: Mapped[list["Recording"]] = relationship(back_populates="camera")
    clip_exports: Mapped[list["ClipExport"]] = relationship(back_populates="camera")
    motion_events: Mapped[list["MotionEvent"]] = relationship(back_populates="camera")


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

    camera: Mapped["Camera"] = relationship(back_populates="recordings")


class ClipExport(Base):
    __tablename__ = "clip_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
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

    camera: Mapped["Camera"] = relationship(back_populates="motion_events")
