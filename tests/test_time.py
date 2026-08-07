from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select

from app.time import UTCDateTime, as_utc, utc_now


def test_utc_now_is_timezone_aware():
    now = utc_now()

    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)


def test_as_utc_normalizes_naive_and_offset_values():
    assert as_utc(datetime(2026, 8, 7, 12, 0)) == datetime(
        2026, 8, 7, 12, 0, tzinfo=UTC
    )
    assert as_utc(
        datetime(2026, 8, 7, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    ) == datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def test_utc_datetime_preserves_legacy_values_and_normalizes_offsets():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    timestamps = Table(
        "timestamps",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", UTCDateTime, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO timestamps (id, value) VALUES (?, ?)",
            (1, "2026-08-07 12:00:00.000000"),
        )
        connection.execute(
            timestamps.insert().values(
                id=2,
                value=datetime(2026, 8, 7, 14, 0, tzinfo=timezone(timedelta(hours=2))),
            )
        )

        values = connection.execute(
            select(timestamps.c.value).order_by(timestamps.c.id)
        ).scalars().all()
        stored_offset_value = connection.exec_driver_sql(
            "SELECT value FROM timestamps WHERE id = 2"
        ).scalar_one()

    assert values == [
        datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    ]
    assert stored_offset_value == "2026-08-07 12:00:00.000000"
