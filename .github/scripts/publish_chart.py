#!/usr/bin/env python3
"""Validate and publish an Octopus Helm chart snapshot to GHCR."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

STABLE_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[1-9][0-9]*$")
BOT_LOGIN = "bullet-release[bot]"
REGISTRY = "ghcr.io"
OWNER = "bulletxyz"
CHART_PREFIX = "charts"
REPOSITORY = f"{OWNER}/helm"
# GHCR packages are scoped to the owner, not to this repository.
IMAGE_REPOSITORY = f"{REGISTRY}/{OWNER}"
CHART_REPOSITORY = f"{REGISTRY}/{OWNER}/{CHART_PREFIX}"
# Chart -> the one image it deploys (keep identical to public_helm_publish.py).
CHART_IMAGES = {"hyperlane-bullet-validator": "hyperlane-bullet-validator"}
TRAILERS = (
    "Public-Chart",
    "Chart-Version",
    "App-Version",
    "Image-Name",
    "Image-Digest",
    "Image-Archive-Sha256",
    "Image-Release-Id",
    "Image-Asset-Id",
    "Octopus-Source",
    "Octopus-Release",
)

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYERS = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
    }
)
PLATFORMS = frozenset({("linux", "amd64"), ("linux", "arm64")})
# Accepted variants besides none. arm64 and arm64/v8 are the same platform, and the
# index and image config may each state or omit v8.
PLATFORM_VARIANTS = {"amd64": frozenset(), "arm64": frozenset({"v8"})}
DEFAULT_VARIANTS = {"arm64": "v8"}
LAYOUT_ENTRY_RE = re.compile(r"^(?:oci-layout|index\.json|blobs/sha256/[0-9a-f]{64})$")
LAYOUT_DIRS = frozenset({"blobs", "blobs/sha256"})
MANIFEST_LIMIT = 4 * 1024 * 1024
CHUNK = 1024 * 1024
# Registry error codes for a missing tag or package.
ABSENT_CODES = (": MANIFEST_UNKNOWN: ", ": NAME_UNKNOWN: ")


class PublishError(RuntimeError):
    pass


def require_mapping(value: object, subject: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PublishError(f"{subject} must be an object.")
    return value


def require_string(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value:
        raise PublishError(f"{subject} must be a non-empty string.")
    return value


def require_list(value: object, subject: str) -> list[object]:
    if not isinstance(value, list):
        raise PublishError(f"{subject} must be a list.")
    return value


def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=False, capture_output=True, text=True)


def checked_output(args: Sequence[str]) -> str:
    completed = run(args)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise PublishError(f"Command {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def append_output(path: str, **values: str) -> None:
    with open(path, "a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in values.items())


def gh_json(args: Sequence[str]) -> Mapping[str, object]:
    return require_mapping(
        json.loads(checked_output(["gh", "api", *args])), "GitHub API"
    )


def require_chart(chart: str) -> None:
    if chart not in CHART_IMAGES:
        raise PublishError(f"Unsupported public chart {chart!r}.")


def require_image(chart: str, image: str) -> None:
    require_chart(chart)
    if image != CHART_IMAGES[chart]:
        raise PublishError(f"Public chart {chart} does not publish image {image!r}.")


def release_tag(chart: str, chart_version: str) -> str:
    require_chart(chart)
    if STABLE_SEMVER_RE.fullmatch(chart_version) is None:
        raise PublishError("Chart version is not exact stable semantic versioning.")
    return f"{chart}-{chart_version}"


def asset_name(chart: str, image: str, app_version: str) -> str:
    require_image(chart, image)
    if STABLE_SEMVER_RE.fullmatch(app_version) is None:
        raise PublishError("App version is not exact stable semantic versioning.")
    return f"{image}-{app_version}.oci.tar"


def commit_trailers(commit: str) -> dict[str, str]:
    raw = checked_output(
        ["git", "show", "-s", "--format=%(trailers:only,unfold=true)", commit]
    )
    trailers: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            raise PublishError("Snapshot commit has a malformed trailer.")
        name, value = (item.strip() for item in line.split(":", 1))
        if name in trailers:
            raise PublishError(f"Snapshot commit repeats trailer {name}.")
        trailers[name] = value
    return trailers


def validate_trailers(trailers: Mapping[str, str]) -> None:
    if tuple(trailers) != TRAILERS:
        raise PublishError(
            "Snapshot trailers must be exactly, in order: " + ", ".join(TRAILERS) + "."
        )
    require_image(trailers["Public-Chart"], trailers["Image-Name"])
    for name in ("Chart-Version", "App-Version"):
        if STABLE_SEMVER_RE.fullmatch(trailers[name]) is None:
            raise PublishError(
                f"Snapshot {name} trailer is not exact stable semantic versioning."
            )
    if DIGEST_RE.fullmatch(trailers["Image-Digest"]) is None:
        raise PublishError("Snapshot Image-Digest trailer is not a sha256 digest.")
    if SHA256_RE.fullmatch(trailers["Image-Archive-Sha256"]) is None:
        raise PublishError("Snapshot Image-Archive-Sha256 trailer is not a sha256.")
    for name in ("Image-Release-Id", "Image-Asset-Id"):
        if ID_RE.fullmatch(trailers[name]) is None:
            raise PublishError(f"Snapshot {name} trailer is not a numeric id.")
    if SHA_RE.fullmatch(trailers["Octopus-Source"]) is None:
        raise PublishError("Snapshot Octopus-Source trailer is not a full SHA.")
    require_string(trailers["Octopus-Release"], "Snapshot Octopus-Release trailer")


def chart_field(text: str, key: str) -> str:
    matches = [line for line in text.splitlines() if line.startswith(f"{key}:")]
    if len(matches) != 1:
        raise PublishError(f"Chart.yaml must declare {key} exactly once.")
    value = matches[0][len(key) + 1 :].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def verify_chart_metadata(
    commit: str, chart: str, chart_version: str, app_version: str
) -> None:
    path = f"{commit}:charts/{chart}/Chart.yaml"
    text = checked_output(["git", "show", path])
    expected = {"name": chart, "version": chart_version, "appVersion": app_version}
    for key, value in expected.items():
        if chart_field(text, key) != value:
            raise PublishError(f"{path} {key} does not match the snapshot trailers.")


def validate_trigger(args: argparse.Namespace) -> None:
    """Check that the run publishes a snapshot commit of public main.

    Runs from the workflow's trusted checkout, so it reads the snapshot via git only.
    """
    if args.repository != REPOSITORY:
        raise PublishError(
            f"Publication repository must be {REPOSITORY}, got {args.repository!r}."
        )
    if args.ref != "refs/heads/main":
        raise PublishError(
            f"Publication ref must be refs/heads/main, got {args.ref!r}."
        )
    event = require_mapping(
        json.loads(Path(args.event).read_text(encoding="utf-8")), "GitHub event"
    )
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    commit = args.sha
    if SHA_RE.fullmatch(commit) is None:
        raise PublishError("Publication commit must be a full lowercase SHA.")

    if event_name == "push":
        if args.actor != BOT_LOGIN:
            raise PublishError(
                f"Snapshot pushes must come from {BOT_LOGIN}, got {args.actor!r}."
            )
        before = require_string(event.get("before"), "push before SHA").lower()
        if (
            event.get("after") != commit
            or event.get("ref") != "refs/heads/main"
            or event.get("forced") is not False
            or require_mapping(event.get("head_commit"), "push head commit").get("id")
            != commit
        ):
            raise PublishError("Push is not the exact non-force main snapshot update.")
        commits = event.get("commits")
        if not isinstance(commits, list) or len(commits) != 1:
            raise PublishError("Snapshot push must contain exactly one commit.")
        parents = checked_output(["git", "show", "-s", "--format=%P", commit]).split()
        if parents != [before]:
            raise PublishError(
                "Snapshot commit is not the one direct child of push before."
            )
    elif event_name == "workflow_dispatch":
        # Any actor may re-publish: only the release App can update main and
        # publishing a main snapshot again is idempotent.
        inputs = require_mapping(event.get("inputs"), "workflow dispatch inputs")
        if inputs.get("commit") != commit:
            raise PublishError("Recovery dispatch inputs are not exact.")
    else:
        raise PublishError(f"Unsupported publication event {event_name!r}.")

    # An older snapshot may no longer be HEAD. First-parent history excludes
    # commits that only reached main through a merge.
    history = checked_output(
        ["git", "rev-list", "--first-parent", "refs/remotes/origin/main"]
    ).split()
    if commit not in history:
        raise PublishError(
            "Publication commit is not on the first-parent history of public main."
        )

    trailers = commit_trailers(commit)
    validate_trailers(trailers)
    chart = trailers["Public-Chart"]
    chart_version = trailers["Chart-Version"]
    app_version = trailers["App-Version"]
    verify_chart_metadata(commit, chart, chart_version, app_version)
    append_output(
        args.output,
        commit=commit,
        chart=chart,
        chart_version=chart_version,
        app_version=app_version,
        image=trailers["Image-Name"],
        image_digest=trailers["Image-Digest"],
        archive_sha256=trailers["Image-Archive-Sha256"],
        release_id=trailers["Image-Release-Id"],
        asset_id=trailers["Image-Asset-Id"],
        tag=release_tag(chart, chart_version),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def extract_layout(archive: Path, destination: Path) -> None:
    """Extract an OCI layout tar, allowing only the layout's own regular files."""
    if destination.exists() and any(destination.iterdir()):
        raise PublishError(f"Layout destination {destination} is not empty.")
    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    try:
        with tarfile.open(archive, mode="r:") as tar:
            for member in tar:
                name = member.name.rstrip("/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or str(path) != name:
                    raise PublishError(
                        f"Image archive has unsafe path {member.name!r}."
                    )
                if name in seen:
                    raise PublishError(f"Image archive repeats entry {name!r}.")
                seen.add(name)
                if member.isdir() and name in LAYOUT_DIRS:
                    continue
                if not member.isreg():
                    raise PublishError(
                        f"Image archive entry {name!r} is not a regular file."
                    )
                if LAYOUT_ENTRY_RE.fullmatch(name) is None:
                    raise PublishError(f"Image archive has unexpected entry {name!r}.")
                source = tar.extractfile(member)
                if source is None:
                    raise PublishError(f"Image archive entry {name!r} is unreadable.")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, CHUNK)
    except tarfile.TarError as error:
        raise PublishError(f"Image archive is malformed: {error}") from error


def load_json(path: Path, subject: str) -> Mapping[str, object]:
    try:
        if path.stat().st_size > MANIFEST_LIMIT:
            raise PublishError(f"{subject} exceeds {MANIFEST_LIMIT} bytes.")
        return require_mapping(json.loads(path.read_bytes()), subject)
    except FileNotFoundError as error:
        raise PublishError(f"{subject} is missing.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishError(f"{subject} is not valid JSON.") from error


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.referenced: set[str] = set()

    def blob(self, descriptor: object, subject: str) -> tuple[Path, str]:
        mapping = require_mapping(descriptor, subject)
        digest = require_string(mapping.get("digest"), f"{subject} digest")
        media_type = require_string(mapping.get("mediaType"), f"{subject} mediaType")
        size = mapping.get("size")
        if DIGEST_RE.fullmatch(digest) is None:
            raise PublishError(f"{subject} digest is not a sha256 digest.")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise PublishError(f"{subject} size is not a non-negative integer.")
        path = self.root / "blobs" / "sha256" / digest.removeprefix("sha256:")
        if not path.is_file():
            raise PublishError(f"{subject} blob {digest} is missing.")
        if path.stat().st_size != size:
            raise PublishError(f"{subject} blob {digest} has the wrong size.")
        self.referenced.add(path.name)
        return path, media_type

    def document(
        self, descriptor: object, subject: str, media_type: str
    ) -> Mapping[str, object]:
        path, declared = self.blob(descriptor, subject)
        if declared != media_type:
            raise PublishError(f"{subject} mediaType must be {media_type}.")
        document = load_json(path, subject)
        if document.get("schemaVersion") != 2:
            raise PublishError(f"{subject} schemaVersion must be 2.")
        if "mediaType" in document and document["mediaType"] != media_type:
            raise PublishError(f"{subject} content mediaType must be {media_type}.")
        return document


def verify_blob_names(root: Path) -> None:
    for path in sorted((root / "blobs" / "sha256").glob("*")):
        if file_sha256(path) != path.name:
            raise PublishError(f"Image blob sha256:{path.name} does not match content.")


def platform_variant(platform: Mapping[str, object], subject: str) -> str:
    """Return the platform's variant, filling in the architecture's default."""
    variant = platform.get("variant", "")
    if not isinstance(variant, str):
        raise PublishError(f"{subject} variant must be a string.")
    architecture = platform.get("architecture")
    if not variant and isinstance(architecture, str):
        return DEFAULT_VARIANTS.get(architecture, "")
    return variant


def verify_platform(entry: object, subject: str) -> tuple[str, str, str]:
    platform = require_mapping(
        require_mapping(entry, subject).get("platform"), f"{subject} platform"
    )
    os_name = require_string(platform.get("os"), f"{subject} os")
    architecture = require_string(platform.get("architecture"), f"{subject} arch")
    if (os_name, architecture) not in PLATFORMS:
        raise PublishError(
            f"Image index has unexpected platform {os_name}/{architecture}."
        )
    variant = platform_variant(platform, subject)
    if variant and variant not in PLATFORM_VARIANTS[architecture]:
        raise PublishError(
            f"Image index has unexpected {architecture} variant {variant!r}."
        )
    return os_name, architecture, variant


def verify_layout(root: Path, digest: str) -> None:
    if load_json(root / "oci-layout", "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise PublishError("oci-layout must declare imageLayoutVersion 1.0.0.")
    if not (root / "blobs" / "sha256").is_dir():
        raise PublishError("Image layout has no sha256 blobs.")
    verify_blob_names(root)
    layout = Layout(root)
    top = load_json(root / "index.json", "index.json")
    if top.get("schemaVersion") != 2:
        raise PublishError("index.json schemaVersion must be 2.")
    descriptors = require_list(top.get("manifests"), "index.json manifests")
    if len(descriptors) != 1:
        raise PublishError("index.json must hold exactly one manifest descriptor.")
    if require_mapping(descriptors[0], "index.json descriptor").get("digest") != digest:
        raise PublishError(f"index.json descriptor is not the expected image {digest}.")
    index = layout.document(descriptors[0], "image index", OCI_INDEX)

    platforms: set[tuple[str, str]] = set()
    entries = require_list(index.get("manifests"), "image index manifests")
    for number, entry in enumerate(entries):
        subject = f"image index entry {number}"
        os_name, architecture, variant = verify_platform(entry, subject)
        if (os_name, architecture) in platforms:
            raise PublishError(
                f"Image index repeats platform {os_name}/{architecture}."
            )
        platforms.add((os_name, architecture))
        manifest = layout.document(entry, subject, OCI_MANIFEST)
        config_path, config_type = layout.blob(
            manifest.get("config"), f"{subject} config"
        )
        if config_type != OCI_CONFIG:
            raise PublishError(f"{subject} config mediaType must be {OCI_CONFIG}.")
        config = load_json(config_path, f"{subject} config")
        if (
            config.get("os") != os_name
            or config.get("architecture") != architecture
            or platform_variant(config, f"{subject} config") != variant
        ):
            raise PublishError(f"{subject} config platform differs from the index.")
        layers = require_list(manifest.get("layers"), f"{subject} layers")
        if not layers:
            raise PublishError(f"{subject} has no layers.")
        for layer_number, layer in enumerate(layers):
            _, layer_type = layout.blob(layer, f"{subject} layer {layer_number}")
            if layer_type not in OCI_LAYERS:
                raise PublishError(
                    f"{subject} layer {layer_number} is not an OCI layer."
                )
    if platforms != PLATFORMS or len(entries) != len(PLATFORMS):
        raise PublishError("Image index must hold exactly linux/amd64 and linux/arm64.")

    blobs = {path.name for path in (root / "blobs" / "sha256").iterdir()}
    if blobs != layout.referenced:
        raise PublishError("Image layout contains unreferenced blobs.")


def verify_archive(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    if file_sha256(archive) != args.sha256:
        raise PublishError("Image archive sha256 does not match Image-Archive-Sha256.")
    destination = Path(args.destination).resolve()
    extract_layout(archive, destination)
    verify_layout(destination, args.digest)
    append_output(args.output, layout=str(destination))


def check_release(args: argparse.Namespace) -> None:
    tag = release_tag(args.chart, args.chart_version)
    expected_asset = asset_name(args.chart, args.image, args.app_version)
    release = gh_json([f"repos/{args.repository}/releases/{args.release_id}"])
    author = require_mapping(release.get("author"), "GitHub release author")
    if (
        release.get("tag_name") != tag
        or release.get("name") != tag
        or author.get("login") != BOT_LOGIN
        or not isinstance(release.get("draft"), bool)
    ):
        raise PublishError(
            f"GitHub release {args.release_id} is not the trusted {tag}."
        )
    assets = require_list(release.get("assets"), "GitHub release assets")
    if not any(
        require_mapping(asset, "GitHub release asset").get("id") == int(args.asset_id)
        for asset in assets
    ):
        raise PublishError(
            f"Asset {args.asset_id} does not belong to release {args.release_id}."
        )
    asset = gh_json([f"repos/{args.repository}/releases/assets/{args.asset_id}"])
    digest = asset.get("digest")
    if (
        asset.get("name") != expected_asset
        or asset.get("state") != "uploaded"
        or digest not in (None, f"sha256:{args.archive_sha256}")
    ):
        raise PublishError(f"Release asset {args.asset_id} is not {expected_asset}.")


def registry_digest(reference: str) -> str | None:
    completed = run(["crane", "digest", reference])
    if completed.returncode != 0:
        if any(code in completed.stderr for code in ABSENT_CODES):
            return None
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise PublishError(f"Unable to resolve {reference}: {detail}")
    digest = completed.stdout.strip()
    if DIGEST_RE.fullmatch(digest) is None:
        raise PublishError(f"crane returned a malformed digest for {reference}.")
    return digest


def publish_image(args: argparse.Namespace) -> None:
    require_image(args.chart, args.image)
    reference = f"{IMAGE_REPOSITORY}/{args.image}:{args.app_version}"
    existing = registry_digest(reference)
    if existing is None:
        # crane pushes the layout's single image index as-is, keeping its digest.
        pushed = checked_output(["crane", "push", args.layout, reference])
        if pushed != f"{IMAGE_REPOSITORY}/{args.image}@{args.digest}":
            raise PublishError(f"crane pushed {pushed!r}, expected {args.digest}.")
        existing = registry_digest(reference)
    if existing != args.digest:
        raise PublishError(f"{reference} is {existing}, expected {args.digest}.")


def chart_contents(archive: Path) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for member in tar:
                name = member.name
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or not member.isreg():
                    raise PublishError(f"Chart archive {archive} has unsafe {name!r}.")
                if name in contents:
                    raise PublishError(f"Chart archive {archive} repeats {name!r}.")
                source = tar.extractfile(member)
                if source is None:
                    raise PublishError(f"Chart archive entry {name!r} is unreadable.")
                with source:
                    contents[name] = source.read()
    except tarfile.TarError as error:
        raise PublishError(f"Chart archive {archive} is malformed: {error}") from error
    return contents


def single_file(directory: Path) -> Path:
    files = list(directory.iterdir())
    if len(files) != 1 or not files[0].is_file():
        raise PublishError(f"Expected exactly one chart archive in {directory}.")
    return files[0]


def publish_chart(args: argparse.Namespace) -> None:
    require_chart(args.chart)
    reference = f"{CHART_REPOSITORY}/{args.chart}:{args.chart_version}"
    with tempfile.TemporaryDirectory() as temp:
        packaged_dir = Path(temp) / "packaged"
        pulled_dir = Path(temp) / "pulled"
        packaged_dir.mkdir()
        pulled_dir.mkdir()
        checked_output(
            [
                "helm",
                "package",
                str(Path("charts") / args.chart),
                "--destination",
                str(packaged_dir),
            ]
        )
        packaged = single_file(packaged_dir)
        if packaged.name != f"{args.chart}-{args.chart_version}.tgz":
            raise PublishError(f"helm packaged unexpected {packaged.name}.")
        if registry_digest(reference) is None:
            checked_output(["helm", "push", str(packaged), f"oci://{CHART_REPOSITORY}"])
        # helm package output is not byte-reproducible; compare archived files.
        checked_output(
            [
                "helm",
                "pull",
                f"oci://{CHART_REPOSITORY}/{args.chart}",
                "--version",
                args.chart_version,
                "--destination",
                str(pulled_dir),
            ]
        )
        expected = chart_contents(packaged)
        actual = chart_contents(single_file(pulled_dir))
    if expected != actual:
        differing = sorted(
            name
            for name in expected.keys() | actual.keys()
            if expected.get(name) != actual.get(name)
        )
        raise PublishError(
            f"{reference} differs from this snapshot: {', '.join(differing)}."
        )


def trusted_release(
    release: Mapping[str, object], tag: str, release_id: str
) -> Mapping[str, object]:
    author = require_mapping(release.get("author"), "GitHub release author")
    if (
        release.get("tag_name") != tag
        or release.get("name") != tag
        or author.get("login") != BOT_LOGIN
    ):
        raise PublishError(f"GitHub release {release_id} is not the trusted {tag}.")
    return release


def finalize_release(args: argparse.Namespace) -> None:
    tag = release_tag(args.chart, args.chart_version)
    release_path = f"repos/{args.repository}/releases/{args.release_id}"
    release = trusted_release(gh_json([release_path]), tag, args.release_id)
    ref_path = f"repos/{args.repository}/git/ref/tags/{tag}"
    try:
        ref = gh_json([ref_path])
    except PublishError as error:
        if "HTTP 404" not in str(error):
            raise
        ref = gh_json(
            [
                "--method",
                "POST",
                f"repos/{args.repository}/git/refs",
                "-f",
                f"ref=refs/tags/{tag}",
                "-f",
                f"sha={args.commit}",
            ]
        )
    target = require_mapping(ref.get("object"), "Git tag target")
    if target.get("type") != "commit" or target.get("sha") != args.commit:
        raise PublishError(f"Existing {tag} does not point directly to {args.commit}.")

    if release.get("draft") is True:
        # A repository-wide "Latest" release is meaningless across charts.
        gh_json(
            [
                "--method",
                "PATCH",
                release_path,
                "-f",
                f"tag_name={tag}",
                "-f",
                f"target_commitish={args.commit}",
                "-F",
                "draft=false",
                "-f",
                "make_latest=false",
            ]
        )
        release = trusted_release(gh_json([release_path]), tag, args.release_id)
    if release.get("draft") is not False:
        raise PublishError(f"GitHub release {tag} is still a draft.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-trigger")
    validate.add_argument("--event", required=True)
    validate.add_argument("--sha", required=True)
    validate.add_argument("--actor", required=True)
    validate.add_argument("--ref", required=True)
    validate.add_argument("--repository", required=True)
    validate.add_argument("--output", required=True)
    validate.set_defaults(handler=validate_trigger)

    release = commands.add_parser("check-release")
    release.add_argument("--repository", required=True)
    release.add_argument("--chart", required=True)
    release.add_argument("--chart-version", required=True)
    release.add_argument("--app-version", required=True)
    release.add_argument("--image", required=True)
    release.add_argument("--release-id", required=True)
    release.add_argument("--asset-id", required=True)
    release.add_argument("--archive-sha256", required=True)
    release.set_defaults(handler=check_release)

    archive = commands.add_parser("verify-archive")
    archive.add_argument("--archive", required=True)
    archive.add_argument("--sha256", required=True)
    archive.add_argument("--digest", required=True)
    archive.add_argument("--destination", required=True)
    archive.add_argument("--output", required=True)
    archive.set_defaults(handler=verify_archive)

    image = commands.add_parser("publish-image")
    image.add_argument("--chart", required=True)
    image.add_argument("--image", required=True)
    image.add_argument("--app-version", required=True)
    image.add_argument("--digest", required=True)
    image.add_argument("--layout", required=True)
    image.set_defaults(handler=publish_image)

    chart = commands.add_parser("publish-chart")
    chart.add_argument("--chart", required=True)
    chart.add_argument("--chart-version", required=True)
    chart.set_defaults(handler=publish_chart)

    finalize = commands.add_parser("finalize-release")
    finalize.add_argument("--repository", required=True)
    finalize.add_argument("--chart", required=True)
    finalize.add_argument("--chart-version", required=True)
    finalize.add_argument("--commit", required=True)
    finalize.add_argument("--release-id", required=True)
    finalize.set_defaults(handler=finalize_release)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.handler(args)
    except (PublishError, json.JSONDecodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
