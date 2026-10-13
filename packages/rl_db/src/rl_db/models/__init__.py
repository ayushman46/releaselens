"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from rl_db.models.accounts import AuthSession, OAuthState, RefreshToken, TokenPurpose, UserToken
from rl_db.models.alerts import (
    AlertRule,
    NotificationChannel,
    NotificationKind,
    NotificationLog,
    NotificationStatus,
)
from rl_db.models.analyses import RegressionAnalysisRecord
from rl_db.models.audit import AuditLogEntry
from rl_db.models.billing import RateLimitBucket, StripeEvent, Subscription, UsageCounter
from rl_db.models.catalog import Environment, Project, Service
from rl_db.models.deployments import Deployment
from rl_db.models.identity import (
    ApiKey,
    Invitation,
    Membership,
    Organization,
    Plan,
    Role,
    User,
)
from rl_db.models.jobs import AnalysisJob, JobKind, JobStatus
from rl_db.models.outbox import OutboxEvent
from rl_db.models.telemetry import ServiceEdge, ServiceMinuteRollup, SpanRecord

__all__ = [
    "AlertRule",
    "AnalysisJob",
    "ApiKey",
    "AuditLogEntry",
    "AuthSession",
    "Deployment",
    "Environment",
    "Invitation",
    "JobKind",
    "JobStatus",
    "Membership",
    "NotificationChannel",
    "NotificationKind",
    "NotificationLog",
    "NotificationStatus",
    "OAuthState",
    "Organization",
    "OutboxEvent",
    "Plan",
    "Project",
    "RateLimitBucket",
    "RefreshToken",
    "RegressionAnalysisRecord",
    "Role",
    "Service",
    "ServiceEdge",
    "ServiceMinuteRollup",
    "SpanRecord",
    "StripeEvent",
    "Subscription",
    "TokenPurpose",
    "UsageCounter",
    "User",
    "UserToken",
]
