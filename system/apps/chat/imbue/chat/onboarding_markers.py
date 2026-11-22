"""The chat app's writes to the workspace's onboarding markers (``onboarding_activity``): what its owner has
already done, which the activity suggestions read to decide what is still worth offering."""

from loguru import logger as _loguru_logger
from onboarding_activity.markers import Marker
from onboarding_activity.markers import MarkerNotWrittenError
from onboarding_activity.markers import mark

logger = _loguru_logger


def set_onboarding_marker(marker: Marker) -> bool:
    """Set the marker, or refresh its time; False when it could not be written. That is logged and never raised:
    the suggestion the marker would have retired may be offered again, which is no reason to fail the request or
    the read that noticed the fact."""
    try:
        mark(marker)
    except MarkerNotWrittenError as error:
        logger.warning("Could not set the onboarding marker {}: {}", marker.value, error)
        return False
    return True
