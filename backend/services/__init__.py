"""后端服务层：摄像头、模型推理、串口通信、检测流水线与数据标注。"""

from .annotator import AnnotateError, Annotator
from .camera import CameraError, CameraInfo, CameraService
from .detector import Detection, DetectorError, DetectorService, draw_detections
from .pipeline import Pipeline, PipelineError
from .serial_esp32 import SerialError, SerialPortInfo, SerialService

__all__ = [
    "Annotator",
    "AnnotateError",
    "CameraService",
    "CameraInfo",
    "CameraError",
    "DetectorService",
    "Detection",
    "DetectorError",
    "draw_detections",
    "SerialService",
    "SerialPortInfo",
    "SerialError",
    "Pipeline",
    "PipelineError",
]
