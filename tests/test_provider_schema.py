"""Schema-level checks for provider profile storage."""

from src.infrastructure.database.models import ProviderModel, ProviderTradeModel, TradeModel


def test_provider_model_includes_searchable_location_columns() -> None:
    """Provider profiles should expose structured location fields for matching."""
    columns = ProviderModel.__table__.c

    assert "usuario_id" in columns
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


def test_trade_catalog_tables_exist() -> None:
    """Providers should have a normalized oficio catalog with a link table."""
    assert TradeModel.__table__.name == "trades"
    assert ProviderTradeModel.__table__.name == "provider_trades"
    assert {column.name for column in ProviderTradeModel.__table__.primary_key.columns} == {
        "provider_id",
        "trade_id",
    }