"""Process exit codes. They are part of the public CLI contract: never renumber them."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    USAGE_ERROR = 2
    VALIDATION_FAILED = 3
    CONFIG_ERROR = 4
    PROVIDER_ACCOUNT_ERROR = 5
    RUN_PAUSED = 6
    BUDGET_REACHED = 7
    COMPLETED_WITH_FAILURES = 8
    ENVIRONMENT_CHECK_FAILED = 9
    CALIBRATION_REQUIRED = 10
    OUTPUT_ERROR = 11
    AUTHORIZATION_REQUIRED = 12  # sending content to a provider needs explicit consent
    INTERRUPTED = 130
