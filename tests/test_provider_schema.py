"""Schema-level checks for provider profile storage."""

from src.infrastructure.database.models import ProviderModel


def test_provider_model_includes_searchable_location_columns() -> None:
    """Provider profiles should expose structured location fields for matching."""
    columns = ProviderModel.__table__.c

    assert "usuario_id" in columns
    assert "rubros" in columns
    assert "ciudad" in columns
    assert "barrio" in columns
    assert "lat" in columns
    assert "lon" in columns
    assert "zona" not in columns  # zona was removed, use {barrio, ciudad}


def test_provider_model_constraints_support_single_profile_per_user() -> None:
    """The ORM should describe the integrity constraints for provider profiles."""
    constraint_names = {constraint.name for constraint in ProviderModel.__table__.constraints}
    index_names = {index.name for index in ProviderModel.__table__.indexes}

    assert "uq_providers_usuario_id" in constraint_names
    assert "ck_providers_active_requires_location" in constraint_names
    assert "ix_providers_ciudad_barrio_activo" in index_names
    assert "ix_providers_zona" not in index_names  # zona index was removed


def test_provider_model_keeps_rubros_on_the_main_table() -> None:
    """Provider rubros should live directly on providers as the single source of truth."""
    rubros_column = ProviderModel.__table__.c["rubros"]

    assert rubros_column.nullable is False