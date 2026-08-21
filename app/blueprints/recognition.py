"""Camera and recognition control.

Every route here was ``@csrf_exempt`` before, which meant any page on the internet could
start the camera on this machine or stop an in-progress attendance session with a
cross-site form post.  CSRF applies now; the JavaScript sends the token in an
``X-CSRFToken`` header (see ``static/js/csrf.js``).
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify
from flask_login import login_required

from app.extensions import limiter
from app.recognition import CameraError, get_pipeline
from app.services.student_service import known_students

recognition_bp = Blueprint("recognition", __name__)


def _known_payload():
    from app.recognition import KnownStudent

    return [
        KnownStudent(pk=student.id, name=student.name, roll=student.student_id)
        for student in known_students()
    ]


@recognition_bp.route("/start_detection", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def start_detection():
    """Open the camera for preview only, without recognition."""
    pipeline = get_pipeline()
    try:
        pipeline.start(preview_only=True)
    except CameraError as exc:
        return jsonify({"success": False, "message": str(exc)}), 503
    return jsonify({"success": True, "message": "Camera started", "status": pipeline.status()})


@recognition_bp.route("/start_face_recognition", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def start_face_recognition():
    pipeline = get_pipeline()
    students = _known_payload()
    if not students:
        return jsonify(
            {
                "success": False,
                "message": (
                    "No students have a face enrolment yet. Register students with a "
                    "clear photo first."
                ),
            }
        ), 409

    if not pipeline.recognizer.is_trained:
        # Recover from a missing or stale model file without an admin round trip.
        from app.services.student_service import rebuild_face_model

        rebuild_face_model()

    pipeline.load_known(students)
    try:
        pipeline.start(preview_only=False)
    except CameraError as exc:
        return jsonify({"success": False, "message": str(exc)}), 503

    return jsonify(
        {
            "success": True,
            "message": (
                f"Face recognition started with {len(students)} enrolled students. "
                f"A student must be recognised in "
                f"{current_app.config['RECOGNITION_CONFIRM_FRAMES']} consecutive frames "
                f"before attendance is marked."
            ),
            "status": pipeline.status(),
        }
    )


@recognition_bp.route("/stop_detection", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def stop_detection():
    pipeline = get_pipeline()
    pipeline.stop()
    return jsonify({"success": True, "message": "Camera stopped"})


@recognition_bp.route("/stop_face_recognition", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def stop_face_recognition():
    pipeline = get_pipeline()
    pipeline.stop()
    return jsonify({"success": True, "message": "Face recognition stopped"})


@recognition_bp.route("/get_video_feed")
@login_required
def video_feed():
    pipeline = get_pipeline()
    if not pipeline.is_running:
        return jsonify({"success": False, "message": "Camera is not running"}), 409
    return Response(
        pipeline.mjpeg_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@recognition_bp.route("/get_detected_faces")
@login_required
@limiter.exempt
def detected_faces():
    """Polled by the recognition UI a few times a second."""
    pipeline = get_pipeline()
    return jsonify(
        {
            "faces": pipeline.detected_faces(),
            "confirm_frames": pipeline.confirm_frames,
            "recognizing": pipeline.is_recognizing,
        }
    )
