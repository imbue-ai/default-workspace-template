"""The request identity and the provenance check, which live in the app SDK (``imbue.app_sdk.identity``).

# CLEANUP: delete this module once no supported workspace holds an app written before the SDK that has not been
# published since; nothing built in imports it, it is kept only for such apps, and publishing one is what moves it
# to the SDK's names.
"""

from imbue.app_sdk.identity import ANONYMOUS_OWNER as ANONYMOUS_OWNER
from imbue.app_sdk.identity import FETCH_MODE_HEADER as FETCH_MODE_HEADER
from imbue.app_sdk.identity import FETCH_SITE_HEADER as FETCH_SITE_HEADER
from imbue.app_sdk.identity import IDENTITY_HEADER as IDENTITY_HEADER
from imbue.app_sdk.identity import LOOPBACK_ADDRESSES as LOOPBACK_ADDRESSES
from imbue.app_sdk.identity import LOOPBACK_HOSTNAMES as LOOPBACK_HOSTNAMES
from imbue.app_sdk.identity import OTHER_ORIGIN_REFUSAL as OTHER_ORIGIN_REFUSAL
from imbue.app_sdk.identity import UNTRUSTED_LOOPBACK_REFUSAL as UNTRUSTED_LOOPBACK_REFUSAL
from imbue.app_sdk.identity import RequestIdentity as RequestIdentity
from imbue.app_sdk.identity import RequestProvenance as RequestProvenance
from imbue.app_sdk.identity import describe_provenance_refusal as describe_provenance_refusal
from imbue.app_sdk.identity import is_from_another_origin as is_from_another_origin
from imbue.app_sdk.identity import is_loopback_address as is_loopback_address
from imbue.app_sdk.identity import is_loopback_host as is_loopback_host
from imbue.app_sdk.identity import is_owner_participant as is_owner_participant
from imbue.app_sdk.identity import parse_identity_header as parse_identity_header
from imbue.app_sdk.identity import participant_key_of as participant_key_of
from imbue.app_sdk.identity import visiting_user_id as visiting_user_id
