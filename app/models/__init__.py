"""SQLAlchemy ORM models describing the persisted domain schema.

Importing this package imports every model module, which is required twice over.
SQLAlchemy resolves ``relationship()`` targets given as strings through the
declarative registry, so a model whose module was never imported cannot be
resolved and mapper configuration fails at first use. Alembic's autogenerate
compares ``Base.metadata`` against the live database and only sees tables whose
modules have been imported, so an unimported model is silently left out of the
migration rather than reported as an error.

Anything needing complete metadata — the migration environment, the test
fixtures that create tables — imports this package rather than individual
modules.
"""

from app.models.ad import Ad
from app.models.ad_account import AdAccount
from app.models.ad_creative import AdCreative
from app.models.ad_set import AdSet
from app.models.campaign import Campaign
from app.models.enums import (
    EntityStatus,
    InsightLevel,
    RecommendationAction,
    RecommendationSeverity,
    RecommendationStatus,
)
from app.models.insight import InsightRecord
from app.models.recommendation import Recommendation

__all__ = [
    "Ad",
    "AdAccount",
    "AdCreative",
    "AdSet",
    "Campaign",
    "EntityStatus",
    "InsightLevel",
    "InsightRecord",
    "Recommendation",
    "RecommendationAction",
    "RecommendationSeverity",
    "RecommendationStatus",
]
