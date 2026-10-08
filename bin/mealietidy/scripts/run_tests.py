"""Run all discovered tests against a fresh, seeded local Mealie instance."""

import subprocess
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
COMPOSE: tuple[str, ...] = ("docker", "compose", "-f", str(ROOT / "compose.test.yaml"))


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    try:
        run(*COMPOSE, "down")
        try:
            run(*COMPOSE, "up", "-d")
            run(sys.executable, "scripts/seed_mealie_v3_28.py")
            run(
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
                "-v",
            )
            run(sys.executable, "tests/verify_mealie_v3_28.py")
            run(sys.executable, "tests/verify_apply_mealie_v3_28.py")
            run(sys.executable, "tests/verify_dedup_mealie_v3_28.py")
        finally:
            run(*COMPOSE, "down")
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"Test run failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
