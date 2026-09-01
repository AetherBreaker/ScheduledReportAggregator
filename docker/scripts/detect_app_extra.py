"""Print `--extra app` for uv if pyproject.toml defines an `app` optional-dependency group."""

# Standard library imports
import tomllib

with open("/app/pyproject.toml", "rb") as f:
    data = tomllib.load(f)

optional_deps = data.get("project", {}).get("optional-dependencies", {})
if "app" in optional_deps:
    print("--extra app", end="")
