"""Stage runners for pipeline execution.

Importing this package populates the STAGE_RUNNERS registry via
module-level self-registration in each concrete stage module.
"""

from build_your_room.stages.base import (  # noqa: F401
    STAGE_RUNNERS,
    StageRunnerFn,
    get_stage_runner,
    register_stage_runner,
)

# Import concrete stage modules to trigger self-registration.
import build_your_room.stages.spec_author as _spec_author  # noqa: F401
import build_your_room.stages.impl_plan as _impl_plan  # noqa: F401
import build_your_room.stages.impl_task as _impl_task  # noqa: F401
import build_your_room.stages.code_review as _code_review  # noqa: F401
import build_your_room.stages.validation as _validation  # noqa: F401
