"""The vocabulary Stage G is written in (sections 1, 32, 40, 93, 96).

Three ideas are defined here once, so nothing anywhere else invents its own:

* **what a backend is for** - an image model, a video model, an upscaler, a
  background remover, or (architecture only, for now) a language model;
* **what state a provider is really in** - the seven precise words section 93
  allows, with no way to say "PASS" about something that never ran;
* **how a job is doing** - the eight states section 32 lists.

Every one of these is a plain string constant rather than an enum: these values
travel through job payloads, log lines, JSON metadata and the GUI, and a plain
string survives that round trip without a conversion step that could change it.
"""

from __future__ import annotations

__all__ = [
    "BackendKind",
    "BACKEND_KINDS",
    "KIND_LABELS",
    "ProviderState",
    "PROVIDER_STATES",
    "STATE_MEANINGS",
    "is_reachable",
    "DeviceRequirement",
    "DEVICE_REQUIREMENTS",
    "REQUIREMENT_LABELS",
    "AIJobState",
    "JOB_STATES",
    "JOB_STATE_LABELS",
    "TERMINAL_JOB_STATES",
    "AIOperation",
    "AI_OPERATIONS",
    "OPERATION_LABELS",
    "OPERATION_KIND",
    "IMAGE_OPERATIONS",
    "VIDEO_OPERATIONS",
    "VerificationLevel",
    "VERIFICATION_LEVELS",
    "VERIFICATION_LABELS",
]


class BackendKind:
    """What a backend produces (section 1).

    ``LLM`` exists so the architecture has a named slot for a future local
    language model.  Nothing implements it in this build, and no backend reports
    it: an empty category is honest, an implemented-but-fake one is not.
    """

    IMAGE = "image"
    VIDEO = "video"
    UPSCALE = "upscale"
    BACKGROUND_REMOVAL = "background_removal"
    LLM = "llm"


BACKEND_KINDS: tuple[str, ...] = (
    BackendKind.IMAGE, BackendKind.VIDEO, BackendKind.UPSCALE,
    BackendKind.BACKGROUND_REMOVAL, BackendKind.LLM,
)

KIND_LABELS: dict[str, str] = {
    BackendKind.IMAGE: "Image generation",
    BackendKind.VIDEO: "Video generation",
    BackendKind.UPSCALE: "Upscaling",
    BackendKind.BACKGROUND_REMOVAL: "Background removal",
    BackendKind.LLM: "Language model",
}


class ProviderState:
    """The only words that may describe a backend or model (section 93).

    ``PASS`` is deliberately absent.  A provider is never "passing": it is
    installed and usable, installed but unproven, or not there at all - and the
    UI shows which.
    """

    #: Ran here, on this machine, and produced a real result.
    VERIFIED = "VERIFIED"
    #: Installed and usable; a generation with it has not been run by the check.
    AVAILABLE = "AVAILABLE"
    #: Not present on this machine.
    NOT_INSTALLED = "NOT_INSTALLED"
    #: Present, but nothing has proven it works yet.
    NOT_VERIFIED = "NOT_VERIFIED"
    #: Present, but it cannot do the thing being asked about.
    NOT_SUPPORTED = "NOT_SUPPORTED"
    #: The check could not run (a dependency it needs is missing).
    CHECK_NOT_AVAILABLE = "CHECK NOT AVAILABLE"
    #: Works for part of what it claims, or with a stated restriction.
    LIMITED = "LIMITED"


PROVIDER_STATES: tuple[str, ...] = (
    ProviderState.VERIFIED, ProviderState.AVAILABLE, ProviderState.NOT_INSTALLED,
    ProviderState.NOT_VERIFIED, ProviderState.NOT_SUPPORTED,
    ProviderState.CHECK_NOT_AVAILABLE, ProviderState.LIMITED,
)

STATE_MEANINGS: dict[str, str] = {
    ProviderState.VERIFIED: "It ran on this machine and produced a real result.",
    ProviderState.AVAILABLE: "It is installed and usable; the check did not run it.",
    ProviderState.NOT_INSTALLED: "It is not installed on this machine.",
    ProviderState.NOT_VERIFIED: "It is installed, but nothing has proven it works.",
    ProviderState.NOT_SUPPORTED: "It is installed, but cannot do this.",
    ProviderState.CHECK_NOT_AVAILABLE: "The check could not run, so nothing is claimed.",
    ProviderState.LIMITED: "It works, with a stated restriction.",
}

#: States in which a provider can actually be used for a generation.
USABLE_STATES: frozenset[str] = frozenset(
    {ProviderState.VERIFIED, ProviderState.AVAILABLE, ProviderState.LIMITED})


def is_reachable(state: str) -> bool:
    """Whether a provider in this state can be asked to do work."""
    return str(state) in USABLE_STATES


def state_label(state: str) -> str:
    """A state as the user should read it: ``NOT_INSTALLED`` -> ``NOT INSTALLED``.

    The words are the contract (section 93), so this only spaces them out - it
    never softens NOT INSTALLED into something that sounds usable.
    """
    text = str(state or "")
    if text in STATE_MEANINGS:
        return text if " " in text else text.replace("_", " ")
    return text.replace("_", " ")


def device_label(requirement: str) -> str:
    """``GPU_REQUIRED`` -> ``GPU required``, and the same for the others."""
    text = str(requirement or DeviceRequirement.UNKNOWN)
    return text.replace("_", " ").capitalize()


class DeviceRequirement:
    """What a model needs from the machine (section 40).

    This exists so "the application runs without a GPU" is never confused with
    "every model runs on CPU".  A CPU-only machine reports the application as
    usable and a GPU-required model as unusable, in the same breath.
    """

    CPU_SUPPORTED = "CPU_SUPPORTED"
    GPU_RECOMMENDED = "GPU_RECOMMENDED"
    GPU_REQUIRED = "GPU_REQUIRED"
    UNKNOWN = "UNKNOWN"


DEVICE_REQUIREMENTS: tuple[str, ...] = (
    DeviceRequirement.CPU_SUPPORTED, DeviceRequirement.GPU_RECOMMENDED,
    DeviceRequirement.GPU_REQUIRED, DeviceRequirement.UNKNOWN,
)

REQUIREMENT_LABELS: dict[str, str] = {
    DeviceRequirement.CPU_SUPPORTED: "Runs on CPU",
    DeviceRequirement.GPU_RECOMMENDED: "CPU works; a supported GPU is faster",
    DeviceRequirement.GPU_REQUIRED: "Requires a supported GPU",
    DeviceRequirement.UNKNOWN: "Device requirement unknown",
}


class AIJobState:
    """The eight states a job passes through (section 32)."""

    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PROCESSING = "PROCESSING"
    SAVING = "SAVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


JOB_STATES: tuple[str, ...] = (
    AIJobState.QUEUED, AIJobState.INITIALIZING, AIJobState.RUNNING,
    AIJobState.PROCESSING, AIJobState.SAVING, AIJobState.COMPLETED,
    AIJobState.FAILED, AIJobState.CANCELLED,
)

JOB_STATE_LABELS: dict[str, str] = {
    AIJobState.QUEUED: "Queued",
    AIJobState.INITIALIZING: "Initializing",
    AIJobState.RUNNING: "Running",
    AIJobState.PROCESSING: "Processing",
    AIJobState.SAVING: "Saving",
    AIJobState.COMPLETED: "Completed",
    AIJobState.FAILED: "Failed",
    AIJobState.CANCELLED: "Cancelled",
}

TERMINAL_JOB_STATES: frozenset[str] = frozenset(
    {AIJobState.COMPLETED, AIJobState.FAILED, AIJobState.CANCELLED})


class AIOperation:
    """One unit of AI work (sections 32, 66)."""

    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    INPAINT = "inpaint"
    OUTPAINT = "outpaint"
    VARIATION = "variation"
    UPSCALE = "upscale"
    BACKGROUND_REMOVAL = "background_removal"
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    VIDEO_TO_VIDEO = "video_to_video"
    VIDEO_EXTEND = "video_extend"
    STORYBOARD_TO_VIDEO = "storyboard_to_video"
    MODEL_CHECK = "model_check"
    BACKEND_TEST = "backend_test"
    #: The user-facing actions, for the job queue.  A clip job also carries the
    #: exact mode (text to video ...), which is what its title shows; these two
    #: exist so a queue row always has a named operation.
    VIDEO_GENERATE = "video_generate"
    IMAGE_GENERATE = "image_generate"


AI_OPERATIONS: tuple[str, ...] = (
    AIOperation.TEXT_TO_IMAGE, AIOperation.IMAGE_TO_IMAGE, AIOperation.INPAINT,
    AIOperation.OUTPAINT, AIOperation.VARIATION, AIOperation.UPSCALE,
    AIOperation.BACKGROUND_REMOVAL, AIOperation.TEXT_TO_VIDEO,
    AIOperation.IMAGE_TO_VIDEO, AIOperation.VIDEO_TO_VIDEO,
    AIOperation.VIDEO_EXTEND, AIOperation.STORYBOARD_TO_VIDEO,
    AIOperation.MODEL_CHECK, AIOperation.BACKEND_TEST,
    AIOperation.VIDEO_GENERATE, AIOperation.IMAGE_GENERATE,
)

OPERATION_LABELS: dict[str, str] = {
    AIOperation.TEXT_TO_IMAGE: "Text to image",
    AIOperation.IMAGE_TO_IMAGE: "Image to image",
    AIOperation.INPAINT: "Inpaint",
    AIOperation.OUTPAINT: "Outpaint",
    AIOperation.VARIATION: "Variation",
    AIOperation.UPSCALE: "Upscale",
    AIOperation.BACKGROUND_REMOVAL: "Remove background",
    AIOperation.TEXT_TO_VIDEO: "Text to video",
    AIOperation.IMAGE_TO_VIDEO: "Image to video",
    AIOperation.VIDEO_TO_VIDEO: "Video to video",
    AIOperation.VIDEO_EXTEND: "Extend clip",
    AIOperation.STORYBOARD_TO_VIDEO: "Storyboard to video",
    AIOperation.MODEL_CHECK: "Check model",
    AIOperation.BACKEND_TEST: "Test backend",
    AIOperation.VIDEO_GENERATE: "Generate clip",
    AIOperation.IMAGE_GENERATE: "Generate image",
}

#: Which kind of backend runs which operation.
OPERATION_KIND: dict[str, str] = {
    AIOperation.TEXT_TO_IMAGE: BackendKind.IMAGE,
    AIOperation.IMAGE_TO_IMAGE: BackendKind.IMAGE,
    AIOperation.INPAINT: BackendKind.IMAGE,
    AIOperation.OUTPAINT: BackendKind.IMAGE,
    AIOperation.VARIATION: BackendKind.IMAGE,
    AIOperation.UPSCALE: BackendKind.UPSCALE,
    AIOperation.BACKGROUND_REMOVAL: BackendKind.BACKGROUND_REMOVAL,
    AIOperation.TEXT_TO_VIDEO: BackendKind.VIDEO,
    AIOperation.IMAGE_TO_VIDEO: BackendKind.VIDEO,
    AIOperation.VIDEO_TO_VIDEO: BackendKind.VIDEO,
    AIOperation.VIDEO_EXTEND: BackendKind.VIDEO,
    AIOperation.STORYBOARD_TO_VIDEO: BackendKind.VIDEO,
    AIOperation.MODEL_CHECK: "",
    AIOperation.BACKEND_TEST: "",
    AIOperation.VIDEO_GENERATE: BackendKind.VIDEO,
    AIOperation.IMAGE_GENERATE: BackendKind.IMAGE,
}

IMAGE_OPERATIONS: tuple[str, ...] = tuple(
    name for name in AI_OPERATIONS if OPERATION_KIND.get(name) in
    (BackendKind.IMAGE, BackendKind.UPSCALE, BackendKind.BACKGROUND_REMOVAL))

VIDEO_OPERATIONS: tuple[str, ...] = tuple(
    name for name in AI_OPERATIONS if OPERATION_KIND.get(name) == BackendKind.VIDEO)


class VerificationLevel:
    """How far a feature has been proven (section 96).

    The distinction section 96 asks for is between something an automated test
    really exercised here, and something that needs hardware or a model this
    machine does not have.
    """

    AUTOMATIC = "AUTOMATICALLY VERIFIED"
    MANUAL_PENDING = "MANUAL VERIFICATION PENDING"
    NOT_INSTALLED = "NOT INSTALLED"
    NOT_SUPPORTED = "NOT SUPPORTED"
    NOT_VERIFIED = "NOT VERIFIED"
    LIMITED = "LIMITED"


VERIFICATION_LEVELS: tuple[str, ...] = (
    VerificationLevel.AUTOMATIC, VerificationLevel.MANUAL_PENDING,
    VerificationLevel.NOT_INSTALLED, VerificationLevel.NOT_SUPPORTED,
    VerificationLevel.NOT_VERIFIED, VerificationLevel.LIMITED,
)

VERIFICATION_LABELS: dict[str, str] = {
    VerificationLevel.AUTOMATIC: "An automated test on this machine exercised it.",
    VerificationLevel.MANUAL_PENDING: "Written and tested in parts; a real run is still owed.",
    VerificationLevel.NOT_INSTALLED: "The software or model it needs is not installed here.",
    VerificationLevel.NOT_SUPPORTED: "This build does not implement it.",
    VerificationLevel.NOT_VERIFIED: "Implemented, but nothing has proven it works yet.",
    VerificationLevel.LIMITED: "Works with a stated restriction.",
}
