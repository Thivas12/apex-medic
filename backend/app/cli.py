import argparse
import json
import sqlite3
from pathlib import Path
from backend.app.agent.rules import recommend_next_step

from backend.app.db.repository import (
    append_evidence,
    create_case,
    get_case,
    get_history,
    initialise_database,
)
from backend.app.models.case import Case
from backend.app.db.config import PostgresDatabase, StorageUnavailable, resolve_database


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / "data" / "private" / "cases.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage ApexMedic cases.")

    parser.add_argument(
        "command",
        choices=[
            "import",
            "show",
            "add-observation",
            "add-test",
            "history",
            "next-step",
            "answer"
        ],
    )
    parser.add_argument("value", help="Import file path or case ID.")
    parser.add_argument("entry_file", nargs="?")
    parser.add_argument("--event-id")
    parser.add_argument("--sqlite", action="store_true", help="Use the original local SQLite database.")
    args = parser.parse_args()

    is_update = args.command in {"add-observation", "add-test", "answer"}

    if is_update and (not args.entry_file or not args.event_id):
        parser.error("Updates require an entry file and --event-id.")

    if not is_update and (args.entry_file or args.event_id):
        parser.error("Entry files and event IDs are only used for updates.")

    try:
        database = resolve_database(DATABASE_PATH if args.sqlite else None)
        if args.command == "import":
            contents = Path(args.value).read_text(encoding="utf-8-sig")
            case = Case.model_validate_json(contents)

            initialise_database(database)
            create_case(database, case)

            print(f"Saved case: {case.case_id}")
            return

        if isinstance(database, Path) and not database.is_file():
            raise ValueError("No database exists yet. Import a case first.")
        if isinstance(database, PostgresDatabase):
            initialise_database(database)

        if args.command == "show":
            case = get_case(database, args.value)

            if case is None:
                raise ValueError(f"Case '{args.value}' was not found.")

            print(case.model_dump_json(indent=2))

        elif args.command == "history":
            print(
                json.dumps(
                    get_history(database, args.value),
                    indent=2,
                )
            )
        elif args.command == "next-step":
            case = get_case(database, args.value)

            if case is None:
                raise ValueError(f"Case '{args.value}'was not found")

            decision = recommend_next_step(case)
            print(decision.model_dump_json(indent=2))

        else:
            event_type = {
                "add-observation" : "observation",
                'add-test' : "test",
                "answer" : "answer"
            }[args.command]

            added = append_evidence(
                database,
                args.value,
                event_type,
                Path(args.entry_file).read_text(encoding="utf-8-sig"),
                args.event_id,
            )

            print(
                "Evidence recorded."
                if added
                else "Already recorded; no changes."
            )
            

    except (OSError, ValueError, sqlite3.Error, StorageUnavailable) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
