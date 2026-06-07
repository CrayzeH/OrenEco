import argparse
import sqlite3

from external_environment_data import (
    ensure_environment_columns,
    sync_forests_from_overpass,
    sync_pollution_from_openmeteo,
    sync_pollution_from_openaq,
    sync_region_boundary_from_overpass,
)


DATABASE = "oreneco.db"


def main():
    parser = argparse.ArgumentParser(description="Sync OrenEco environmental data")
    parser.add_argument(
        "source",
        choices=["forests", "pollution", "boundary", "openaq", "all"],
        help="Data source to sync"
    )
    args = parser.parse_args()

    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    ensure_environment_columns(db)

    if args.source in ("forests", "all"):
        print(sync_forests_from_overpass(db))

    if args.source in ("boundary", "all"):
        print(sync_region_boundary_from_overpass(db))

    if args.source in ("pollution", "all"):
        print(sync_pollution_from_openmeteo(db))

    if args.source == "openaq":
        print(sync_pollution_from_openaq(db))

    db.close()


if __name__ == "__main__":
    main()
