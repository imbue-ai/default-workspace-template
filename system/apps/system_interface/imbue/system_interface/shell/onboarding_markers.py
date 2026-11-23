"""The shell's writes to the workspace's onboarding markers (``onboarding_activity``): what its owner has already
done, which the rest of the workspace reads to decide what is still worth suggesting."""

from loguru import logger
from onboarding_activity.markers import Marker
from onboarding_activity.markers import MarkerNotWrittenError
from onboarding_activity.markers import mark


def set_onboarding_marker(marker: Marker) -> None:
    """Set the marker, or refresh its time. A marker that cannot be written is logged and left unset: whatever it
    would have retired may be suggested again, which is no reason to fail the request that noticed the fact."""
    try:
        mark(marker)
    except MarkerNotWrittenError as error:
        logger.warning("Could not set the onboarding marker {}: {}", marker.value, error)
