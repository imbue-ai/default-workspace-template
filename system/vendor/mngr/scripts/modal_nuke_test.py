from scripts.modal_nuke import _nuke_resources


def test_nuke_resources_acts_on_every_identifier() -> None:
    acted_on: list[tuple[str, str]] = []

    def record_success(identifier: str, environment: str) -> tuple[bool, str]:
        acted_on.append((identifier, environment))
        return True, ""

    failure_count = _nuke_resources(["ap-h8Kq2vRm", "ap-Zx41pLdT"], "Stopping app", record_success, "mngr-test-4318")

    assert acted_on == [("ap-h8Kq2vRm", "mngr-test-4318"), ("ap-Zx41pLdT", "mngr-test-4318")]
    assert failure_count == 0


def test_nuke_resources_counts_failures_and_keeps_going() -> None:
    acted_on: list[str] = []

    def fail_the_first(identifier: str, environment: str) -> tuple[bool, str]:
        del environment
        acted_on.append(identifier)
        return identifier != "ap-h8Kq2vRm", "app not found"

    failure_count = _nuke_resources(["ap-h8Kq2vRm", "ap-Zx41pLdT"], "Stopping app", fail_the_first, "mngr-test-4318")

    assert acted_on == ["ap-h8Kq2vRm", "ap-Zx41pLdT"]
    assert failure_count == 1
