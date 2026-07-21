import asyncio
import logging
import os
import re
import unicodedata
from datetime import date, datetime
from typing import Any

import boto3
import cv2
import numpy as np
from botocore.config import Config


logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1").strip()
BIOMETRIC_ENABLED = os.getenv("BIOMETRIC_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "sim",
}
FACE_SIMILARITY_THRESHOLD = float(os.getenv("FACE_SIMILARITY_THRESHOLD", "85"))
MAX_VIDEO_FRAMES = max(3, min(int(os.getenv("MAX_VIDEO_FRAMES", "5")), 8))

DATE_PATTERN = re.compile(r"(?<!\d)([0-3]?\d)[./\-]([01]?\d)[./\-]((?:19|20)\d{2})(?!\d)")
DOCUMENT_WORDS = (
    "CARTEIRA DE IDENTIDADE",
    "REGISTRO GERAL",
    "REPUBLICA FEDERATIVA",
    "SECRETARIA DE SEGURANCA",
    "NASCIMENTO",
    "IDENTIDADE",
)


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in normalized if not unicodedata.combining(char)).upper()


def _calculate_age(birth_date: date) -> int:
    today = datetime.now().date()
    age = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def _parse_date(day: str, month: str, year: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _find_birth_date(lines: list[str]) -> tuple[date | None, float | None]:
    candidates: list[tuple[int, date, float]] = []

    for index, original_line in enumerate(lines):
        line = _normalize(original_line)
        previous = _normalize(lines[index - 1]) if index else ""
        following = _normalize(lines[index + 1]) if index + 1 < len(lines) else ""
        context = f"{previous} {line} {following}"

        for match in DATE_PATTERN.finditer(line):
            parsed = _parse_date(*match.groups())
            if not parsed:
                continue

            age = _calculate_age(parsed)
            score = 0

            if "NASC" in line:
                score += 120
            elif "NASC" in previous:
                score += 90
            elif "NASC" in following:
                score += 45

            if any(word in context for word in ("EMISSAO", "EXPEDICAO", "VALIDADE")):
                score -= 80
            if 14 <= age <= 100:
                score += 30
            else:
                score -= 100

            candidates.append((score, parsed, 0.0))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (item[0], -item[1].toordinal()), reverse=True)
    best_score, best_date, confidence = candidates[0]
    if best_score < 0:
        return None, None
    return best_date, confidence


def _aws_clients():
    config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2})
    session = boto3.session.Session(region_name=AWS_REGION)
    return (
        session.client("textract", config=config),
        session.client("rekognition", config=config),
    )


def _jpeg_bytes(image: np.ndarray, max_side: int = 1800) -> bytes:
    height, width = image.shape[:2]
    largest = max(height, width)
    if largest > max_side:
        scale = max_side / largest
        image = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )

    for quality in (92, 85, 75, 65):
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and len(encoded) <= 4_900_000:
            return encoded.tobytes()
    raise ValueError("Não foi possível preparar a imagem dentro do limite da análise.")


def prepare_document_image(image_bytes: bytes) -> bytes:
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("A imagem do documento não pôde ser lida.")
    return _jpeg_bytes(image)


def _textract_lines(textract, image_bytes: bytes) -> list[tuple[str, float]]:
    response = textract.detect_document_text(Document={"Bytes": image_bytes})
    return [
        (block.get("Text", ""), float(block.get("Confidence", 0.0)))
        for block in response.get("Blocks", [])
        if block.get("BlockType") == "LINE" and block.get("Text")
    ]


def _analyze_document_sync(image_bytes: bytes, declared_birth_date: str) -> dict[str, Any]:
    if not BIOMETRIC_ENABLED:
        return {"available": False, "reason": "Análise automática desativada"}

    textract, _ = _aws_clients()
    prepared = prepare_document_image(image_bytes)
    extracted_lines = _textract_lines(textract, prepared)
    lines = [text for text, _ in extracted_lines]
    birth_date, _ = _find_birth_date(lines)
    average_confidence = (
        sum(confidence for _, confidence in extracted_lines) / len(extracted_lines)
        if extracted_lines
        else 0.0
    )

    declared = datetime.strptime(declared_birth_date, "%d/%m/%Y").date()
    extracted_text = "\n".join(lines)
    normalized_text = _normalize(extracted_text)
    document_detected = any(word in normalized_text for word in DOCUMENT_WORDS)

    return {
        "available": True,
        "text_detected": bool(lines),
        "document_detected": document_detected,
        "ocr_confidence": round(average_confidence, 1),
        "birth_date": birth_date.strftime("%d/%m/%Y") if birth_date else None,
        "age": _calculate_age(birth_date) if birth_date else None,
        "matches_declared": birth_date == declared if birth_date else None,
    }


async def analyze_document(image_bytes: bytes, declared_birth_date: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            _analyze_document_sync,
            image_bytes,
            declared_birth_date,
        )
    except Exception as exc:
        logger.exception("Falha na análise OCR do documento: %s", exc)
        return {"available": False, "reason": type(exc).__name__}


def _extract_video_frames(video_path: str) -> list[bytes]:
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError("O vídeo não pôde ser aberto para análise.")

    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count <= 0:
            raise ValueError("O vídeo não possui quadros válidos.")

        positions = np.linspace(0.15, 0.85, MAX_VIDEO_FRAMES)
        frames: list[bytes] = []
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, int((frame_count - 1) * float(position)))
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(_jpeg_bytes(frame, max_side=1280))
        if not frames:
            raise ValueError("Nenhum quadro do vídeo pôde ser analisado.")
        return frames
    finally:
        capture.release()


def _crop_largest_face(rekognition, frame_bytes: bytes) -> tuple[bytes | None, int]:
    response = rekognition.detect_faces(Image={"Bytes": frame_bytes}, Attributes=["DEFAULT"])
    details = response.get("FaceDetails", [])
    if not details:
        return None, 0

    largest = max(
        details,
        key=lambda item: item["BoundingBox"]["Width"] * item["BoundingBox"]["Height"],
    )
    box = largest["BoundingBox"]
    array = np.frombuffer(frame_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    height, width = image.shape[:2]

    left = max(0, int((box["Left"] - box["Width"] * 0.20) * width))
    top = max(0, int((box["Top"] - box["Height"] * 0.20) * height))
    right = min(width, int((box["Left"] + box["Width"] * 1.20) * width))
    bottom = min(height, int((box["Top"] + box["Height"] * 1.20) * height))
    crop = image[top:bottom, left:right]
    if crop.size == 0:
        return None, len(details)
    return _jpeg_bytes(crop, max_side=1000), len(details)


def _document_visible_in_frame(textract, frame_bytes: bytes, declared_birth_date: str) -> bool:
    try:
        lines = _textract_lines(textract, frame_bytes)
    except Exception:
        return False
    text = _normalize(" ".join(line for line, _ in lines))
    date_variants = {
        declared_birth_date,
        declared_birth_date.replace("/", "-"),
        declared_birth_date.replace("/", "."),
    }
    return any(word in text for word in DOCUMENT_WORDS) or any(value in text for value in date_variants)


def _analyze_video_sync(
    document_bytes: bytes,
    video_path: str,
    declared_birth_date: str,
) -> dict[str, Any]:
    if not BIOMETRIC_ENABLED:
        return {"available": False, "reason": "Análise automática desativada"}

    textract, rekognition = _aws_clients()
    source = prepare_document_image(document_bytes)
    frames = _extract_video_frames(video_path)
    similarities: list[float] = []
    detected_face_frames = 0
    frames_with_multiple_faces = 0

    for frame in frames:
        face_crop, face_count = _crop_largest_face(rekognition, frame)
        if face_count > 0:
            detected_face_frames += 1
        if face_count >= 2:
            frames_with_multiple_faces += 1
        if not face_crop:
            continue

        response = rekognition.compare_faces(
            SourceImage={"Bytes": source},
            TargetImage={"Bytes": face_crop},
            SimilarityThreshold=0,
            QualityFilter="AUTO",
        )
        matches = response.get("FaceMatches", [])
        if matches:
            similarities.append(max(float(match.get("Similarity", 0.0)) for match in matches))

    center_frame = frames[len(frames) // 2]
    document_visible = _document_visible_in_frame(textract, center_frame, declared_birth_date)
    best_similarity = max(similarities) if similarities else None

    return {
        "available": True,
        "frames_analyzed": len(frames),
        "frames_with_face": detected_face_frames,
        "frames_with_multiple_faces": frames_with_multiple_faces,
        "document_visible": document_visible,
        "similarity": round(best_similarity, 1) if best_similarity is not None else None,
        "threshold": FACE_SIMILARITY_THRESHOLD,
        "face_match": best_similarity >= FACE_SIMILARITY_THRESHOLD if best_similarity is not None else None,
    }


async def analyze_video(
    document_bytes: bytes,
    video_path: str,
    declared_birth_date: str,
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            _analyze_video_sync,
            document_bytes,
            video_path,
            declared_birth_date,
        )
    except Exception as exc:
        logger.exception("Falha na comparação biométrica: %s", exc)
        return {"available": False, "reason": type(exc).__name__}


def automatic_recommendation(ocr: dict[str, Any], biometric: dict[str, Any], minimum_age: int) -> str:
    checks = (
        ocr.get("available") is True,
        ocr.get("document_detected") is True,
        isinstance(ocr.get("age"), int) and ocr["age"] >= minimum_age,
        ocr.get("matches_declared") is True,
        biometric.get("available") is True,
        biometric.get("face_match") is True,
        biometric.get("document_visible") is True,
    )
    if all(checks):
        return "✅ APTO PARA DECISÃO MANUAL"
    return "⚠️ REVISÃO MANUAL OBRIGATÓRIA"
