"""Stage D - scene background jobs.

The job bodies are executed synchronously here (the same functions the GUI
thread pool calls), which proves the work, the file output and the cancellation
behaviour without needing Qt.  A separate GUI test drives them through the real
JobManager to prove they run off-thread.
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.jobs.cancel import CancelToken
from app.jobs.keys import JobKeys
from app.jobs.progress import ProgressReporter
from app.jobs.spec import JobContext
from app.project.model import build_project
from app.scene import jobs
from app.scene.templates import create_scene_from_template, default_templates_registered


@pytest.fixture(scope="module", autouse=True)
def registry():
    default_templates_registered()
    yield


@pytest.fixture()
def project_with_image(tmp_path):
    assets = tmp_path / "proj" / "assets"
    assets.mkdir(parents=True)
    image = Image.new("RGB", (640, 360), (200, 60, 60))
    image.save(assets / "logo.png")

    project = build_project("Preview Me")
    project.scenes = [
        create_scene_from_template("title", {"title": "Opening"}),
        create_scene_from_template("image", {"asset_id": "logo", "text": "caption"}),
        create_scene_from_template("chart", {"values": [1, 2, 3], "labels": ["a", "b", "c"]}),
    ]
    # Attach the asset so the image element resolves to the real file.
    from app.project.model import AssetSpec, new_id

    asset = AssetSpec(id="logo", name="logo.png", kind="image", path="assets/logo.png")
    asset.id = asset.id or new_id("asset")
    project.assets.append(asset)
    return project, tmp_path / "proj"


def _context(payload, *, previews: object, cancelled: bool = False):
    cancel = CancelToken()
    if cancelled:
        cancel.cancel("test")
    return JobContext(
        job_id="test-job",
        key=JobKeys.SCENE_PREVIEW,
        cancel=cancel,
        progress=ProgressReporter(),
        payload=dict(payload, previews_dir=str(previews)),
    )


def test_storyboard_job_writes_a_thumbnail_per_scene(project_with_image, tmp_path):
    project, project_dir = project_with_image
    previews = tmp_path / "previews"
    context = _context({"project": project, "project_dir": str(project_dir)}, previews=previews)

    result = jobs.storyboard_body(context)

    assert result["count"] == 3
    for row in result["rows"]:
        path = row["thumbnail_path"]
        assert path
        with Image.open(path) as image:
            assert image.size[0] > 0
    # Thumbnails live in the preview folder, never in the project.
    assert str(previews) in result["rows"][0]["thumbnail_path"]
    assert not (project_dir / "scene-000.png").exists()


def test_storyboard_job_requires_a_project(tmp_path):
    context = _context({}, previews=tmp_path)
    with pytest.raises(ValueError):
        jobs.storyboard_body(context)


def test_storyboard_job_reports_progress(project_with_image, tmp_path):
    project, project_dir = project_with_image
    context = _context({"project": project, "project_dir": str(project_dir)}, previews=tmp_path)
    jobs.storyboard_body(context)
    assert context.progress.progress.fraction == pytest.approx(1.0)


def test_scene_preview_job_renders_one_full_resolution_frame(project_with_image, tmp_path):
    project, project_dir = project_with_image
    scene = project.scenes[0]
    previews = tmp_path / "previews"

    result = jobs.scene_preview_body(
        JobContext(job_id="p", key=JobKeys.SCENE_PREVIEW, cancel=CancelToken(),
                   progress=ProgressReporter(),
                   payload={"project": project, "project_dir": str(project_dir),
                            "previews_dir": str(previews), "scene_id": scene.id, "time": 1.0}))

    assert result["scene_id"] == scene.id
    with Image.open(result["path"]) as image:
        # Full project resolution, not a thumbnail.
        assert image.size == (project.format.width, project.format.height)


def test_scene_preview_job_unknown_scene_raises(project_with_image, tmp_path):
    project, project_dir = project_with_image
    context = JobContext(job_id="p", key=JobKeys.SCENE_PREVIEW, cancel=CancelToken(),
                         progress=ProgressReporter(),
                         payload={"project": project, "project_dir": str(project_dir),
                                  "previews_dir": str(tmp_path), "scene_id": "missing"})
    with pytest.raises(ValueError):
        jobs.scene_preview_body(context)


def test_cancelled_storyboard_stops_between_scenes(project_with_image, tmp_path):
    project, project_dir = project_with_image
    cancel = CancelToken()
    cancel.cancel("user stopped")
    context = JobContext(job_id="c", key=JobKeys.STORYBOARD_RENDER, cancel=cancel,
                         progress=ProgressReporter(),
                         payload={"project": project, "project_dir": str(project_dir),
                                  "previews_dir": str(tmp_path)})
    with pytest.raises(Exception):
        jobs.storyboard_body(context)  # raise_if_cancelled at the start


def test_preview_spec_carries_the_key_and_payload():
    spec = jobs.scene_preview_spec({"project": object()}, scene_id="s1", time=2.0)
    assert spec.key == JobKeys.SCENE_PREVIEW
    assert spec.payload["scene_id"] == "s1"
    assert spec.payload["time"] == 2.0
    assert spec.allow_parallel is False

    sb = jobs.storyboard_spec({"project": object()}, long_edge=240)
    assert sb.key == JobKeys.STORYBOARD_RENDER
    assert sb.payload["long_edge"] == 240
